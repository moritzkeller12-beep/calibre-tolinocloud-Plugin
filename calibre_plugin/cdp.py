"""Live-Token-Grab ueber das Chrome DevTools Protocol."""

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from . import browser_finder
from .partners import WEBREADER_URL
from .tolino import TolinoAuthError, log_info
from .cdp_transport import _Ws, _http_get_json
from .cdp_scripts import _GRAB_SNIPPET, idb_expression


def _start_port():
    """Pick a fresh ephemeral loopback port for an owned grabber window."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# Der Besitz ist bewusst prozesslokal: nur Profil, Loopback-Port und
# Popen-Handle dieses Plugins stehen hier. Die Prozessgruppe wird beim Start
# festgehalten -- der Wrapper eines Flatpak-/Snap-Pakets kann vor seinem
# Browserkind enden, danach findet os.getpgid(pid) nichts mehr.
_LAUNCHED = {"process": None, "port": None, "profile": "", "pgid": None}


def _verified_owner(require_endpoint=True):
    """This process's browser record, by default only while it answers."""
    record = dict(_LAUNCHED)
    if not (record["process"] and record["port"] and record["profile"]):
        return None
    record["port"] = int(record["port"])
    if require_endpoint and not _endpoint_answers(record["port"]):
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = ws.recv_json(timeout=max(1, deadline - time.monotonic()))
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


# Sekunden, die ein einzelner Kandidat bekommt; ein laufender Browser
# meldet seinen DevTools-Port schon nach der ersten Abfrage.
_START_TIMEOUT = 12
# Ein Titel fuer alle Startfehler: die Details stehen in `detail`.
_BROWSER_NOT_POSSIBLE = "Browser-Anmeldung nicht möglich"
# Loopback-Endpunkt dieses Prozesses; nur der DevTools-Port variiert.
_VERSION_URL = "http://127.0.0.1:%d/json/version"


def _wait_for_debug_port(port, timeout, process):
    """Warte auf den DevTools-Port dieses Prozesses.

    Ein bereits beendeter Kandidat (etwa ein defektes snap-Paket) bricht
    sofort ab, statt den naechsten Kandidaten um die volle Wartezeit zu
    verzoegern.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_get_json(_VERSION_URL % port, 2):
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def launch_reader_window():
    """Start Chromium with an isolated temporary profile and random port."""
    previous = _verified_owner()
    if previous:
        return
    options, status = browser_finder.find_browsers()
    if not options:
        message = browser_finder.missing_browser_error(status)
        raise TolinoAuthError(message, code="B01",
                              short=_BROWSER_NOT_POSSIBLE,
                              detail="Host-Sonde: %s" % status)
    if _LAUNCHED["process"] is not None:
        close_grabber_window()
        if _LAUNCHED["process"] is not None:
            raise TolinoAuthError(
                "Das vorherige Anmeldefenster konnte nicht sicher "
                "geschlossen werden. Bitte schliessen Sie es und versuchen "
                "Sie es erneut.", code="B01", short=_BROWSER_NOT_POSSIBLE)

    port = _start_port()
    failures = []
    for argv in options:
        # Jeder Kandidat wird versucht: ein installiertes, aber nicht
        # startfaehiges Paket darf die Anmeldung nicht blockieren, solange
        # ein spaeterer Browser funktioniert.
        profile = tempfile.mkdtemp(prefix="tolino-grab-")
        try:
            process = subprocess.Popen(
                list(argv) + _reader_flags(port, profile),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # Eigene Prozessgruppe: ein Flatpak-Wrapper gibt sein
                # Browserkind sonst nicht mehr her, das Fenster bliebe offen.
                **({"start_new_session": True} if os.name == "posix"
                   else {"creationflags": 0x00000008}))
        except OSError as exc:
            shutil.rmtree(profile, ignore_errors=True)
            failures.append("%s: %s" % (argv[0], exc))
            continue
        _LAUNCHED.update({"process": process, "port": port,
                          "profile": profile,
                          "pgid": (process.pid if os.name == "posix"
                                   else None)})
        if _wait_for_debug_port(port, _START_TIMEOUT, process):
            # Der Port gehört ins Log: nur so lässt sich die CDP-Verbindung
            # von aussen prüfen (curl http://127.0.0.1:<Port>/json/list).
            log_info("Tolino-Anmeldefenster: Chromium auf 127.0.0.1:%d."
                     % port)
            return
        failures.append("%s: keine Antwort" % argv[0])
        if not _stop_launched_process(process, timeout=2,
                                      pgid=_LAUNCHED["pgid"], port=port):
            # Der Prozess lebt weiter und haelt den DevTools-Port; ein
            # weiterer Kandidat wuerde dessen Antwort als eigene lesen. Der
            # Besitzer bleibt vermerkt, damit ein erneutes Schliessen ihn
            # beenden kann; das Profil bleibt, solange der Browser es nutzt.
            raise TolinoAuthError(
                "Das Anmeldefenster liess sich nicht beenden. Bitte den "
                "Browser schliessen und es erneut versuchen.", code="B01",
                short=_BROWSER_NOT_POSSIBLE, detail="; ".join(failures))
        shutil.rmtree(profile, ignore_errors=True)
    _LAUNCHED.update({"process": None, "port": None, "profile": "",
                      "pgid": None})
    raise TolinoAuthError(
        "Chromium reagiert nicht. Bitte alle Chromium-Fenster schliessen "
        "und es erneut versuchen.", code="B01", short=_BROWSER_NOT_POSSIBLE,
        detail="; ".join(failures) or "kein Kandidat")


def _process_group(process, pgid=None):
    """Die eigene Prozessgruppe des Fensters, falls es eine hat.

    Gestartet wird mit ``start_new_session``; die pid des Wrappers ist dann
    die Gruppen-ID. Sie wird bevorzugt, weil die Gruppe auch dann noch
    laufen kann, wenn der Wrapper bereits beendet ist.
    """
    if os.name != "posix":
        return None
    if pgid:
        return int(pgid)
    pid = getattr(process, "pid", None)
    try:
        own_group = bool(pid) and os.getpgid(pid) == pid
    except OSError:
        own_group = False
    return pid if own_group else None


def _group_alive(group):
    """True, solange noch irgendein Prozess dieser Gruppe existiert."""
    if not group:
        return False
    try:
        os.killpg(int(group), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_until_settled(active, seconds):
    """Warten, bis `active` aufhoert -- hoechstens `seconds` lang."""
    deadline = time.monotonic() + max(0.2, seconds)
    while active() and time.monotonic() < deadline:
        time.sleep(0.1)


def _stop_launched_process(process, timeout=2, pgid=None, port=None):
    """Den gestarteten Browser samt Gruppe beenden; True, wenn er weg ist.

    Das Ende des Wrapper-Prozesses beweist nicht, dass das Browserkind weg
    ist. Deshalb zaehlt erst, wenn Wrapper, gemerkte Prozessgruppe UND
    DevTools-Port schweigen; die Gruppe wird dafuer immer beendet -- auch
    wenn der Wrapper schon fertig ist. Sonst bliebe ein Browserkind (oder
    der Port fuer den naechsten Kandidaten) am Leben.
    """
    group = _process_group(process, pgid)
    port = int(port or 0)

    def leftover():
        if process.poll() is None:
            return True
        if port and _endpoint_answers(port, 0.2):
            return True
        return _group_alive(group)

    for hard in (False, True):  # erst hoeflich, dann bestimmt.
        if not leftover():
            return True
        try:
            if group:
                os.killpg(group, signal.SIGKILL if hard else signal.SIGTERM)
            elif hard:
                process.kill()
            else:
                process.terminate()
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.2, timeout))
            except (OSError, subprocess.TimeoutExpired):
                pass
        if group and not hard:
            # Nach SIGTERM bekommt die Gruppe kurz Zeit, selbst zu enden.
            _wait_until_settled(lambda: _group_alive(group), timeout)
    if port:
        # Nach SIGKILL braucht der Port einen Moment, bis er geschlossen ist.
        _wait_until_settled(lambda: _endpoint_answers(port, 0.2), 0.3)
    if group:
        # Das Browserkind einer beendeten Gruppe wird kurz zum Zombie.
        _wait_until_settled(lambda: _group_alive(group), 0.3)
    return not leftover()


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


def _reader_targets(timeout=5):
    """Owned window's reader target, resolved in one place."""
    record = _verified_owner()
    if record is None:
        return None
    port = int(record["port"])
    targets = _http_get_json(
        "http://127.0.0.1:%d/json/list" % port, timeout)
    if not isinstance(targets, list):
        return None
    target, reader_found = _reader_target(targets, port)
    return {"window": True, "reader_tab": reader_found, "target": target}


def grab_once_from_grabber(timeout=12):
    """ONE grab attempt against OUR grabber window (no polling).

    Returns the grabbed dict (refresh/hardware/idb lists, possibly empty)
    or None when no reader tab exists or the endpoint is not provably ours.
    Wird vom Anmelde-Wartepfad genutzt, damit die Calibre-GUI nicht
    minutenlang auf eine Anmeldung wartet, die vielleicht nie kommt.
    """
    target = _reader_targets(timeout=max(0.1, min(5, timeout)))
    if target is None or target["target"] is None:
        return None
    return _grab_page_tokens(target["target"], timeout)


def _browser_ws_for_record(record, timeout=2):
    """Return the browser's CDP address, limited to its chosen port."""
    port = int(record.get("port") or 0)
    version = _http_get_json(_VERSION_URL % port, timeout)
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


def _endpoint_answers(port, timeout=0.5):
    """True while the selected loopback DevTools port answers."""
    port = int(port or 0)
    return (0 < port < 65536 and bool(_http_get_json(
        _VERSION_URL % port, timeout)))


def _shutdown(record, timeout=2):
    """Close a launched browser, then remove its temporary profile."""
    process = record["process"]
    port = int(record.get("port") or 0)
    if port and _endpoint_answers(port, timeout):
        target = _browser_ws_for_record(record, timeout)
        if target:
            _send_browser_close(*target, timeout=timeout)
        _wait_until_settled(lambda: _endpoint_answers(port, 0.2), timeout)
    if not _stop_launched_process(process, timeout,
                                  pgid=record.get("pgid"), port=port):
        return False
    _wait_until_settled(lambda: _endpoint_answers(port, 0.2), timeout)
    if _endpoint_answers(port, 0.2):
        # A wrapper may have exited before its browser child; never delete
        # a profile while DevTools can still be using it.
        return False
    shutil.rmtree(record["profile"], ignore_errors=True)
    if os.path.exists(record["profile"]):
        return False
    if _LAUNCHED["process"] is process:
        _LAUNCHED.update({"process": None, "port": None, "profile": "",
                          "pgid": None})
    return True


def close_grabber_window(timeout=2):
    """Close only the browser started by this module, then remove its profile."""
    record = _verified_owner(require_endpoint=False)
    if record is None:
        return False
    return _shutdown(record, timeout)


def grabber_window_state(timeout=5):
    """Return whether the grabber window and a reader tab are available."""
    target = _reader_targets(timeout)
    if target is None:
        return {"window": False, "reader_tab": False}
    return {"window": True, "reader_tab": target["reader_tab"]}
