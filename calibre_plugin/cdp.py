"""Live-Token-Grab ueber das Chrome DevTools Protocol."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from .partners import WEBREADER_URL
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


# Ownership is deliberately process-local: this module records only the
# ephemeral profile, loopback port and Popen handle created by this plugin.
_LAUNCHED = {"process": None, "port": None, "profile": ""}


def _launched_record(port=None, require_endpoint=True):
    process = _LAUNCHED["process"]
    actual_port = _LAUNCHED["port"]
    profile = _LAUNCHED["profile"]
    if process is None or not actual_port or not profile:
        return None
    if port is not None and int(port) != int(actual_port):
        return None
    record = {"process": process, "port": int(actual_port),
              "profile": profile}
    if require_endpoint and not _endpoint_answers(record):
        return None
    return record


def _verified_owner(port=None):
    """Return this process's browser only while its chosen port answers."""
    return _launched_record(port)


def _new_profile_dir():
    return tempfile.mkdtemp(prefix="tolino-grab-")



def devtools_port_alive():
    """Whether the browser launched by this plugin still answers."""
    return _verified_owner() is not None


def _devtools_ws_address(ws_url, expected_port):
    """Use the launched loopback port; never connect to a URL-supplied host."""
    try:
        parsed = urlsplit(str(ws_url or ""))
        port = parsed.port
        if (parsed.scheme != "ws"
                or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or expected_port is None or port != int(expected_port)
                or not parsed.path.startswith("/devtools/")):
            raise ValueError("DevTools URL does not match the launched browser")
        return "127.0.0.1", port, parsed.path
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
    """Start Chromium with an isolated temporary profile and random port."""
    binary, label = pick_chromium() or (None, None)
    if not binary:
        message, code, status = browser_finder.missing_browser_error()
        raise TolinoAuthError(message, code=code,
                              detail="Host-Sonde: %s" % status)
    previous = _verified_owner()
    if previous:
        return previous["port"]
    if _LAUNCHED["process"] is not None:
        close_grabber_window()
        if _LAUNCHED["process"] is not None:
            raise TolinoAuthError(
                "Das vorherige Anmeldefenster konnte nicht sicher "
                "geschlossen werden. Bitte schliessen Sie es und versuchen "
                "Sie es erneut.", code="B07")

    port = _start_port()
    profile = _new_profile_dir()
    flags = [
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=%d" % port,
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble", "--disable-infobars",
        "--window-size=1280,900", "--new-window", WEBREADER_URL,
    ]
    creationflags = 0x00000008 if os.name == "nt" else 0
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
                attempts.extend(
                    candidate for candidate in browser_finder.chromium_options()
                    if candidate[0] != binary)
    else:
        shutil.rmtree(profile, ignore_errors=True)
        raise TolinoAuthError(
            "Chromium konnte nicht gestartet werden.", code="B07",
            detail="%s: %s" % last_error)

    _LAUNCHED.update({"process": process, "port": port, "profile": profile})
    deadline = time.time() + 20
    while time.time() < deadline:
        if _http_get_json("http://127.0.0.1:%d/json/version" % port, 2):
            return port
        time.sleep(0.5)
    _abandon_failed_launch(process, {"profile": profile})
    raise TolinoAuthError(
        "Chromium reagiert nicht. Bitte alle Chromium-Fenster schliessen "
        "und es erneut versuchen.", code="B07", detail="Port %d" % port)


def _stop_launched_process(process, timeout=2):
    if process is None:
        return True
    try:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=max(0.2, timeout))
        return process.poll() is not None
    except Exception:
        return False


def _abandon_failed_launch(process, record):
    """Close the launched browser before removing its temporary profile."""
    port = _LAUNCHED["port"]
    if port:
        live = {"process": process, "port": int(port),
                "profile": record["profile"]}
        target = _browser_ws_for_record(live)
        if target:
            _send_browser_close(*target, expected_port=port)
    if not _stop_launched_process(process):
        return False
    if port and _endpoint_answers({"port": port}, 0.2):
        deadline = time.time() + 2
        while _endpoint_answers({"port": port}, 0.2) and time.time() < deadline:
            time.sleep(0.1)
        if _endpoint_answers({"port": port}, 0.2):
            return False
    shutil.rmtree(record["profile"], ignore_errors=True)
    if os.path.exists(record["profile"]):
        return False
    if _LAUNCHED["process"] is process:
        _LAUNCHED.update({"process": None, "port": None, "profile": ""})
    return True


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
    """Return the browser's CDP address, limited to its chosen port."""
    port = int(record.get("port") or 0)
    version = _http_get_json(
        "http://127.0.0.1:%d/json/version" % port, timeout)
    ws_url = str((version or {}).get("webSocketDebuggerUrl") or "")
    try:
        return _devtools_ws_address(ws_url, port)
    except ValueError:
        return None


def _send_browser_close(host, port, path, timeout=2, expected_port=None):
    """Send Browser.close to a loopback DevTools endpoint on our port."""
    expected_port = port if expected_port is None else expected_port
    try:
        host_literal = "[%s]" % host if ":" in host else host
        host, port, path = _devtools_ws_address(
            "ws://%s:%s%s" % (host_literal, port, path), expected_port)
        ws = _Ws.connect(host, port, path, timeout=timeout)
        try:
            ws.send_json({"id": 1, "method": "Browser.close"})
            try:
                ws.recv_json(timeout=timeout)
            except OSError:
                pass
        finally:
            ws.close()
        return True
    except Exception:
        return False


def _endpoint_answers(record, timeout=0.5):
    """True while the selected loopback DevTools port answers."""
    port = int(record.get("port") or 0)
    return (0 < port < 65536 and bool(_http_get_json(
        "http://127.0.0.1:%d/json/version" % port, timeout)))


def close_grabber_window(timeout=2, port=None):
    """Close only the browser started by this module, then remove its profile."""
    record = _launched_record(port, require_endpoint=False)
    if record is None:
        return False
    if _endpoint_answers(record, timeout):
        target = _browser_ws_for_record(record, timeout)
        if target:
            _send_browser_close(*target, timeout=timeout,
                                expected_port=record["port"])
        deadline = time.time() + max(0.2, timeout)
        while _endpoint_answers(record, 0.2) and time.time() < deadline:
            time.sleep(0.1)
    if not _stop_launched_process(record["process"], timeout):
        return False
    deadline = time.time() + max(0.2, timeout)
    while _endpoint_answers(record, 0.2) and time.time() < deadline:
        time.sleep(0.1)
    if _endpoint_answers(record, 0.2):
        # A wrapper may have exited before its browser child; never delete
        # a profile while DevTools can still be using it.
        return False
    shutil.rmtree(record["profile"], ignore_errors=True)
    if os.path.exists(record["profile"]):
        return False
    if _LAUNCHED["process"] is record["process"]:
        _LAUNCHED.update({"process": None, "port": None, "profile": ""})
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
