"""Cross-platform Chromium-family browser discovery for the Web Reader."""
import os
import shutil
import subprocess
import sys


def in_flatpak_sandbox():
    """True when this process runs inside a Flatpak sandbox."""
    return os.path.exists("/.flatpak-info")


def flatpak_permission_hint():
    """Commands to grant host-browser access to sandboxed Calibre."""
    return (
        "Calibre läuft als Flatpak und sieht die Browser des Rechners "
        "nicht. Einmalig freigeben und Calibre vollständig neu starten."
        "\n\nBenutzer-Installation:"
        "\nflatpak override --user --talk-name=org.freedesktop.Flatpak"
        " com.calibre_ebook.calibre"
        "\n\nSystem-Installation:"
        "\nsudo flatpak override --talk-name=org.freedesktop.Flatpak"
        " com.calibre_ebook.calibre"
        "\n\nPrüfen: flatpak info --show-permissions"
        " com.calibre_ebook.calibre")


def missing_browser_error(status=""):
    """Return user-facing details when no Chromium browser is available.

    ``status`` ist das Ergebnis der Host-Sonde aus ``find_browsers()``.
    """
    message = (
        "Kein Chromium-Browser gefunden. Bitte Chrome, Chromium, Brave oder "
        "Edge installieren – oder in den Einstellungen Zugangstoken und "
        "Geräte-ID eintragen.")
    if status == "unavailable" and in_flatpak_sandbox():
        return (
            "Calibre läuft als Flatpak, aber «flatpak-spawn» fehlt. "
            "Bitte «flatpak update» ausführen und Calibre danach "
            "vollständig neu starten.")
    if in_flatpak_sandbox() and status not in ("empty", "unavailable"):
        message += "\n\n" + flatpak_permission_hint()
    return message


# PATH-independent locations for desktop-launched Calibre.
_EXTRA_BROWSER_PATHS = (
    "/usr/lib/chromium/chromium",
    "/opt/google/chrome/chrome",
    "/snap/bin/chromium",
)

# Executable aliases shared by PATH and Flatpak host probing.
_BROWSER_COMMANDS = (
    "google-chrome", "google-chrome-stable", "google-chrome-beta", "chrome",
    "chromium", "chromium-browser", "brave-browser", "brave-browser-stable",
    "microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta",
    "microsoft-edge-dev", "vivaldi", "vivaldi-stable", "vivaldi-snapshot",
    "opera", "opera-stable",
)

# Keep the native Flatpak launcher and host probe mappings in one place.
_FLATPAK_BROWSER_APPS = (
    ("org.chromium.Chromium", "chrome"),
    ("com.brave.Browser", "brave"),
    ("com.google.Chrome", "chrome"),
    ("com.vivaldi.Vivaldi", "vivaldi"),
)


def _sandbox_host_launchers():
    """Discover host Chromium launchers; returns ``(launchers, status)``."""
    spawn = shutil.which("flatpak-spawn")
    if not spawn:
        return [], "unavailable"
    script = (
        "for b in HOST_COMMAND_LIST; do "
        "p=$(command -v \"$b\") && { echo \"NATIVE $p\"; break; }; done; "
        "for a in APP_ID_LIST; do "
        "command -v flatpak >/dev/null 2>&1 && flatpak info \"$a\" "
        ">/dev/null 2>&1 && echo \"FLATPAK $a\"; done"
        "; exit 0"
    ).replace("APP_ID_LIST", " ".join(
        app_id for app_id, _command in _FLATPAK_BROWSER_APPS)).replace(
            "HOST_COMMAND_LIST", " ".join(_BROWSER_COMMANDS))
    try:
        proc = subprocess.run([spawn, "--host", "sh", "-c", script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return [], ""
    output = proc.stdout or ""
    if proc.returncode != 0 and not output.strip():
        return [], "denied"
    commands = dict(_FLATPAK_BROWSER_APPS)
    launchers = []
    for line in output.splitlines():
        kind, _, value = line.partition(" ")
        value = value.strip()
        if not value:
            continue
        if kind == "NATIVE":
            launchers.append([spawn, "--host", value])
        elif kind == "FLATPAK" and value in commands:
            launchers.append([spawn, "--host", "flatpak", "run",
                              "--command=%s" % commands[value], value])
    return launchers, ("ok" if launchers else "empty")


def _collect_candidates(home=None):
    """(Kandidaten, Status der Host-Sonde) -- Status ist "" ohne Sonde.

    User-directory checks are used for Flatpak and Snap apps; host probing
    runs only from inside a Flatpak sandbox.
    """
    home = home or os.path.expanduser("~")
    flat = os.path.join(home, ".var", "app")
    snap = os.path.join(home, "snap")
    found = []
    for exe in _BROWSER_COMMANDS:
        path = shutil.which(exe)
        if path:
            found.append([path])
    for path in _EXTRA_BROWSER_PATHS:
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        if any(path == argv[0] for argv in found):
            continue
        found.append([path])
    flatpak_bin = shutil.which("flatpak")
    if flatpak_bin and os.path.isdir(flat):
        for app_id, command in _FLATPAK_BROWSER_APPS:
            if os.path.isdir(os.path.join(flat, app_id)):
                found.append([flatpak_bin, "run", "--command=%s" % command,
                              app_id])
    snap_bin = shutil.which("snap")
    if snap_bin and os.path.isdir(os.path.join(snap, "chromium")):
        found.append([snap_bin, "run", "chromium"])
    status = ""
    if in_flatpak_sandbox():
        launchers, status = _sandbox_host_launchers()
        found.extend(launchers)
    return found, status


# Windows registriert installierte Browser unter "App Paths".
_WINDOWS_BROWSER_EXECUTABLES = (
    "chrome.exe", "chromium.exe", "brave.exe", "msedge.exe",
    "vivaldi.exe", "opera.exe",
)

_DARWIN_BROWSERS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def _installed_under(roots, browsers):
    """Browser unter Standardpfaden; je relativem Pfad der erste Treffer."""
    found = []
    seen = set()
    for root in roots:
        for relative_path in browsers:
            if relative_path in seen:
                continue
            path = os.path.join(root, relative_path)
            if os.path.isfile(path):
                found.append([path])
                seen.add(relative_path)
    return found


def _windows_browser_paths(winreg_module=None):
    """Nur von Windows registrierte Browser: App-Paths, keine geratenen Ordner."""
    if winreg_module is None:
        try:
            import winreg as winreg_module
        except ImportError:
            return []
    app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    roots = (winreg_module.HKEY_CURRENT_USER,
             winreg_module.HKEY_LOCAL_MACHINE)
    views = tuple(dict.fromkeys((
        0, getattr(winreg_module, "KEY_WOW64_64KEY", 0),
        getattr(winreg_module, "KEY_WOW64_32KEY", 0))))
    found = []
    seen = set()
    for root in roots:
        for view in views:
            access = winreg_module.KEY_READ | view
            for executable in _WINDOWS_BROWSER_EXECUTABLES:
                try:
                    key = winreg_module.OpenKey(
                        root, app_paths + "\\" + executable, 0, access)
                except OSError:
                    continue
                try:
                    value, _kind = winreg_module.QueryValueEx(key, "")
                except OSError:
                    continue
                finally:
                    winreg_module.CloseKey(key)
                path = str(value or "").strip().strip('"')
                if not path:
                    continue
                path = os.path.expandvars(path)
                if not os.path.isfile(path):
                    continue
                normalized = os.path.normcase(os.path.abspath(path))
                if normalized in seen:
                    continue
                seen.add(normalized)
                found.append([path])
    return found


def _macos_browser_paths(home=None):
    """Browser in den macOS-Programmverzeichnissen (System und Benutzer)."""
    home = home or os.path.expanduser("~")
    return _installed_under(
        ("/Applications", os.path.join(home, "Applications")),
        _DARWIN_BROWSERS)


def _platform_browser_paths(platform=None):
    """Find browsers in the platform's standard installation directories."""
    platform = platform or sys.platform
    if platform == "win32":
        return _windows_browser_paths()
    if platform == "darwin":
        return _macos_browser_paths()
    return []


def find_browsers(home=None):
    """Alle Kandidaten inkl. Plattformpfade und Status der Host-Sonde."""
    found, status = _collect_candidates(home)
    return found + _platform_browser_paths(), status
