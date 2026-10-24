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


def missing_browser_error():
    """Return user-facing details when no Chromium browser is available."""
    status = _host_probe_status
    message = (
        "Kein Chromium-Browser gefunden. Bitte Chrome, Chromium, Brave oder "
        "Edge installieren – oder den Zugangsschlüssel in den "
        "Einstellungen eintragen.")
    if status == "unavailable" and in_flatpak_sandbox():
        message = (
            "Calibre läuft als Flatpak, aber «flatpak-spawn» fehlt. "
            "Bitte «flatpak update» ausführen und Calibre danach "
            "vollständig neu starten.")
        return message, "B06", status
    if in_flatpak_sandbox() and status not in ("empty", "unavailable"):
        message += "\n\n" + flatpak_permission_hint()
        return message, "B08", status
    return message, "B05", status


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

# Host-probe state: not run/found, unavailable, denied, empty, or ok.
_host_probe_status = ""

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
    for prefix, label in (("google-chrome", "Google Chrome"),
                          ("chrome", "Google Chrome"),
                          ("chromium", "Chromium"),
                          ("brave", "Brave"),
                          ("microsoft-edge", "Edge"),
                          ("vivaldi", "Vivaldi"),
                          ("opera", "Opera")):
        if name.startswith(prefix):
            return label
    return name


def _sandbox_host_launchers():
    """Discover host Chromium launchers through flatpak-spawn."""
    global _host_probe_status
    spawn = shutil.which("flatpak-spawn")
    if not spawn:
        _host_probe_status = "unavailable"
        return []
    script = (
        "for b in google-chrome google-chrome-stable google-chrome-beta "
        "chrome chromium chromium-browser brave-browser "
        "brave-browser-stable microsoft-edge microsoft-edge-stable "
        "microsoft-edge-dev vivaldi vivaldi-stable opera; do "
        "p=$(command -v \"$b\") && { echo \"NATIVE $p\"; break; }; done; "
        "for a in APP_ID_LIST; do "
        "command -v flatpak >/dev/null 2>&1 && flatpak info \"$a\" "
        ">/dev/null 2>&1 && echo \"FLATPAK $a\"; done"
        "; exit 0"
    ).replace("APP_ID_LIST", " ".join(
        app_id for app_id, _command, _label, _host_label
        in _FLATPAK_BROWSER_APPS))
    try:
        proc = subprocess.run([spawn, "--host", "sh", "-c", script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    output = proc.stdout or ""
    if proc.returncode != 0 and not output.strip():
        _host_probe_status = "denied"
        return []
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
    _host_probe_status = "ok" if launchers else "empty"
    return launchers


def chromium_candidates(home=None):
    """Installed Chromium-family browsers, most common first.

    Returns ``(argv_prefix, label)`` pairs for native, Flatpak, and Snap
    installs. User-directory checks are used for Flatpak and Snap apps;
    host probing runs only from inside a Flatpak sandbox.
    """
    home = home or os.path.expanduser("~")
    flat = os.path.join(home, ".var", "app")
    snap = os.path.join(home, "snap")
    execs = [
        ("google-chrome", "Google Chrome"),
        ("google-chrome-stable", "Google Chrome"),
        ("google-chrome-beta", "Google Chrome"),
        ("chrome", "Google Chrome"),
        ("chromium", "Chromium"),
        ("chromium-browser", "Chromium"),
        ("brave-browser", "Brave"),
        ("brave-browser-stable", "Brave"),
        ("microsoft-edge", "Edge"),
        ("microsoft-edge-stable", "Edge"),
        ("microsoft-edge-beta", "Edge"),
        ("microsoft-edge-dev", "Edge"),
        ("vivaldi", "Vivaldi"),
        ("vivaldi-stable", "Vivaldi"),
        ("vivaldi-snapshot", "Vivaldi"),
        ("opera", "Opera"),
        ("opera-stable", "Opera"),
    ]
    found = []
    for exe, label in execs:
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
    if in_flatpak_sandbox():
        found.extend(_sandbox_host_launchers())
    return found


def _windows_chrome_paths():
    program_dirs = [
        os.environ.get("PROGRAMFILES") or "C:\\Program Files",
        os.environ.get("PROGRAMFILES(X86)") or "C:\\Program Files (x86)",
    ]
    rel_paths = [
        ("Google\\Chrome\\Application\\chrome.exe", "Google Chrome"),
        ("Chromium\\Application\\chrome.exe", "Chromium"),
        ("BraveSoftware\\Brave-Browser\\Application\\brave.exe", "Brave"),
        ("Microsoft\\Edge\\Application\\msedge.exe", "Edge"),
    ]
    found = []
    for base in program_dirs:
        for rel, label in rel_paths:
            path = os.path.join(base, rel)
            if os.path.isfile(path) and not any(label == f[1] for f in found):
                found.append(([path], label))
    return found


def _macos_chrome_paths():
    roots = ["/Applications", os.path.join(os.path.expanduser("~"),
                                           "Applications")]
    bundles = [
        ("Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
        ("Chromium.app/Contents/MacOS/Chromium", "Chromium"),
        ("Brave Browser.app/Contents/MacOS/Brave Browser", "Brave"),
        ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Edge"),
    ]
    found = []
    for base in roots:
        for rel, label in bundles:
            path = os.path.join(base, rel)
            if os.path.isfile(path) and not any(label == f[1] for f in found):
                found.append(([path], label))
    return found


def chromium_options():
    """All candidates, including Windows/macOS installation paths."""
    found = chromium_candidates()
    if sys.platform == "win32":
        found = found + _windows_chrome_paths()
    elif sys.platform == "darwin":
        found = found + _macos_chrome_paths()
    return found


def pick_chromium():
    """Return the best available Chromium-family candidate, or None."""
    found = chromium_options()
    return found[0] if found else None
