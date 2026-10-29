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

    ``status`` ist das Ergebnis der Host-Sonde aus ``pick_chromium()``.
    """
    message = (
        "Kein Chromium-Browser gefunden. Bitte Chrome, Chromium, Brave oder "
        "Edge installieren – oder den Zugangsschlüssel in den "
        "Einstellungen eintragen.")
    if status == "unavailable" and in_flatpak_sandbox():
        message = (
            "Calibre läuft als Flatpak, aber «flatpak-spawn» fehlt. "
            "Bitte «flatpak update» ausführen und Calibre danach "
            "vollständig neu starten.")
        return message, "B06"
    if in_flatpak_sandbox() and status not in ("empty", "unavailable"):
        message += "\n\n" + flatpak_permission_hint()
        return message, "B08"
    return message, "B05"


# PATH-independent locations for desktop-launched Calibre.
_EXTRA_BROWSER_PATHS = (
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/lib/chromium/chromium", "/usr/lib/chromium-browser/chromium-browser",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/usr/bin/brave-browser", "/usr/bin/brave-browser-stable",
    "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
    "/usr/bin/vivaldi", "/usr/bin/vivaldi-stable",
    "/usr/bin/opera", "/usr/bin/opera-stable", "/snap/bin/chromium",
)

# Executable aliases and display names shared by PATH and Flatpak host probing.
# The third field preserves the smaller command set previously probed on hosts.
_BROWSER_COMMANDS = (
    ("google-chrome", "Google Chrome", True),
    ("google-chrome-stable", "Google Chrome", True),
    ("google-chrome-beta", "Google Chrome", True),
    ("chrome", "Google Chrome", True),
    ("chromium", "Chromium", True),
    ("chromium-browser", "Chromium", True),
    ("brave-browser", "Brave", True),
    ("brave-browser-stable", "Brave", True),
    ("microsoft-edge", "Edge", True),
    ("microsoft-edge-stable", "Edge", True),
    ("microsoft-edge-beta", "Edge", False),
    ("microsoft-edge-dev", "Edge", True),
    ("vivaldi", "Vivaldi", True),
    ("vivaldi-stable", "Vivaldi", True),
    ("vivaldi-snapshot", "Vivaldi", False),
    ("opera", "Opera", True),
    ("opera-stable", "Opera", False),
)

# Keep the native Flatpak launcher and host probe mappings in one place.
_FLATPAK_BROWSER_APPS = (
    ("org.chromium.Chromium", "chrome", "Chromium (Flatpak)",
     "Chromium (Flatpak, Host)"),
    ("com.brave.Browser", "brave", "Brave (Flatpak)",
     "Brave (Flatpak, Host)"),
    ("com.google.Chrome", "chrome", "Chrome (Flatpak)",
     "Chrome (Flatpak, Host)"),
    ("com.vivaldi.Vivaldi", "vivaldi", "Vivaldi (Flatpak)",
     "Vivaldi (Flatpak, Host)"),
)


def _browser_label_for(path):
    """Display name for a browser executable path."""
    name = os.path.basename(str(path))
    for command, label, _probe_on_host in _BROWSER_COMMANDS:
        if name.startswith(command):
            return label
    return name


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
        app_id for app_id, _command, _label, _host_label
        in _FLATPAK_BROWSER_APPS)).replace(
            "HOST_COMMAND_LIST", " ".join(
                name for name, _label, probe_on_host in _BROWSER_COMMANDS
                if probe_on_host))
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
    commands = {app_id: (command, host_label)
                for app_id, command, _label, host_label
                in _FLATPAK_BROWSER_APPS}
    launchers = []
    for line in output.splitlines():
        kind, _, value = line.partition(" ")
        value = value.strip()
        if not value:
            continue
        if kind == "NATIVE":
            launchers.append(([spawn, "--host", value],
                              "%s (Host)" % os.path.basename(value)))
        elif kind == "FLATPAK" and value in commands:
            command, label = commands[value]
            launchers.append(([spawn, "--host", "flatpak", "run",
                               "--command=%s" % command, value], label))
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
    for exe, label, _probe_on_host in _BROWSER_COMMANDS:
        path = shutil.which(exe)
        if path:
            found.append(([path], label))
    for path in _EXTRA_BROWSER_PATHS:
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        if any(path == argv[0] for argv, _label in found):
            continue
        found.append(([path], _browser_label_for(path)))
    flatpak_bin = shutil.which("flatpak")
    if flatpak_bin and os.path.isdir(flat):
        for app_id, command, label, _host_label in _FLATPAK_BROWSER_APPS:
            if os.path.isdir(os.path.join(flat, app_id)):
                found.append(([flatpak_bin, "run", "--command=%s" % command,
                               app_id], label))
    snap_bin = shutil.which("snap")
    if snap_bin and os.path.isdir(os.path.join(snap, "chromium")):
        found.append(([snap_bin, "run", "chromium"], "Chromium (Snap)"))
    status = ""
    if in_flatpak_sandbox():
        launchers, status = _sandbox_host_launchers()
        found.extend(launchers)
    return found, status


_WINDOWS_BROWSERS = (
    ("Google\\Chrome\\Application\\chrome.exe", "Google Chrome"),
    ("Chromium\\Application\\chrome.exe", "Chromium"),
    ("BraveSoftware\\Brave-Browser\\Application\\brave.exe", "Brave"),
    ("Microsoft\\Edge\\Application\\msedge.exe", "Edge"),
)

_DARWIN_BROWSERS = (
    ("Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
    ("Chromium.app/Contents/MacOS/Chromium", "Chromium"),
    ("Brave Browser.app/Contents/MacOS/Brave Browser", "Brave"),
    ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Edge"),
)


def _installed_under(roots, browsers):
    """Browser unter Standardpfaden; je Anzeigename nur der erste Treffer."""
    found = []
    for root in roots:
        for relative_path, label in browsers:
            path = os.path.join(root, relative_path)
            if (os.path.isfile(path)
                    and not any(existing_label == label
                                for _argv, existing_label in found)):
                found.append(([path], label))
    return found


def _windows_browser_paths(environ=None):
    """Browser in den Windows-Programmverzeichnissen."""
    environ = os.environ if environ is None else environ
    roots = (environ.get("PROGRAMFILES") or r"C:\Program Files",
             environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)")
    return _installed_under(roots, _WINDOWS_BROWSERS)


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


def _options_with_status(home=None):
    """(alle Kandidaten inkl. Plattformpfade, Status der Host-Sonde)."""
    found, status = _collect_candidates(home)
    return found + _platform_browser_paths(), status


def chromium_candidates(home=None):
    """Installed Chromium-family browsers, most common first."""
    return _collect_candidates(home)[0]


def chromium_options():
    """All candidates, including standard Windows/macOS install paths."""
    return _options_with_status()[0]


def pick_chromium():
    """(bester Kandidat oder None, Status der Host-Sonde)."""
    options, status = _options_with_status()
    return (options[0] if options else None), status
