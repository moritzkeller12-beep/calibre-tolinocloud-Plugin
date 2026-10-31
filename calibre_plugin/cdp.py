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
from .tolino import TolinoAuthError
from .cdp_transport import _Ws, _http_get_json
from .cdp_scripts import _GRAB_SNIPPET, idb_expression


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


def _verified_owner(port=None, require_endpoint=True):
    """This process's browser record, by default only while it answers."""
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



def _open_target(target, timeout):
    """Eine DevTools-WebSocket-Verbindung zum eigenen Fenster öffnen.

    ``target`` ist bereits ``(Port, Pfad)``; der Host bleibt immer
    127.0.0.1. Damit wird keine selbst gebaute URL wieder zerlegt.
    """
    port, path = target
    return _Ws.connect("127.0.0.1", int(port), str(path), timeout=timeout)


def _evaluate_message(ws, expression, timeout, await_promise, message_id):
    """Eine Runtime.evaluate-Anfrage über eine bereits offene Verbindung.

    Rückgabe: ``{\"ok\": bool, \"value\": ..., \"exception\": ...}``.
    Der Aufrufer besitzt die Verbindung und schliesst sie. So braucht eine
    Reihe von Ausdrücken (z. B. alle IndexedDB-Datenbanken) nur EINEN
    WebSocket.
    """
    ws.send_json({"id": message_id, "method": "Runtime.evaluate",
                  "params": {"expression": expression,
                             "returnByValue": True,
                             "awaitPromise": bool(await_promise)}})
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = ws.recv_json(timeout=max(1, deadline - time.time()))
        if message.get("id") != message_id:
            continue
        if "error" in message:
            return {"ok": False, "value": None,
                    "exception": str(message.get("error"))[:200]}
        result = (message.get("result") or {}).get("result") or {}
        details = (message.get("result") or {}).get("exceptionDetails")
        if details:
            text = str((details.get("exception") or {}).get("description")
                       or details.get("text") or "page error")
            return {"ok": False, "value": None, "exception": text[:300]}
        if "value" in result:
            return {"ok": True, "value": result.get("value"),
                    "exception": None}
        return {"ok": False, "value": None, "exception": "no value returned"}
    return {"ok": False, "value": None, "exception": "evaluate timeout"}


def _evaluate_in_target(target, expression, timeout=10, await_promise=False):
    """Run a JS expression in a DevTools page target, return its JSON value."""
    try:
        ws = _open_target(target, timeout)
    except (OSError, ValueError):
        return None
    try:
        raw = _evaluate_message(ws, expression, timeout, await_promise, 1)
    except OSError:
        return None
    finally:
        ws.close()
    if not raw["ok"]:
        return None
    try:
        parsed = json.loads(raw["value"])
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------- Fenster-Flow

def _reader_flags(port, profile):
    """Chromium flags for the private reader window."""
    return [
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=%d" % port,
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble", "--disable-infobars",
        "--window-size=1280,900", "--new-window", WEBREADER_URL,
    ]


def _wait_for_debug_port(port, timeout=20):
    """Wait until Chromium's local CDP endpoint answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _http_get_json("http://127.0.0.1:%d/json/version" % port, 2):
            return True
        time.sleep(0.5)
    return False


def launch_reader_window():
    """Start Chromium with an isolated temporary profile and random port."""
    previous = _verified_owner()
    if previous:
        return previous["port"]
    candidate, status = pick_chromium()
    binary, label = candidate or (None, None)
    if not binary:
        message, code = browser_finder.missing_browser_error(status)
        raise TolinoAuthError(message, code=code,
                              detail="Host-Sonde: %s" % status)
    if _LAUNCHED["process"] is not None:
        close_grabber_window()
        if _LAUNCHED["process"] is not None:
            raise TolinoAuthError(
                "Das vorherige Anmeldefenster konnte nicht sicher "
                "geschlossen werden. Bitte schliessen Sie es und versuchen "
                "Sie es erneut.", code="B01")

    port = _start_port()
    profile = tempfile.mkdtemp(prefix="tolino-grab-")
    flags = _reader_flags(port, profile)
    creationflags = 0x00000008 if os.name == "nt" else 0
    def candidates():
        """Erster Kandidat, danach die Alternativen nur bei Bedarf."""
        yield list(binary), label
        for argv, option_label in browser_finder.chromium_options():
            if list(argv) != list(binary):
                yield list(argv), option_label

    last_error = None
    process = None
    for argv, candidate_label in candidates():
        try:
            process = subprocess.Popen(
                list(argv) + flags, creationflags=creationflags,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except OSError as exc:
            last_error = (candidate_label, exc)
    if process is None:
        shutil.rmtree(profile, ignore_errors=True)
        raise TolinoAuthError(
            "Chromium konnte nicht gestartet werden.", code="B01",
            detail="%s: %s" % last_error)

    _LAUNCHED.update({"process": process, "port": port, "profile": profile})
    # Der Port gehört ins Log: nur so lässt sich die CDP-Verbindung von
    # aussen prüfen (curl http://127.0.0.1:<Port>/json/list).
    from .tolino import log_info
    log_info("Tolino-Anmeldefenster: Chromium auf 127.0.0.1:%d." % port)
    if _wait_for_debug_port(port):
        return port
    _shutdown({"process": process, "port": port, "profile": profile})
    raise TolinoAuthError(
        "Chromium reagiert nicht. Bitte alle Chromium-Fenster schliessen "
        "und es erneut versuchen.", code="B01", detail="Port %d" % port)


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


def read_idb_tokens(target, db_names, timeout=10):
    """Walk every IndexedDB database's object stores for token values.

    Returns the merged ``{"refresh": [...], "hardware": [...]}`` dict;
    databases that fail to open are skipped (the localStorage/session
    scan already ran in the main grab).
    """
    merged = {"refresh": [], "hardware": []}
    names = [name for name in db_names or [] if str(name or "").strip()]
    if not names:
        return merged
    try:
        ws = _open_target(target, timeout)
    except (OSError, ValueError):
        return merged
    try:
        for index, name in enumerate(names, 1):
            result = _evaluate_message(ws, idb_expression(name), timeout,
                                       True, index)
            if not result.get("ok"):
                continue
            try:
                parsed = json.loads(result.get("value"))
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            for bucket in ("refresh", "hardware"):
                for value in parsed.get(bucket) or ():
                    if value not in merged[bucket]:
                        merged[bucket].append(value)
    except OSError:
        return merged
    finally:
        ws.close()
    return merged


def _grab_page_tokens(target, timeout=10):
    """ONE full storage grab: localStorage, sessionStorage, IndexedDB."""
    grabbed = _evaluate_in_target(target, _GRAB_SNIPPET, timeout,
                                  await_promise=True) or {}
    for bucket in ("refresh", "hardware", "idb"):
        if not isinstance(grabbed.get(bucket), list):
            grabbed[bucket] = []
    idb_names = list(grabbed.get("idb"))
    if idb_names:
        idb_tokens = read_idb_tokens(target, idb_names, timeout)
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


def _reader_target(targets, port):
    """Return the reader tab's ``(port, path)`` and whether one exists.

    Die Adresse wird aus Port und Target-id gebaut: für einen selbst
    gestarteten Browser meldet Chrome genau
    ws://127.0.0.1:<Port>/devtools/page/<id>. Ohne Target-id gibt es
    keinen sicheren Verbindungsweg; die gemeldete URL wird nicht mehr
    ungeprueft uebernommen.
    """
    if not isinstance(targets, list):
        return None, False
    reader_found = False
    for target in targets:
        if (not isinstance(target, dict) or target.get("type") != "page"
                or not _is_reader_url(target.get("url"))):
            continue
        reader_found = True
        target_id = str(target.get("id") or "").strip()
        if target_id:
            return (int(port), "/devtools/page/%s" % target_id), True
    return None, reader_found


def _reader_targets(port=None, timeout=5):
    """Owned window's reader target, resolved in one place."""
    record = _verified_owner(port)
    if record is None:
        return None
    port = int(record["port"])
    targets = _http_get_json(
        "http://127.0.0.1:%d/json/list" % port, timeout)
    if not isinstance(targets, list):
        return None
    target, reader_found = _reader_target(targets, port)
    return {"window": True, "reader_tab": reader_found, "target": target}


def grab_once_from_grabber(port=None, timeout=12):
    """ONE grab attempt against OUR grabber window (no polling).

    Returns the grabbed dict (refresh/hardware/idb lists, possibly empty)
    or None when no reader tab exists or the endpoint is not provably ours.
    Used by the extract button so the Calibre GUI never blocks for minutes
    waiting for a sign-in that may not happen – the user just clicks the
    button again after signing in.
    """
    target = _reader_targets(port, timeout=max(0.1, min(5, timeout)))
    if target is None or target["target"] is None:
        return None
    return _grab_page_tokens(target["target"], timeout)


def _browser_ws_for_record(record, timeout=2):
    """Return the browser's CDP address, limited to its chosen port."""
    port = int(record.get("port") or 0)
    version = _http_get_json(
        "http://127.0.0.1:%d/json/version" % port, timeout)
    ws_url = str((version or {}).get("webSocketDebuggerUrl") or "")
    prefix = "ws://127.0.0.1:%d" % port
    if not ws_url.startswith(prefix + "/devtools/"):
        return None
    return port, ws_url[len(prefix):]


def _send_browser_close(port, path, timeout=2):
    """Send Browser.close to the loopback DevTools endpoint on our port."""
    try:
        ws = _Ws.connect("127.0.0.1", int(port), path, timeout=timeout)
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


def _wait_for_endpoint_exit(record, seconds):
    """Sleep in small steps while the DevTools endpoint keeps answering."""
    if not _endpoint_answers(record, 0.2):
        return
    deadline = time.time() + max(0.2, seconds)
    while _endpoint_answers(record, 0.2) and time.time() < deadline:
        time.sleep(0.1)


def _shutdown(record, timeout=2):
    """Close a launched browser, then remove its temporary profile."""
    process = record["process"]
    port = int(record.get("port") or 0)
    if port and _endpoint_answers(record, timeout):
        target = _browser_ws_for_record(record, timeout)
        if target:
            _send_browser_close(*target, timeout=timeout)
        _wait_for_endpoint_exit(record, timeout)
    if not _stop_launched_process(process, timeout):
        return False
    _wait_for_endpoint_exit(record, timeout)
    if _endpoint_answers(record, 0.2):
        # A wrapper may have exited before its browser child; never delete
        # a profile while DevTools can still be using it.
        return False
    shutil.rmtree(record["profile"], ignore_errors=True)
    if os.path.exists(record["profile"]):
        return False
    if _LAUNCHED["process"] is process:
        _LAUNCHED.update({"process": None, "port": None, "profile": ""})
    return True


def close_grabber_window(timeout=2, port=None):
    """Close only the browser started by this module, then remove its profile."""
    record = _verified_owner(port, require_endpoint=False)
    if record is None:
        return False
    return _shutdown(record, timeout)


def grabber_window_state(port=None, timeout=5):
    """Return whether the grabber window and a reader tab are available."""
    target = _reader_targets(port, timeout)
    if target is None:
        return {"window": False, "reader_tab": False}
    return {"window": True, "reader_tab": target["reader_tab"]}
