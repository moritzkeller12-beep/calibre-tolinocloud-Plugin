"""Live-Token-Grab ueber das Chrome DevTools Protocol."""

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.parse import quote as _url_quote

from .partners import PARTNERS
from .tolino import RELOGIN, RELOGIN_THEN, TolinoAuthError
from .cdp_transport import (_Ws, _http_get_json, _ws_path_to_host_port)
from .cdp_scripts import _EXCHANGE_SNIPPET, _GRAB_SNIPPET, idb_expression


def _is_loopback_url(url):
    """Compatibility export; implementation lives in cdp_transport."""
    from .cdp_transport import _is_loopback_url as is_loopback_url
    return is_loopback_url(url)


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


def _clean_profile_dir():
    shutil.rmtree(_profile_dir(), ignore_errors=True)


# ---------------------------------------------------------- Besitz-Nachweis
#
# Ein Anmeldefenster gehoert nur dann dem Plugin, wenn ALLE drei Punkte
# zusammenpassen: eindeutiges Profil mit geheimem Marker, passender laufender
# Prozess (Linux /proc) und ein DevTools-Endpunkt genau dieses Prozesses.
# Ohne diesen Nachweis werden weder Tokens gelesen noch Browser.close gesendet.
_ACTIVE = {"port": None, "profile": "", "pid": None, "token": ""}
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
    return {"port": port, "profile": profile, "token": token,
            "pid": record.get("pid")}


def _marker_matches(record):
    try:
        with open(os.path.join(record["profile"], _OWNER_MARKER),
                  "r", encoding="utf-8") as handle:
            return handle.read().strip() == record["token"]
    except OSError:
        return False


# Browser main processes carry these names in /proc/<pid>/exe. The
# recorded PID can be a short-lived launcher (snap, flatpak, flatpak-spawn)
# that has already exited while the real browser keeps running with the
# same profile and debugging port.
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
    if not isinstance(record, dict) or not os.path.isdir("/proc"):
        return []
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError):
        return []
    profile = str(record.get("profile") or "")
    if port <= 0 or not profile:
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
        if (argv and any(profile in part for part in argv)
                and flag in argv and _is_browser_process(pid)):
            pids.append(pid)
    return pids


def _process_matches(record):
    """Verify that OUR profile is running on OUR debugging port.

    On platforms without /proc (Windows/macOS) the private profile marker and
    the persisted record remain the best available signal; no foreign local
    endpoint is ever selected by port alone.
    """
    if not os.path.isdir("/proc"):
        return True
    return bool(_owned_pids(record))


def _owner_alive(record):
    if not isinstance(record, dict):
        return False
    if not _marker_matches(record):
        return False
    if not _process_matches(record):
        return False
    return bool(_http_get_json(
        "http://127.0.0.1:%d/json/version" % record["port"], 2))


def _active_port():
    """Port of OUR running grabber window, or a fresh unused port."""
    if _ACTIVE.get("port"):
        return int(_ACTIVE["port"])
    record = _read_owner()
    if record and _owner_alive(record):
        _ACTIVE.update(record)
        return int(record["port"])
    return _start_port()


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


def devtools_port_alive(port=None):
    """True when OUR grabber window's DevTools endpoint answers right now."""
    if port is None:
        record = _read_owner()
        if not record or not _owner_alive(record):
            return False
        return True
    try:
        record = _read_owner()
    except Exception:
        record = None
    if not record or int(record.get("port") or 0) != int(port):
        return False
    return _owner_alive(record)


def _evaluate_raw_in_target(ws_url, expression, timeout=10,
                            await_promise=False):
    """Like _evaluate_in_target but returns CDP's full result dict.

    Returns ``{"ok": bool, "value": <decoded JSON value or raw string>,
    "exception": str}`` – callers that need the exceptionDetails (e.g.
    an in-page fetch whose failure text matters) use this variant.
    """
    try:
        path = ws_url[len("ws://"):]
        host, port, ws_path = _ws_path_to_host_port(path)
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


def _evaluate_in_target(ws_url, expression, timeout=10, await_promise=False):
    """Run a JS expression in a DevTools page target, return its JSON value."""
    raw = _evaluate_raw_in_target(ws_url, expression, timeout, await_promise)
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
    # Reuse ONLY our own window: ownership record + private marker + the
    # recorded process must still match. A foreign listener on a port is
    # never used (and never closed).
    previous = _read_owner()
    if previous and _owner_alive(previous):
        _ACTIVE.update(previous)
        return int(previous["port"])
    if previous and _marker_matches(previous):
        # Ein Fenster aus einem frueheren Versuch lebt noch, laesst sich
        # aber nicht mehr als «unser» im laufenden Prozess nachweisen.
        # Erst schliessen, statt sein Profil unter ihm wegzuloeschen --
        # sonst bleibt genau dieses Fenster dauerhaft offen.
        close_grabber_window()
    _clean_profile_dir()
    port = _start_port()
    profile = _new_profile_dir()
    token = secrets.token_hex(16)
    _write_marker(profile, token)
    record = {"port": port, "profile": profile, "pid": None,
              "token": token}
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
        _abandon_failed_launch(None)
        raise TolinoAuthError(
            "Chromium konnte nicht gestartet werden.", code="B07",
            detail="%s: %s" % (last_error[0], last_error[1]))
    record["pid"] = getattr(process, "pid", None)
    _write_owner(record)
    _ACTIVE.update(record)
    # Fuer den spaeteren Close: das Popen-Objekt beweist, dass es unser
    # Kind ist -- auch auf Windows/macOS ohne /proc.
    _LAUNCHED.update({"process": process, "port": port})
    # Wait until the DevTools endpoint answers.
    deadline = time.time() + 20
    while time.time() < deadline:
        if _http_get_json("http://127.0.0.1:%d/json/version" % port, 2):
            return port
        time.sleep(0.5)
    # Do not leave an orphan instance, profile or ownership record behind
    # on a failed start.
    _abandon_failed_launch(process)
    raise TolinoAuthError(
        "Chromium reagiert nicht. Bitte alle Chromium-Fenster schliessen "
        "und es erneut versuchen.", code="B07", detail="Port %d" % port)


def _stop_launched_process(process, timeout=2):
    """Terminate the browser we started and wait for it to disappear.

    Used only on the failed-start path. Without the wait the private profile
    could be removed while the process still had it open (and Windows keeps
    handles longer than POSIX). Failures are deliberately swallowed: the
    caller is already raising the useful startup error.
    """
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        process.terminate()
    except Exception:
        return
    try:
        process.wait(timeout=max(0.2, timeout))
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=1)
    except Exception:
        pass


def _abandon_failed_launch(process):
    """No orphan browser, ownership record or profile after a failed start."""
    _stop_launched_process(process)
    _ACTIVE.update({"port": None, "profile": "", "pid": None, "token": ""})
    _LAUNCHED.update({"process": None, "port": None})
    try:
        os.remove(_owner_file())
    except OSError:
        pass
    _clean_profile_dir()


def read_idb_tokens(ws_url, db_names, timeout=10):
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
                                     await_promise=True)
        if not result:
            continue
        for bucket in ("refresh", "hardware"):
            for value in result.get(bucket) or []:
                if value not in merged[bucket]:
                    merged[bucket].append(value)
    return merged


def _grab_page_tokens(ws_url, timeout=10):
    """ONE full storage grab: localStorage, sessionStorage, IndexedDB."""
    grabbed = _evaluate_in_target(ws_url, _GRAB_SNIPPET, timeout,
                                  await_promise=True) or {}
    for bucket in ("refresh", "hardware", "idb"):
        if not isinstance(grabbed.get(bucket), list):
            grabbed[bucket] = []
    idb_names = list(grabbed.get("idb"))
    if idb_names:
        idb_tokens = read_idb_tokens(ws_url, idb_names, timeout)
        for bucket in ("refresh", "hardware"):
            for value in idb_tokens.get(bucket) or []:
                if value not in grabbed[bucket]:
                    grabbed[bucket].append(value)
    return grabbed


def grab_once_from_grabber(port=None, timeout=12):
    """ONE grab attempt against OUR grabber window (no polling).

    Returns the grabbed dict (refresh/hardware/idb lists, possibly empty)
    or None when no reader tab exists or the endpoint is not provably ours.
    Used by the extract button so the Calibre GUI never blocks for minutes
    waiting for a sign-in that may not happen – the user just clicks the
    button again after signing in.
    """
    record = _read_owner()
    if port is None:
        if not record or not _owner_alive(record):
            return None
        port = int(record["port"])
    elif not record or int(record.get("port") or 0) != int(port) or not _owner_alive(record):
        return None
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
    for target in targets:
        if target.get("type") != "page":
            continue
        if "mytolino.com" not in str(target.get("url") or ""):
            continue
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url.startswith("ws://"):
            continue
        # idb-Behaeltnis bleibt erhalten: describe_grab_state nennt
        # sonst immer "IndexedDB-Datenbanken: keine".
        return _grab_page_tokens(ws_url, timeout)
    return None


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
    expression = (_EXCHANGE_SNIPPET
                  .replace("TOKEN_URL_PLACEHOLDER",
                           json.dumps(str(token_url)))
                  .replace("BODY_PLACEHOLDER", json.dumps(str(form_body))))
    raw = _evaluate_raw_in_target(ws_url, expression, timeout,
                                  await_promise=True)
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
    record = _read_owner()
    if port is None:
        if not record or not _owner_alive(record):
            return None
        port = int(record["port"])
    elif not record or int(record.get("port") or 0) != int(port) or not _owner_alive(record):
        return None
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
    for target in targets:
        if target.get("type") != "page":
            continue
        if "mytolino.com" not in str(target.get("url") or ""):
            continue
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if ws_url.startswith("ws://"):
            return ws_url
    return None


def _owned_browser_ws(port=None, timeout=2):
    """(host, port, path) of OUR browser endpoint, or None.

    The ownership record must match the requested port, the private profile
    marker, the recorded process and the DevTools endpoint belong to that
    same port. A foreign local listener can therefore never be selected by
    port alone.
    """
    try:
        record = _read_owner()
        if port is not None and (not record
                                 or int(record.get("port") or 0) != int(port)):
            return None
        if not record or not _owner_alive(record):
            return None
        version = _http_get_json(
            "http://127.0.0.1:%d/json/version" % record["port"], timeout)
        ws_url = str((version or {}).get("webSocketDebuggerUrl") or "")
        if not ws_url.startswith("ws://"):
            return None
        host, ws_port, ws_path = _ws_path_to_host_port(ws_url[len("ws://"):])
        if int(ws_port) != int(record["port"]):
            return None
        return host, ws_port, ws_path
    except Exception:
        return None


def _browser_ws_for_record(record, timeout=2):
    """Browser-WebSocket aus dem Besitzsatz, ohne /proc-Nachweis.

    Nur fuer das Schliessen nach bestandenem Marker-Nachweis: ein frueherer
    Versuch konnte `owner.json` entfernen, waehrend das Fenster noch lebte.
    """
    try:
        port = int((record or {}).get("port") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    if port <= 0:
        return None
    version = _http_get_json(
        "http://127.0.0.1:%d/json/version" % port, timeout)
    ws_url = str((version or {}).get("webSocketDebuggerUrl") or "")
    if not ws_url.startswith("ws://"):
        return None
    host, ws_port, ws_path = _ws_path_to_host_port(ws_url[len("ws://"):])
    if int(ws_port) != port:
        return None
    return host, ws_port, ws_path


def _send_browser_close(host, port, path, timeout=2):
    """Send Browser.close over an owned DevTools websocket (never raises)."""
    try:
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


# Der zuletzt von uns gestartete Browser als Popen-Objekt. Solange
# ``poll()`` None liefert, laeuft genau dieses Kind -- unabhaengig von
# /proc und ohne PID-Wiederverwendung.
_LAUNCHED = {"process": None, "port": None}


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
            if target.get("type") == "page"]


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

    Nur mit bestandenem Besitznachweis: das Profil traegt unseren Marker.
    """
    if not _marker_matches(record):
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
    """Unseren eigenen Startprozess beenden -- auch ohne /proc.

    Auf Windows und macOS gab es bisher keinen Fallback, wenn Chromium
    `Browser.close` ignorierte. Das Popen-Objekt beweist, dass es unser
    Kind ist; nach `poll()` ist keine fremde PID im Spiel.
    """
    process = _LAUNCHED.get("process")
    if process is None:
        return False
    try:
        if int(_LAUNCHED.get("port") or 0) != int((record or {}).get("port") or 0):
            return False
    except (TypeError, ValueError):
        return False
    try:
        if process.poll() is not None:
            return not _endpoint_answers(record, 0.4)
    except Exception:
        return False
    for stop in ("terminate", "kill"):
        try:
            getattr(process, stop)()
        except Exception:
            pass
        deadline = time.time() + max(0.2, timeout / 2.0)
        while time.time() < deadline:
            if not _endpoint_answers(record, 0.4):
                return True
            time.sleep(0.1)
    return not _endpoint_answers(record, 0.4)


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
            "token": _ACTIVE["token"], "pid": _ACTIVE.get("pid")}


def _endpoint_answers(record, timeout=0.5):
    """True while the owned DevTools endpoint still answers."""
    if not isinstance(record, dict):
        return False
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError):
        return False
    if port <= 0:
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
            return not _endpoint_answers(record, 0.4)
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
    target = _owned_browser_ws(port, timeout)
    if target is None and record is not None and _marker_matches(record):
        # Das Fenster ist ueber Profil und Marker belegt, der /proc- oder
        # Datei-Nachweis fehlt aber (etwa nach einem alten Fehlversuch).
        target = _browser_ws_for_record(record, timeout)
    closed = bool(target and _send_browser_close(
        target[0], target[1], target[2], timeout))
    if not closed:
        _close_log("Browser.close konnte nicht gesendet werden.")
    if record is not None and _endpoint_answers(record, 0.4):
        # Chromium hat Browser.close (noch) nicht befolgt: die offenen
        # Fenster direkt ueber den Debug-Endpunkt schliessen.
        _close_page_targets(record, timeout)
    terminated = False
    if record is not None:
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
    gone = terminated or (record is not None and _window_gone(record, 0.4))
    if not gone:
        _close_log("Fenster liess sich nicht schliessen; Besitznachweis "
                   "bleibt fuer einen weiteren Versuch erhalten.")
        return False
    _ACTIVE.update({"port": None, "profile": "", "pid": None, "token": ""})
    _LAUNCHED.update({"process": None, "port": None})
    try:
        os.remove(_owner_file())
    except OSError:
        pass
    # Das Chromium-Profil enthaelt die Anmeldesitzung (Cookies, IndexedDB
    # mit Token). Es darf nicht bis zum naechsten Start in /tmp liegen
    # bleiben. Gelöscht wird nur mit bestandenem Besitznachweis -- sonst
    # koennte ein fremdes Verzeichnis getroffen werden. Ein noch
    # laufender Browser behaelt sein Profil, bis er beendet ist.
    if record is not None and _marker_matches(record) and not _endpoint_answers(record, 0.2):
        shutil.rmtree(str(record["profile"]), ignore_errors=True)
    return True


def grabber_window_state(port=None, timeout=5):
    """Structured grabber-window state for the login wait loop.

    Returns ``{"window", "reader_tab", "login_page"}``:
    """
    port = port or _active_port()
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port, timeout)
    if not isinstance(targets, list):
        return {"window": False, "reader_tab": False, "login_page": False}
    reader_tab = False
    login_page = False
    for target in targets:
        if target.get("type") != "page":
            continue
        url = str(target.get("url") or "")
        if "mytolino.com" in url:
            reader_tab = True
            break
        if not url.startswith(("about:", "chrome:", "edge:",
                               "devtools://")):
            login_page = True
    return {"window": True, "reader_tab": reader_tab,
            "login_page": login_page}


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


def collect_grab(port, timeout=180, progress=None):
    """Poll the grabber window until the reader exposes the current token."""
    deadline = time.time() + timeout
    endpoint_misses = 0
    saw_reader = False
    while time.time() < deadline:
        targets = _http_get_json("http://127.0.0.1:%d/json/list" % port)
        if not isinstance(targets, list):
            # Endpoint antwortet nicht (Fenster weg oder kurz gestoert).
            # Erst mehrere Fehlschlaege in Folge sind ein geschlossenes
            # Fenster -- ein einzelner war der False-Positive.
            endpoint_misses += 1
            if endpoint_misses >= 3:
                if progress:
                    progress("Anmeldefenster geschlossen – abgebrochen.")
                raise TolinoAuthError(
                    "Das Anmeldefenster wurde geschlossen, bevor die "
                    "Übernahme fertig war. Bitte erneut versuchen und es "
                    "offen lassen.", code="B03")
            time.sleep(1)
            continue
        endpoint_misses = 0
        reader = None
        foreign_page = False
        for target in targets:
            if target.get("type") != "page":
                continue
            url = str(target.get("url") or "")
            if "mytolino.com" in url:
                reader = target
                break
            if not url.startswith(("about:", "chrome:", "edge:",
                                   "devtools://")):
                foreign_page = True
        if reader is None:
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
        ws_url = str(reader.get("webSocketDebuggerUrl") or "")
        if ws_url.startswith("ws://"):
            result = _grab_page_tokens(ws_url, 10)
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
