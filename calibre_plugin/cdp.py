"""Live-Token-Grab ueber das Chrome DevTools Protocol."""

import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.parse import quote as _url_quote, urlsplit

from .partners import PARTNERS
from .tolino import RELOGIN, RELOGIN_THEN, TolinoAuthError
from .cdp_transport import _Ws, _http_get_json
from .cdp_scripts import _EXCHANGE_SNIPPET, _GRAB_SNIPPET, idb_expression


# ---------------------------------------------------------------- Vorbedingungen

# Keep the established cdp import path while locating browser discovery in
# its dedicated module.
from . import browser_finder
from .browser_finder import pick_chromium


def _start_port():
    """Pick a fresh ephemeral loopback port for an owned grabber window."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _profile_identity():
    """Kennung des aktuellen Benutzers fuer den Profilpfad.

    Auf einem geteilten Rechner kann sonst ein anderer Benutzer den festen
    Pfad in /tmp vorbelegen (Symlink oder fremdes Profil). Die Kennung ist
    die Benutzer-ID -- bei Windows der Benutzername, entschaerft.
    """
    if os.name == "nt":
        name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        return "".join(ch if (ch.isalnum() or ch in "._-") else "_"
                       for ch in name) or "user"
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    return "user"


def _profile_dir():
    """Private BASE directory for grabber profiles and the ownership record."""
    return os.path.join(tempfile.gettempdir(),
                        "tolino_grab_profile-%s" % _profile_identity())


def _ensure_profile_base():
    """Basisverzeichnis anlegen -- nur fuer den eigenen Benutzer lesbar."""
    base = _profile_dir()
    os.makedirs(base, mode=0o700, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


# ---------------------------------------------------------- Besitz-Nachweis
#
# Ein Anmeldefenster gehoert nur dann dem Plugin, wenn ALLE drei Punkte
# zusammenpassen: eindeutiges Profil mit geheimem Marker, passender laufender
# Prozess (Linux /proc) und ein DevTools-Endpunkt genau dieses Prozesses.
# Ohne diesen Nachweis werden weder Tokens gelesen noch Browser.close gesendet.
_ACTIVE = {"port": None, "profile": "", "token": ""}
_LAUNCHED = {"process": None, "port": None,
             "profile": "", "token": ""}
_OWNER_MARKER = ".tolino-grab-owner"


def _owner_file():
    return os.path.join(_profile_dir(), "owner.json")


def _write_owner(record):
    _ensure_profile_base()
    path = _owner_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_owner():
    try:
        with open(_owner_file(), "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError):
        return None
    profile = str(record.get("profile") or "")
    token = str(record.get("token") or "")
    if port <= 0 or not profile or not token:
        return None
    return {"port": port, "profile": profile, "token": token}


def _profile_is_owned_child(record):
    """Accept only a real profile directory directly below our private base."""
    try:
        profile = os.path.abspath(str(record["profile"]))
        base = os.path.abspath(_profile_dir())
        return (not os.path.islink(base)
                and os.path.dirname(profile) == base
                and os.path.basename(profile).startswith("profile-")
                and not os.path.islink(profile)
                and os.path.realpath(os.path.dirname(profile))
                == os.path.realpath(base))
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _remove_owned_profile(record, process_stopped=False):
    """Remove only the marked direct-child profile after its process exits."""
    if not _marker_matches(record):
        return False
    if not process_stopped and _process_matches(record):
        return False
    profile = os.path.abspath(str(record["profile"]))
    if os.path.islink(profile) or os.path.realpath(profile) != profile:
        return False
    shutil.rmtree(profile, ignore_errors=True)
    return not os.path.lexists(profile)


def _same_record(left, right):
    """Whether two persisted/in-memory ownership records identify one launch."""
    return bool(left and right
                and left.get("port") == right.get("port")
                and left.get("profile") == right.get("profile")
                and left.get("token") == right.get("token"))


def _clear_launch_state(record):
    """Forget only in-memory state belonging to a launch just cleaned up."""
    if _same_record(_ACTIVE, record):
        _ACTIVE.update({"port": None, "profile": "", "token": ""})
    if _same_record(_LAUNCHED, record):
        _LAUNCHED.update({"process": None, "port": None,
                          "profile": "", "token": ""})


def _marker_matches(record):
    if (not isinstance(record, dict) or not _profile_is_owned_child(record)
            or not os.path.isdir(record["profile"])):
        return False
    try:
        with open(os.path.join(record["profile"], _OWNER_MARKER),
                  "r", encoding="utf-8") as handle:
            return handle.read().strip() == record["token"]
    except OSError:
        return False


# Browser main processes carry these names in /proc/<pid>/exe. A short-lived
# launcher (snap, flatpak, flatpak-spawn) may exit while its browser child
# keeps running with the same profile and debugging port.
_BROWSER_EXE_HINTS = ("chrome", "chromium", "brave", "edge", "vivaldi",
                      "opera", "headless_shell")


def _proc_argv(pid):
    """Command line of a Linux process, or an empty list."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return handle.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return []


def _is_browser_process(pid):
    """True when /proc proves that `pid` is a Chromium-family browser."""
    try:
        exe = os.path.basename(os.readlink("/proc/%d/exe" % pid)).lower()
    except OSError:
        return False
    return any(hint in exe for hint in _BROWSER_EXE_HINTS)


def _owned_pids(record):
    """PIDs that provably run OUR profile on OUR debugging port.

    Launchers (snap/flatpak) may have exited and handed the profile to a
    child with a different PID. The unique profile path plus the debugging
    port in the same command line identify the real browser without
    trusting a port number alone.
    """
    if (not isinstance(record, dict) or not _marker_matches(record)
            or not os.path.isdir("/proc")):
        return []
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError):
        return []
    profile = os.path.abspath(str(record.get("profile") or ""))
    if port <= 0 or not profile or not _profile_is_owned_child(record):
        return []
    flag = "--remote-debugging-port=%d" % port
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        argv = _proc_argv(pid)
        profile_flag = "--user-data-dir=%s" % profile
        if (argv and profile_flag in argv and flag in argv
                and _is_browser_process(pid)):
            pids.append(pid)
    return pids


def _process_matches(record):
    """Require live process evidence for the recorded profile and port.

    Linux can verify Chromium's command line. Elsewhere, only the exact live
    Popen child from this process is authoritative; a marker and a listening
    port alone never prove that an endpoint belongs to this plugin.
    """
    process = _LAUNCHED.get("process")
    try:
        if (process is not None
                and int(_LAUNCHED.get("port") or 0)
                == int((record or {}).get("port") or 0)
                and _LAUNCHED.get("profile") == (record or {}).get("profile")
                and _LAUNCHED.get("token") == (record or {}).get("token")
                and process.poll() is None):
            return True
    except (AttributeError, TypeError, ValueError):
        pass
    if os.path.isdir("/proc"):
        return bool(_owned_pids(record))
    return False


def _owner_alive(record):
    if not isinstance(record, dict):
        return False
    port = record.get("port")
    if (isinstance(port, bool) or not isinstance(port, int)
            or not 0 < port < 65536):
        return False
    if not _process_matches(record) or not _marker_matches(record):
        return False
    return bool(_http_get_json(
        "http://127.0.0.1:%d/json/version" % port, 2))


def _verified_owner(port=None):
    """Return the live owned-window record, optionally for one port."""
    record = _read_owner() or _active_record(port)
    if not record or not _owner_alive(record):
        return None
    try:
        owner_port = int(record["port"])
        if port is not None and owner_port != int(port):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return record


def _new_profile_dir():
    return tempfile.mkdtemp(prefix="profile-", dir=_ensure_profile_base())


def _write_marker(profile, token):
    path = os.path.join(profile, _OWNER_MARKER)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _reader_url(partner_id):
    partner = PARTNERS.get(int(partner_id)) or {}
    return (partner.get("reader_url")
            or "https://webreader.mytolino.com/library/index.html")


def devtools_port_alive():
    """True when OUR grabber window's DevTools endpoint answers right now."""
    return _verified_owner() is not None


def _loopback_host(host):
    host = str(host or "").casefold()
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("DevTools websocket host is not loopback") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_loopback:
        raise ValueError("DevTools websocket host is not loopback")
    return str(address)


_DEVTOOLS_PATH = re.compile(
    r"/devtools/(?:page|browser)/[A-Za-z0-9._:-]+$")


def _validate_devtools_ws_parts(host, port, path, expected_port=None):
    """Validate an already-parsed DevTools address before connecting."""
    host = _loopback_host(host)
    try:
        port = int(port)
        expected_port = (None if expected_port is None
                         else int(expected_port))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid DevTools websocket port") from exc
    path = str(path)
    if (not 0 < port <= 65535
            or (expected_port is not None and port != expected_port)
            or not _DEVTOOLS_PATH.fullmatch(path)):
        raise ValueError("invalid DevTools websocket address")
    return host, port, path


def _devtools_ws_address(ws_url, expected_port=None):
    """Parse only a loopback DevTools WebSocket on the verified browser port."""
    try:
        parsed = urlsplit(str(ws_url or ""))
        if (parsed.scheme != "ws" or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("invalid DevTools websocket URL")
        return _validate_devtools_ws_parts(
            parsed.hostname, parsed.port, parsed.path, expected_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid DevTools websocket URL") from exc


def _evaluate_raw_in_target(ws_url, expression, timeout=10,
                            await_promise=False, expected_port=None):
    """Like _evaluate_in_target but returns CDP's full result dict.

    Returns ``{"ok": bool, "value": <decoded JSON value or raw string>,
    "exception": str}`` – callers that need the exceptionDetails (e.g.
    an in-page fetch whose failure text matters) use this variant.
    """
    try:
        host, port, ws_path = _devtools_ws_address(ws_url, expected_port)
        ws = _Ws.connect(host, port, ws_path, timeout=timeout)
    except (OSError, ValueError):
        return {"ok": False, "value": None, "exception": "connect failed"}
    try:
        ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "returnByValue": True,
            "awaitPromise": bool(await_promise),
        }})
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = ws.recv_json(timeout=max(1, deadline - time.time()))
            if message.get("id") != 1:
                continue
            if "error" in message:
                return {"ok": False, "value": None,
                        "exception": str(message.get("error"))[:200]}
            result = (message.get("result") or {}).get("result") or {}
            details = (message.get("result") or {}).get("exceptionDetails")
            if details:
                text = str((details.get("exception") or {}).get("description")
                           or details.get("text") or "page error")
                return {"ok": False, "value": None,
                        "exception": text[:300]}
            if result.get("type") == "string":
                return {"ok": True, "value": result.get("value"),
                        "exception": None}
            if "value" in result:
                return {"ok": True, "value": result.get("value"),
                        "exception": None}
            return {"ok": False, "value": None,
                    "exception": "no value returned"}
        return {"ok": False, "value": None, "exception": "evaluate timeout"}
    except OSError as exc:
        return {"ok": False, "value": None, "exception": str(exc)[:200]}
    finally:
        ws.close()


def _evaluate_in_target(ws_url, expression, timeout=10, await_promise=False,
                        expected_port=None):
    """Run a JS expression in a DevTools page target, return its JSON value."""
    raw = _evaluate_raw_in_target(ws_url, expression, timeout, await_promise,
                                  expected_port)
    if not raw["ok"]:
        return None
    try:
        parsed = json.loads(raw["value"])
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {
        "refresh": [t for t in parsed.get("refresh", [])
                    if isinstance(t, str)],
        "hardware": [t for t in parsed.get("hardware", [])
                     if isinstance(t, str)],
        "idb": [t for t in parsed.get("idb", []) if isinstance(t, str)],
    }


# ---------------------------------------------------------------- Fenster-Flow

def launch_reader_window(partner_id):
    """Open a dedicated grabber Chromium window on the Web Reader page.

    Returns the DevTools port or raises TolinoAuthError when no Chromium
    browser is installed. A leftover grabber window from an earlier
    attempt is reused when its DevTools endpoint still answers (the user
    may have kept the sign-in window open across retries).
    """
    binary, label = pick_chromium() or (None, None)
    if not binary:
        message, code, status = browser_finder.missing_browser_error()
        raise TolinoAuthError(message, code=code,
                              detail="Host-Sonde: %s" % status)
    # Reuse ONLY our own window: ownership record + private marker + live
    # process evidence must match. A foreign listener is never selected.
    # owner.json may have been lost after an earlier failed close; the
    # in-memory proof still identifies this process's own window/profile.
    previous = _read_owner() or _active_record()
    if previous and _owner_alive(previous):
        _ACTIVE.update(previous)
        return int(previous["port"])
    # Do not remove a profile while its browser may still be running. On an
    # unprovable or failed close, stop here and preserve the ownership record
    # so a later attempt can retry safely.
    if previous and (_marker_matches(previous) or _process_matches(previous)):
        if not _marker_matches(previous) or not close_grabber_window():
            raise TolinoAuthError(
                "Das vorherige Anmeldefenster konnte nicht sicher "
                "geschlossen werden. Bitte schliessen Sie es und versuchen "
                "Sie es erneut.", code="B07",
                detail="Vorheriger Browser ist nicht sicher beendet.")
        if _endpoint_answers(previous, 0.2):
            raise TolinoAuthError(
                "Das vorherige Anmeldefenster wird noch beendet. "
                "Bitte warten Sie kurz und versuchen Sie es erneut.",
                code="B07", detail="Vorheriger DevTools-Endpunkt antwortet noch.")
    # Never recursively clean the shared private base here: a stale or
    # malformed owner record is not proof that every profile is unused.
    # The verified close path removes the profile it can prove it owns.
    port = _start_port()
    profile = _new_profile_dir()
    token = secrets.token_hex(16)
    _write_marker(profile, token)
    record = {"port": port, "profile": profile, "token": token}
    _write_owner(record)
    flags = [
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=%d" % port,
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--window-size=1280,900",
        "--new-window",
        _reader_url(partner_id),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008  # DETACHED_PROCESS: Calibre bleibt bedienbar
    # Kandidaten nacheinander versuchen: ein defekter erster Eintrag
    # darf die Anmeldung nicht in den System-Browser-Fallback schicken.
    attempts = [(list(binary), label)]
    last_error = None
    process = None
    index = 0
    while index < len(attempts):
        argv, candidate_label = attempts[index]
        index += 1
        try:
            process = subprocess.Popen(
                list(argv) + flags, creationflags=creationflags,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except OSError as exc:
            last_error = (candidate_label, exc)
            if index == 1:
                # Erst beim ersten Fehlschlag die übrigen Kandidaten
                # ermitteln (die Host-Sonde kostet einen Prozessstart).
                attempts.extend(
                    cand for cand in browser_finder.chromium_options()
                    if cand[0] != binary)
    else:
        # Auch ohne Prozess wurden bereits Profil, Marker und Besitzdatei
        # angelegt. Ein fehlgeschlagener Start darf sie nicht liegen lassen.
        _abandon_failed_launch(None, record)
        raise TolinoAuthError(
            "Chromium konnte nicht gestartet werden.", code="B07",
            detail="%s: %s" % (last_error[0], last_error[1]))
    _write_owner(record)
    _ACTIVE.update(record)
    # Fuer den spaeteren Close: das Popen-Objekt beweist, dass es unser
    # Kind ist -- auch auf Windows/macOS ohne /proc.
    _LAUNCHED.update({"process": process, "port": port,
                      "profile": profile, "token": token})
    # Wait until the DevTools endpoint answers.
    deadline = time.time() + 20
    while time.time() < deadline:
        if _http_get_json("http://127.0.0.1:%d/json/version" % port, 2):
            return port
        time.sleep(0.5)
    # Do not leave an orphan instance, profile or ownership record behind
    # on a failed start.
    _abandon_failed_launch(process, record)
    raise TolinoAuthError(
        "Chromium reagiert nicht. Bitte alle Chromium-Fenster schliessen "
        "und es erneut versuchen.", code="B07", detail="Port %d" % port)


def _stop_launched_process(process, timeout=2):
    """Stop our child and report whether the process is proven to have exited."""
    if process is None:
        return True
    try:
        if process.poll() is not None:
            return True
        process.terminate()
        process.wait(timeout=max(0.2, timeout))
    except Exception:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
        except Exception:
            return False
    try:
        return process.poll() is not None
    except Exception:
        return False


def _abandon_failed_launch(process, record):
    """Clean only this marked profile after the attempted child exits."""
    if not _stop_launched_process(process):
        return
    if not _remove_owned_profile(record, process_stopped=True):
        return
    if _same_record(_read_owner(), record):
        try:
            os.remove(_owner_file())
        except OSError:
            pass
    _clear_launch_state(record)


def read_idb_tokens(ws_url, db_names, timeout=10, expected_port=None):
    """Walk every IndexedDB database's object stores for token values.

    Returns the merged ``{"refresh": [...], "hardware": [...]}`` dict;
    databases that fail to open are skipped (the localStorage/session
    scan already ran in the main grab).
    """
    merged = {"refresh": [], "hardware": []}
    for name in db_names or []:
        if not name or not str(name).strip():
            continue
        result = _evaluate_in_target(ws_url, idb_expression(name), timeout,
                                     await_promise=True,
                                     expected_port=expected_port)
        if not result:
            continue
        for bucket in ("refresh", "hardware"):
            for value in result.get(bucket) or []:
                if value not in merged[bucket]:
                    merged[bucket].append(value)
    return merged


def _grab_page_tokens(ws_url, timeout=10, expected_port=None):
    """ONE full storage grab: localStorage, sessionStorage, IndexedDB."""
    grabbed = _evaluate_in_target(ws_url, _GRAB_SNIPPET, timeout,
                                  await_promise=True,
                                  expected_port=expected_port) or {}
    for bucket in ("refresh", "hardware", "idb"):
        if not isinstance(grabbed.get(bucket), list):
            grabbed[bucket] = []
    idb_names = list(grabbed.get("idb"))
    if idb_names:
        idb_tokens = read_idb_tokens(ws_url, idb_names, timeout,
                                     expected_port=expected_port)
        for bucket in ("refresh", "hardware"):
            for value in idb_tokens.get(bucket) or []:
                if value not in grabbed[bucket]:
                    grabbed[bucket].append(value)
    return grabbed


def _is_reader_url(url):
    """Match HTTPS pages hosted by tolino, not URLs merely containing its name."""
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return (parsed.scheme == "https"
            and (host == "mytolino.com" or host.endswith(".mytolino.com")))


def _reader_ws_url(targets, port):
    """First mytolino page's validated WebSocket on the owned port."""
    if not isinstance(targets, list):
        return None
    for target in targets:
        if (not isinstance(target, dict) or target.get("type") != "page"
                or not _is_reader_url(target.get("url"))):
            continue
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        try:
            _devtools_ws_address(ws_url, port)
        except (TypeError, ValueError):
            continue
        return ws_url
    return None


def grab_once_from_grabber(port=None, timeout=12):
    """ONE grab attempt against OUR grabber window (no polling).

    Returns the grabbed dict (refresh/hardware/idb lists, possibly empty)
    or None when no reader tab exists or the endpoint is not provably ours.
    Used by the extract button so the Calibre GUI never blocks for minutes
    waiting for a sign-in that may not happen – the user just clicks the
    button again after signing in.
    """
    record = _verified_owner(port)
    if record is None:
        return None
    port = int(record["port"])
    ws_url = _reader_ws_url(
        _http_get_json("http://127.0.0.1:%d/json/list" % port), port)
    if ws_url is None:
        return None
    return _grab_page_tokens(ws_url, timeout, expected_port=port)


def _oauth_rejection_reason(body):
    """Short 'error: description' for a Keycloak/OAuth error frame; '' otherwise."""
    text = str(body or "")
    lowered = text.casefold()
    for marker in ("invalid_grant", "invalid_client",
                   "unauthorized_client", "invalid_request",
                   "unsupported_grant_type"):
        if marker not in lowered:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        code = str(data.get("error") or marker)
        description = str(data.get("error_description") or "").strip()[:200]
        if description and description.casefold() != code.casefold():
            return "%s: %s" % (code, description)
        return code
    return ""


def exchange_refresh_in_browser(ws_url, token_url, form_body, timeout=25):
    """Run the refresh-token grant inside the reader page via in-page fetch.

    Returns ``(status, body_text)``. ``status`` mirrors the endpoint's
    HTTP code (0 when the in-page fetch itself failed). The bot-protection
    WAF in front of the token endpoint accepts this request because it is
    the reader's own fingerprint – the exact request family the reader
    performs on every background rotation.

    Raises TolinoAuthError on a definitive OAuth rejection of this
    candidate (invalid_grant & friends): replaying a spent grant cannot
    succeed, so the callers fail fast with instructions instead of
    retrying the same token.
    """
    record = _verified_owner()
    if record is None:
        return 0, "browser ownership could not be verified"
    expected_port = int(record["port"])
    try:
        _devtools_ws_address(ws_url, expected_port)
    except (TypeError, ValueError):
        return 0, "DevTools endpoint did not match the owned browser"
    expression = (_EXCHANGE_SNIPPET
                  .replace("TOKEN_URL_PLACEHOLDER",
                           json.dumps(str(token_url)))
                  .replace("BODY_PLACEHOLDER", json.dumps(str(form_body))))
    raw = _evaluate_raw_in_target(ws_url, expression, timeout,
                                  await_promise=True,
                                  expected_port=expected_port)
    if not raw.get("ok"):
        return 0, "page evaluate failed: %s" % (raw.get("exception") or "?")
    try:
        payload = json.loads(raw.get("value") or "{}")
    except ValueError:
        return 0, "unparseable page reply"
    status = int(payload.get("status") or 0)
    body = str(payload.get("body") or "")
    # 400/401/403 mit OAuth-Fehlerframe = ECHTE Antwort des Token-Endpunkts:
    # sofort TolinoAuthError (kein eigener Fallback-POST -- ein Replay
    # wurde nur erneut abgewiesen); die Rufer warten auf NEUE Kandidaten.
    reason = _oauth_rejection_reason(body) if status in (400, 401, 403) else ""
    if reason:
        raise TolinoAuthError(
            "Tolino hat den gelesenen Zugang abgelehnt. " + RELOGIN,
            code="A02", detail="HTTP %d, %s" % (status, reason))
    # Pass through non-OAuth responses (page failures, WAF HTML pages,
    # unexpected payloads) for the caller to classify.
    return status, body


def reader_ws_url(port=None):
    """WebSocket-Debugger-URL des ersten mytolino-Reader-Tabs (oder None)."""
    record = _verified_owner(port)
    if record is None:
        return None
    port = int(record["port"])
    return _reader_ws_url(
        _http_get_json("http://127.0.0.1:%d/json/list" % port), port)


def _browser_ws_for_record(record, timeout=2):
    """Return the CDP address only when marker, process and endpoint agree."""
    try:
        port = int(record.get("port") or 0)
        if (not 0 < port < 65536 or not _process_matches(record)
                or not _marker_matches(record)):
            return None
        version = _http_get_json(
            "http://127.0.0.1:%d/json/version" % port, timeout)
        ws_url = str((version or {}).get("webSocketDebuggerUrl") or "")
        return _devtools_ws_address(ws_url, port)
    except (AttributeError, TypeError, ValueError):
        return None


def _send_browser_close(host, port, path, timeout=2, expected_port=None):
    """Send Browser.close over a validated loopback DevTools WebSocket."""
    try:
        if expected_port is None:
            expected_port = port
        host, port, path = _validate_devtools_ws_parts(
            host, port, path, expected_port=expected_port)
        ws = _Ws.connect(host, port, path, timeout=timeout)
        try:
            ws.send_json({"id": 1, "method": "Browser.close"})
            try:
                # Die Antwort darf fehlen -- der Browser fährt ja ohnehin
                # herunter und meldet sich danach nicht mehr.
                ws.recv_json(timeout=timeout)
            except OSError:
                pass
        finally:
            ws.close()
        return True
    except Exception:
        return False


def _close_log(message):
    """Ein Satz ins Calibre-Log -- ohne Pfade und Tokens."""
    try:
        from .tolino import log_warning
        log_warning("Anmeldefenster: " + str(message))
    except Exception:
        pass  # ohne Calibre-Log bleibt der Close trotzdem wirksam


def _page_targets(record, timeout=0.5):
    """Offene Seiten-Targets des eigenen Fensters (None: nicht lesbar)."""
    try:
        port = int(record.get("port") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    if port <= 0:
        return None
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port, timeout)
    if not isinstance(targets, list):
        return None
    return [target for target in targets
            if isinstance(target, dict) and target.get("type") == "page"]


def _window_gone(record, timeout=0.4):
    """True, wenn der Debug-Endpunkt weg ist oder kein Fenster mehr offen ist.

    `Browser.close` schliesst den Browser, der Endpunkt kann aber kurz
    nachleben; umgekehrt bleibt nach einem blockierten Entladen manchmal
    nur die Seite haengen. Beides gilt hier als «Fenster zu».
    """
    if not _endpoint_answers(record, timeout):
        return True
    targets = _page_targets(record, timeout)
    return targets == []


def _http_get_status(url, timeout=2):
    """Status einer lokalen Debug-Abfrage (0 = keine Antwort)."""
    try:
        from urllib.request import ProxyHandler, build_opener
        with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200)
    except Exception:
        return 0


def _close_page_targets(record, timeout=2):
    """Fenster ueber den Debug-Endpunkt schliessen, wenn `Browser.close`
    ignoriert wurde (z. B. wegen eines haengenden Entlade-Handlers).

    Nur mit bestandenem Besitznachweis: Profilmarker, Prozess und Endpunkt.
    """
    if (not _marker_matches(record) or not _process_matches(record)
            or not _endpoint_answers(record, timeout)):
        return False
    targets = _page_targets(record, timeout)
    if not targets:
        return False
    try:
        port = int(record["port"])
    except (KeyError, TypeError, ValueError):
        return False
    closed = False
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("id") or "").strip()
        if not target_id:
            continue
        status = _http_get_status(
            "http://127.0.0.1:%d/json/close/%s"
            % (port, _url_quote(target_id, safe="")), timeout)
        if 0 < status < 400:
            closed = True
    return closed


def _terminate_launched(record, timeout=2):
    """Stop the exact Popen child and verify that it exited."""
    process = _LAUNCHED.get("process")
    if process is None:
        return False
    if not _same_record(_LAUNCHED, record):
        return False
    return _stop_launched_process(process, timeout)


def _active_record(port=None):
    """Besitzsatz aus dem Speicher, wenn `owner.json` fehlt.

    Ein frueherer Schliessversuch konnte die Datei entfernen, obwohl das
    Fenster noch lebt; ohne diesen Satz waere es danach unerreichbar
.
    """
    if not _ACTIVE.get("port") or not _ACTIVE.get("profile") or not _ACTIVE.get("token"):
        return None
    try:
        active_port = int(_ACTIVE["port"])
    except (TypeError, ValueError):
        return None
    if port is not None and active_port != int(port):
        return None
    return {"port": active_port, "profile": _ACTIVE["profile"],
            "token": _ACTIVE["token"]}


def _endpoint_answers(record, timeout=0.5):
    """True while the recorded loopback DevTools port still answers."""
    if not isinstance(record, dict):
        return False
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError):
        return False
    if port <= 0 or port >= 65536:
        return False
    return bool(_http_get_json(
        "http://127.0.0.1:%d/json/version" % port, timeout))


def _terminate_owned_process(record, timeout=2):
    """Last-resort close for OUR window when Browser.close is ignored.

    Only on Linux, where ``/proc`` proves that the running browser owns
    exactly this profile and port. A recycled PID on Windows/macOS is never
    killed; there the graceful Browser.close remains the only mechanism.
    """
    if not isinstance(record, dict) or not os.path.isdir("/proc"):
        return False
    if not _marker_matches(record):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _owned_pids(record)
        if not pids:
            return (not _process_matches(record)
                    and not _endpoint_answers(record, 0.4))
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.time() + max(0.2, timeout / 2.0)
        while time.time() < deadline:
            if not _endpoint_answers(record, 0.4):
                return True
            time.sleep(0.1)
    return not _endpoint_answers(record, 0.4)


def close_grabber_window(timeout=2, port=None):
    """Close OUR private sign-in window; never another browser.

    Without a matching ownership record (private profile marker + process +
    endpoint) no ``Browser.close`` is sent at all. If Chromium ignores
    ``Browser.close`` (for example because the page blocks unload), the
    window is closed through the debug endpoint and only then the recorded
    process is terminated -- auf Linux ueber /proc, sonst ueber genau den
    Prozess, den wir gestartet haben.

    Der Besitznachweis (owner.json, Profil mit Marker) wird erst entfernt,
    wenn das Fenster wirklich zu ist. Frueher galt «Browser.close gesendet»
    schon als Erfolg: blieb das Fenster dann offen, war der einzige Weg zu
    seiner Identitaet geloescht und jeder weitere Versuch aussichtslos.
    """
    record = _read_owner() or _active_record(port)
    if port is not None and (not record
                             or int(record.get("port") or 0) != int(port)):
        record = None
    if record is None:
        return False
    if not _process_matches(record):
        return False
    # A false marker only prohibits cleanup; the trusted process+loopback
    # evidence still establishes the CDP endpoint for graceful shutdown.
    if _marker_matches(record) and not _endpoint_answers(record, timeout):
        return False
    if not _marker_matches(record):
        if os.path.isdir("/proc"):
            return False
        # Without the marker, never infer that the listening endpoint is ours.
        # Keep the ownership state unless the exact child was stopped AND the
        # endpoint is gone; otherwise a later close attempt still has context.
        if not _terminate_launched(record, timeout):
            return False
        if not _window_gone(record, timeout):
            return False
        _clear_launch_state(record)
        return True
    target = _browser_ws_for_record(record, timeout)
    expected_port = int(record["port"])
    closed = bool(target and target[1] == expected_port and _send_browser_close(
        target[0], target[1], target[2], timeout,
        expected_port=expected_port))
    if not closed:
        _close_log("Browser.close konnte nicht gesendet werden.")
    if _endpoint_answers(record, 0.4):
        # Chromium hat Browser.close (noch) nicht befolgt: die offenen
        # Fenster direkt ueber den Debug-Endpunkt schliessen.
        _close_page_targets(record, timeout)
    terminated = False
    deadline = time.time() + max(0.2, timeout)
    while time.time() < deadline:
        if _window_gone(record, 0.4):
            break
        time.sleep(0.1)
    if not _window_gone(record, 0.4):
        # Letzter Ausweg: genau unser eigener Startprozess (Popen,
        # alle Plattformen), danach der /proc-Nachweis (Linux).
        terminated = (_terminate_launched(record, timeout)
                      or _terminate_owned_process(record, timeout))
    gone = terminated or _window_gone(record, 0.4)
    if not gone or _process_matches(record):
        _close_log("Fenster oder Browserprozess liess sich nicht sicher "
                   "beenden; Besitznachweis bleibt fuer einen weiteren "
                   "Versuch erhalten.")
        return False
    if not _remove_owned_profile(record):
        _close_log("Das private Browserprofil konnte nicht sicher entfernt "
                   "werden; Besitznachweis bleibt erhalten.")
        return False

    owner = _read_owner()
    if owner is not None and not _same_record(owner, record):
        return False

    # owner.json is shared by launches in this process. Remove it only if it
    # still names the window just closed; a concurrent newer launch owns it.
    if owner is not None:
        try:
            os.remove(_owner_file())
        except OSError:
            pass
    _clear_launch_state(record)
    return True


def grabber_window_state(port=None, timeout=5):
    """Return whether the grabber window and a reader tab are available."""
    record = _verified_owner(port)
    if record is None:
        return {"window": False, "reader_tab": False}
    port = int(record["port"])
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port, timeout)
    if not isinstance(targets, list):
        return {"window": False, "reader_tab": False}
    reader_tab = False
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        if _is_reader_url(target.get("url")):
            reader_tab = True
            break
    return {"window": True, "reader_tab": reader_tab}


def describe_grab_state(port=None, timeout=12):
    """Describe which reader page is open, without returning token values."""
    state = grabber_window_state(port, timeout)
    if not state["window"]:
        return "kein Web-Reader-Tab im Anmeldefenster offen"
    if not state["reader_tab"]:
        return ("Anmeldefenster offen, aber ohne Web-Reader-Tab (der Tab "
                "zeigt gerade die Anmeldeseite des Buchhändlers)")
    grabbed = grab_once_from_grabber(port, timeout)
    if grabbed is None:
        return ("Anmeldefenster offen, aber der Web-Reader-Tab war gerade "
                "nicht lesbar")
    return ("In der Seite gefundene Zugänge: %d, Hardware-IDs: %d "
            "(IndexedDB: %s)" % (
                len(grabbed.get("refresh") or []),
                len(grabbed.get("hardware") or []),
                ", ".join(sorted(set(grabbed.get("idb") or []))) or "keine"))


def _raise_window_closed(progress):
    """Stop the wait with the same clear error after repeated endpoint loss."""
    if progress:
        progress("Anmeldefenster geschlossen – abgebrochen.")
    raise TolinoAuthError(
        "Das Anmeldefenster wurde geschlossen, bevor die Übernahme fertig war. "
        "Bitte erneut versuchen und es offen lassen.", code="B03")


def collect_grab(port, timeout=180, progress=None):
    """Poll the grabber window until the reader exposes the current token."""
    deadline = time.time() + timeout
    endpoint_misses = 0
    saw_reader = False
    while time.time() < deadline:
        record = _verified_owner(port)
        if record is None:
            endpoint_misses += 1
            if endpoint_misses >= 3:
                _raise_window_closed(progress)
            time.sleep(1)
            continue
        targets = _http_get_json("http://127.0.0.1:%d/json/list" % port)
        if not isinstance(targets, list):
            # Endpoint antwortet nicht (Fenster weg oder kurz gestoert).
            # Erst mehrere Fehlschlaege in Folge sind ein geschlossenes
            # Fenster -- ein einzelner war der False-Positive.
            endpoint_misses += 1
            if endpoint_misses >= 3:
                _raise_window_closed(progress)
            time.sleep(1)
            continue
        endpoint_misses = 0
        ws_url = _reader_ws_url(targets, port)
        if ws_url is None:
            saw_reader = saw_reader or any(
                isinstance(target, dict) and target.get("type") == "page"
                and _is_reader_url(target.get("url"))
                for target in targets)
            foreign_page = any(
                isinstance(target, dict) and target.get("type") == "page"
                and not str(target.get("url") or "").startswith(
                    ("about:", "chrome:", "edge:", "devtools://"))
                and not _is_reader_url(target.get("url"))
                for target in targets)
            # Fenster lebt, aber kein Reader-Tab: die Anmeldung laeuft
            # auf der Partner-/Keycloak-Seite (andere Domain) oder der
            # Tab lädt gerade. Beides ist KEIN geschlossenes Fenster.
            if progress:
                if foreign_page:
                    progress("Das Anmeldefenster ist offen – bitte dort "
                             "anmelden, bis die Bücherliste lädt …")
                else:
                    progress("Der Web Reader wird geladen …")
            time.sleep(2)
            continue
        saw_reader = True
        result = _grab_page_tokens(ws_url, 10, expected_port=int(port))
        if result and result.get("refresh"):
            return result
        if progress:
            progress("Warte auf die Anmeldung im Web Reader …")
        time.sleep(2)
    if saw_reader:
        raise TolinoAuthError(
            "Zeitüberschreitung: keine Anmeldung erkannt. Bitte im "
            "Anmeldefenster anmelden, bis die Bücherliste lädt. "
            + RELOGIN_THEN, code="B04")
    raise TolinoAuthError(
        "Zeitüberschreitung: Die Anmeldung wurde nicht abgeschlossen. "
        + RELOGIN, code="B04", detail="kein Web-Reader-Tab")


def grab_live_tokens(partner_id, timeout=300, progress=None):
    """Open the grabber window and read the reader's CURRENT tokens.

    Returns the grabbed dict ``{"refresh": [...], "hardware": [...]}``;
    the token-endpoint exchange stays in tolino.py (single source of
    truth for the client swap, testable without a browser).
    """
    port = launch_reader_window(partner_id)
    if progress:
        progress("Anmeldefenster geöffnet – bitte anmelden und warten, "
                 "bis die Bücherliste lädt.")
    grabbed = collect_grab(port, timeout=timeout, progress=progress)
    if progress:
        progress("Zugang aus dem Web Reader gelesen – prüfe ihn jetzt "
                 "bei Tolino …")
    return grabbed
