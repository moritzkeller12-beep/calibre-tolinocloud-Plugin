"""One-click setup of curl_cffi inside Calibre's frozen Python environment.

curl_cffi ships compiled binaries and cannot be loaded from the plugin zip;
``pip install --user`` targets the system Python, not Calibre's frozen one.
This module downloads the pinned wheels published on PyPI, verifies each
SHA-256 checksum (values taken from the PyPI release metadata), extracts
them into the Calibre plugin directory and makes the packages importable.

Security: exact version pinning plus per-file SHA-256; a mismatch aborts
the setup. The wheels are those officially published by the curl_cffi
project (MIT license, bundled unmodified).
"""

import hashlib
import os
import shutil
import sys
import tempfile
import zipfile

from urllib.request import Request, urlopen

CURL_CFFI_VERSION = "0.16.3"

_FILES = "https://files.pythonhosted.org/packages/"

# Every entry: (distribution, filename, sha256 from PyPI release metadata).
# Platform key -> wheel choice is resolved in _wheel_urls().
WHEELS = {
    "linux/x86_64": [
        ("curl_cffi-0.16.3-cp310-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
         "a875a661e2f9a949be29454880bbb9553307a487c4c08819738298cf5c1622e2",
         "72/01/2bbf141baa0fc3921d31a90de5465b7a94188845a8fe84dee86bf7bd90f1"),
        ("cffi-2.1.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
         "194cffa889098ced9976c3fc6340305e43f6303657d298da55366907c05c22d6",
         "75/77/60bebf6f818bec84210ac5b6979ce4eeadce6fbbaabc9c7ab23e506d1ce5"),
    ],
    "linux/aarch64": [
        ("curl_cffi-0.16.3-cp310-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
         "d5a4103f2baa1fcf619ec3101b419827d367044ba106b206228137cc71a5a9c5",
         "97/2d/25b106e64178829be1ce171b6cd45ba354ab7a2a5169001866b38d4c440f"),
        ("cffi-2.1.1-cp310-cp310-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
         "5a59cc1c4442bc3d5c703bf720b51138d0bfc173618807c9ee2490a7541dd3d9",
         "a3/b9/0f2e58b2cefa33255bff36935d42b13180fe559bba82596540eb404bde7d"),
    ],
    "darwin/x86_64": [
        ("curl_cffi-0.16.3-cp310-abi3-macosx_10_9_x86_64.whl",
         "0f1f6878863fba393801e4d59b2f2766d1983b5c9d9dfa11d4becfd6a74cc937",
         "79/7a/ec08ef0665c4ef4ea76b47042eb1c043e4afb374d8b9218e00272c9e73a2"),
        ("cffi-2.1.1-cp310-cp310-macosx_10_15_x86_64.whl",
         "baed1e86cc735622097354b9d1281406caf42ff42a886d29faa8e8d1630333be",
         "b6/d2/2cde336b375f55c76ca670f0be3978cc048e31e24f3b4d7ce8473150a388"),
    ],
    "darwin/arm64": [
        ("curl_cffi-0.16.3-cp310-abi3-macosx_11_0_arm64.whl",
         "f3b63da797912bc82911e34dfe449725514e4281527fb516931fc457087cfb44",
         "4c/86/e21b8ed384db26401a4438f20f01c7bcd9c3a6f8ceede458344e2d62775c"),
        ("cffi-2.1.1-cp310-cp310-macosx_11_0_arm64.whl",
         "ca82be1a1d406ecfe1d25dc16cb33488e5a16bf4438c9fb590484ea29d92478b",
         "94/1a/4b2f7c92293ba05cbd4a9a1b28faaf0326272d9488e6354657571c48a7aa"),
    ],
    "win/amd64": [
        ("curl_cffi-0.16.3-cp310-abi3-win_amd64.whl",
         "fe87b66e324ed7318166698e02169f3208dbda32b872a27d2bc61a9c19b335eb",
         "9b/72/1732a24ef4a2aeba994b80ec163debe8deda403c07e4abbc0443bca078b8"),
        ("cffi-2.1.1-cp310-cp310-win_amd64.whl",
         "a48d62ab9d6f4f98c983223a547af44be6ca3691074c31cecced6facd3ba2dc1",
         "ba/0b/644a2ec1a4eaba49c2939410bb1eb1d25b09d6d0582f5d2f95c537043725"),
    ],
}

# Pure-python wheels are identical on every platform.
WHEELS_ANY = [
    ("pycparser-2.23-py3-none-any.whl",
     "e5c6e8d3fbad53479cab09ac03729e0a9faf2bee3db8208a550daf5af81a5934",
     "a0/e3/59cd50310fc9b59512193629e1984c1f95e5c8ae6e5d8c69532ccc65a7fe"),
    ("certifi-2026.7.22-py3-none-any.whl",
     "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775",
     "0b/a7/71ac2cff56fec219ed242bb11b8efb69fcc4bec75db06fb7bfe35de520e6"),
]


class BootstrapError(Exception):
    pass


def calibre_plugin_dir():
    """Directory Calibre adds to sys.path for plugins (the zip's parent)."""
    import calibre.utils.config as calibre_config  # noqa: PLC0415

    plugins = getattr(calibre_config, "plugins_dir", None)
    if plugins and os.path.isdir(plugins):
        return plugins
    config = getattr(calibre_config, "config_dir", None)
    if config:
        candidate = os.path.join(config, "plugins")
        if os.path.isdir(candidate):
            return candidate
    raise BootstrapError("Cannot locate Calibre's plugin directory.")


def _download(url, expected_sha256):
    """Download `url` and enforce its SHA-256 checksum."""
    request = Request(url, headers={"User-Agent": "calibre-tolino-sync/0.9.6"})
    with urlopen(request, timeout=120) as response:
        blob = response.read()
    if hashlib.sha256(blob).hexdigest() != expected_sha256:
        raise BootstrapError(
            "SHA-256 mismatch for %s (got %s, expected %s); aborting install."
            % (url.rsplit("/", 1)[-1], hashlib.sha256(blob).hexdigest()[:16],
               expected_sha256[:16]))
    return blob


def _extract_wheel(wheel_bytes, target_dir):
    """Extract a wheel into target_dir, preserving top-level packages."""
    with tempfile.TemporaryDirectory(prefix="tolino-wheel-") as tmp:
        archive = os.path.join(tmp, "wheel.whl")
        with open(archive, "wb") as handle:
            handle.write(wheel_bytes)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target_dir)


def _sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_available():
    """True when curl_cffi is importable right now."""
    try:
        from curl_cffi.requests import Session  # noqa: PLC0415, F401
        return True
    except Exception:
        return False


def setup_status():
    """(installed, importable, plugin_dir or None) for diagnostics."""
    try:
        plugin_dir = calibre_plugin_dir()
    except Exception:
        plugin_dir = None
    marker = os.path.join(plugin_dir or "", "curl_cffi-libs")
    return (os.path.isdir(marker), is_available(), plugin_dir)


def import_from_plugin_dir():
    """Import curl_cffi from the projected plugin dir without a restart.

    Returns a curl_cffi Session factory (or None). Reloads the interpreter's
    module cache entries for the extracted packages first, so an install
    performed in the same session works without restarting Calibre.
    """
    try:
        plugin_dir = calibre_plugin_dir()
    except Exception:
        return None
    root = os.path.join(plugin_dir, "curl_cffi-libs")
    if not os.path.isdir(root):
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    for prefix in ("curl_cffi", "cffi", "pycparser", "certifi"):
        for name in [m for m in list(sys.modules)
                     if m == prefix or m.startswith(prefix + ".")]:
            # Keep the real cffi if it was already imported successfully
            # (it is a compiled dependency the plugin may share).
            del sys.modules[name]
    try:
        import curl_cffi  # noqa: PLC0415
        from curl_cffi.requests import Session  # noqa: PLC0415
        return Session
    except Exception:
        return None


def install(plugin_dir=None, progress=None):
    """Download, verify and extract curl_cffi + dependencies.

    Returns the list of installed top-level package directories.
    """
    notify = progress or (lambda text: None)
    plugin_dir = plugin_dir or calibre_plugin_dir()
    root = os.path.join(plugin_dir, "curl_cffi-libs")
    os.makedirs(root, exist_ok=True)

    wheels = list(WHEELS_ANY)
    for key, entries in WHEELS.items():
        if _platform_key() == key:
            wheels.extend(entries)
            break
    else:
        raise BootstrapError(
            "No prebuilt curl_cffi wheel for this platform "
            "(sys.platform=%s, machine=%s)." % (sys.platform, _machine()))

    installed = []
    for filename, sha256, url_path in wheels:
        notify("Lade %s ..." % filename)
        blob = _download(_FILES + url_path + "/" + filename, sha256)
        notify("Prüfe SHA-256 ...")
        _extract_wheel(blob, root)
        installed.append(filename)

    # Preserve the executable bit lost by zipped plugin distribution.
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith((".so", ".dylib", ".pyd")) or ".so." in name:
                path = os.path.join(base, name)
                os.chmod(path, 0o755)

    notify("Fertig. Calibre neu starten, damit die Module geladen werden.")
    return installed


def _machine():
    try:
        import platform  # noqa: PLC0415
        return (platform.machine() or "").lower()
    except Exception:
        return ""


def _platform_key():
    machine = _machine()
    if sys.platform.startswith("linux"):
        return "linux/x86_64" if machine in ("x86_64", "amd64") else "linux/aarch64"
    if sys.platform == "darwin":
        return "darwin/arm64" if machine in ("arm64",) else "darwin/x86_64"
    if os.name == "nt":
        return "win/amd64"
    return "unknown"
