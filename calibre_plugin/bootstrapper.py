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

# The curl_cffi wheel is built against the stable ABI (cp310-abi3) and works
# on every CPython >= 3.10 regardless of the exact interpreter version. The
# cffi companion wheel is CPython-version specific, so its tag is resolved
# from the running interpreter (_cffi_tag). Every entry is
# (filename, sha256 from the PyPI release metadata, full path below
# files.pythonhosted.org/packages/); every hash was verified by download.
CURL_CFFI_WHEELS = {
    "linux/x86_64":                     ("curl_cffi-0.16.3-cp310-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                     "a875a661e2f9a949be29454880bbb9553307a487c4c08819738298cf5c1622e2",
                     "72/01/2bbf141baa0fc3921d31a90de5465b7a94188845a8fe84dee86bf7bd90f1/curl_cffi-0.16.3-cp310-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
    "linux/aarch64":                      ("curl_cffi-0.16.3-cp310-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
                      "d5a4103f2baa1fcf619ec3101b419827d367044ba106b206228137cc71a5a9c5",
                      "97/2d/25b106e64178829be1ce171b6cd45ba354ab7a2a5169001866b38d4c440f/curl_cffi-0.16.3-cp310-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
    "darwin/x86_64":                      ("curl_cffi-0.16.3-cp310-abi3-macosx_10_9_x86_64.whl",
                      "0f1f6878863fba393801e4d59b2f2766d1983b5c9d9dfa11d4becfd6a74cc937",
                      "79/7a/ec08ef0665c4ef4ea76b47042eb1c043e4afb374d8b9218e00272c9e73a2/curl_cffi-0.16.3-cp310-abi3-macosx_10_9_x86_64.whl"),
    "darwin/arm64":                     ("curl_cffi-0.16.3-cp310-abi3-macosx_11_0_arm64.whl",
                     "f3b63da797912bc82911e34dfe449725514e4281527fb516931fc457087cfb44",
                     "4c/86/e21b8ed384db26401a4438f20f01c7bcd9c3a6f8ceede458344e2d62775c/curl_cffi-0.16.3-cp310-abi3-macosx_11_0_arm64.whl"),
    "win/amd64":                  ("curl_cffi-0.16.3-cp310-abi3-win_amd64.whl",
                  "fe87b66e324ed7318166698e02169f3208dbda32b872a27d2bc61a9c19b335eb",
                  "9b/72/1732a24ef4a2aeba994b80ec163debe8deda403c07e4abbc0443bca078b8/curl_cffi-0.16.3-cp310-abi3-win_amd64.whl"),
}

# cffi 2.1.1 per CPython ABI tag and platform.
CFFI_WHEELS = {
    ("cp310", "linux/x86_64"):                              ("cffi-2.1.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                              "194cffa889098ced9976c3fc6340305e43f6303657d298da55366907c05c22d6",
                              "75/77/60bebf6f818bec84210ac5b6979ce4eeadce6fbbaabc9c7ab23e506d1ce5/cffi-2.1.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
    ("cp311", "linux/x86_64"):                              ("cffi-2.1.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                              "34e261f78cb6ceaaa36f42f2613f4380d94d9c759a9c73c769ee6e0247364632",
                              "f7/a4/4399daaf8f7dfee9d7c3327fdb0426ee041cc63edc358b93911ceb2bfc7a/cffi-2.1.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
    ("cp312", "linux/x86_64"):                              ("cffi-2.1.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                              "c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf",
                              "b1/db/dceb9dd5b231e1da801793f8acc9f3c52a7e1afe40bb1aae37e02b0faad5/cffi-2.1.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
    ("cp313", "linux/x86_64"):                              ("cffi-2.1.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                              "a931079504ecc49efed7744c476a5c343a92fabf66dec2db95edb1b2fdc770e2",
                              "95/95/86342356ff5953b3fb06f7ef7c5bee212d45e770abc7218d451b9148313c/cffi-2.1.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
    ("cp314", "linux/x86_64"):                              ("cffi-2.1.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                              "b0431303acaea1089ad4b3e9ce4e6518193def1118d4073ca848635ee4ea2e96",
                              "e9/02/4e7d553a7ac4b4238b38b3c1b80d486e9d4436f8d2acbf87a0997fe3f402/cffi-2.1.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"),
    ("cp310", "linux/aarch64"):                               ("cffi-2.1.1-cp310-cp310-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
                               "5a59cc1c4442bc3d5c703bf720b51138d0bfc173618807c9ee2490a7541dd3d9",
                               "a3/b9/0f2e58b2cefa33255bff36935d42b13180fe559bba82596540eb404bde7d/cffi-2.1.1-cp310-cp310-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
    ("cp311", "linux/aarch64"):                               ("cffi-2.1.1-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
                               "3311ed60d36f83378794e1009ac6258bafbf81f7888b4caa7b35a521e3f95813",
                               "ad/66/c19feabb28485b6e0bbaaafa90837a1ef5d302e90f2178bd33f17a49879b/cffi-2.1.1-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
    ("cp312", "linux/aarch64"):                               ("cffi-2.1.1-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
                               "68e62fe11f30d5ca8289242866f0a5291402d8529ca2178ab8afc5c9694ae890",
                               "44/de/f98430906df1545ffde0d543dd124a7a439bc2cd32b36b9c53f805df7333/cffi-2.1.1-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
    ("cp313", "linux/aarch64"):                               ("cffi-2.1.1-cp313-cp313-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
                               "f16c709686a78c727bbbf059f92b0bf41c6fc60deec706d2dc19f529175a6125",
                               "37/6f/3b5ce4c3b2192d250f04908f2bfd91ef34552ec8f7716a5d4abdb8d67bb2/cffi-2.1.1-cp313-cp313-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
    ("cp314", "linux/aarch64"):                               ("cffi-2.1.1-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
                               "58acb8ab8e295e6c5ea12f888cbb13cf21511ef2a3303a23f4325c29d17fe5c1",
                               "67/b8/b42132ca113dc567d37684437b46ca1dafc885902b02a110a02d5b511857/cffi-2.1.1-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"),
    ("cp310", "darwin/x86_64"):                               ("cffi-2.1.1-cp310-cp310-macosx_10_15_x86_64.whl",
                               "baed1e86cc735622097354b9d1281406caf42ff42a886d29faa8e8d1630333be",
                               "b6/d2/2cde336b375f55c76ca670f0be3978cc048e31e24f3b4d7ce8473150a388/cffi-2.1.1-cp310-cp310-macosx_10_15_x86_64.whl"),
    ("cp311", "darwin/x86_64"):                               ("cffi-2.1.1-cp311-cp311-macosx_10_15_x86_64.whl",
                               "c8d2c9fd1f2d16f780d15127abb050d13d1a76c03a4bd87d7e4980e45e511e12",
                               "70/d2/16d99a0c4948febc0ebd133a13b2f688ff7f8cb04da971e1128872ce0c03/cffi-2.1.1-cp311-cp311-macosx_10_15_x86_64.whl"),
    ("cp312", "darwin/x86_64"):                               ("cffi-2.1.1-cp312-cp312-macosx_10_15_x86_64.whl",
                               "c8c69575568085ba0b1b10c0249d779a214aea6f6522e949a0fc9fb0fcb449d0",
                               "10/69/43965eccfdead3b9220015fd1320e117be8c6ed01a62ffab76eeb752f5d5/cffi-2.1.1-cp312-cp312-macosx_10_15_x86_64.whl"),
    ("cp313", "darwin/x86_64"):                               ("cffi-2.1.1-cp313-cp313-macosx_10_15_x86_64.whl",
                               "9d2055050ea716bd38b7f7f1579c275386646b4894c155a3e2f3cd62ed41b7c6",
                               "a7/46/2e5fdde8555706dd98139a910ca11be02809f3f605ce956f655d0214e100/cffi-2.1.1-cp313-cp313-macosx_10_15_x86_64.whl"),
    ("cp314", "darwin/x86_64"):                               ("cffi-2.1.1-cp314-cp314-macosx_10_15_x86_64.whl",
                               "d28630f5854ab07ab1fd4aba756de52326c82e6be15d414b12793f1975048b54",
                               "d9/99/c4b0c17cacdc9c3b8f280026286a9826d6a208c0f047591a3c3ce99b91fd/cffi-2.1.1-cp314-cp314-macosx_10_15_x86_64.whl"),
    ("cp310", "darwin/arm64"):                              ("cffi-2.1.1-cp310-cp310-macosx_11_0_arm64.whl",
                              "ca82be1a1d406ecfe1d25dc16cb33488e5a16bf4438c9fb590484ea29d92478b",
                              "94/1a/4b2f7c92293ba05cbd4a9a1b28faaf0326272d9488e6354657571c48a7aa/cffi-2.1.1-cp310-cp310-macosx_11_0_arm64.whl"),
    ("cp311", "darwin/arm64"):                              ("cffi-2.1.1-cp311-cp311-macosx_11_0_arm64.whl",
                              "398aff33cee2767e3e781d2554c54bd0dff386bb437581e0d8011fde1a942ec1",
                              "cd/95/31b535a9f0220ae9f357de4a08d57ce89cb417653c2fd9f075f50822a388/cffi-2.1.1-cp311-cp311-macosx_11_0_arm64.whl"),
    ("cp312", "darwin/arm64"):                              ("cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl",
                              "f81b3b8f3d4e343550fa4baa0e479bba9f2d29ce9c2e9b51d1ce1718d7442fcf",
                              "54/7d/16e5a096677b5e313ca80cd5e5170efa3ea44624a82bb111925522da64b1/cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl"),
    ("cp313", "darwin/arm64"):                              ("cffi-2.1.1-cp313-cp313-macosx_11_0_arm64.whl",
                              "19ee6127ee34de7d83ce3d371ebc5ed91addbdcc39f9ab15ce4eb35a4e534971",
                              "55/41/4c7042f317b9217502988f0873af87e16ad606dc20f84e546e3e6ce9764c/cffi-2.1.1-cp313-cp313-macosx_11_0_arm64.whl"),
    ("cp314", "darwin/arm64"):                              ("cffi-2.1.1-cp314-cp314-macosx_11_0_arm64.whl",
                              "661c298b4821edebead0c91edd2b00374d67ad7c5a1f7a91d4442633b79d6a72",
                              "b3/a9/9db617d05d7367c1ad0ab00b3aa6e6f9281edd689b4ee9ea0e5a84e89c97/cffi-2.1.1-cp314-cp314-macosx_11_0_arm64.whl"),
    ("cp310", "win/amd64"):                           ("cffi-2.1.1-cp310-cp310-win_amd64.whl",
                           "a48d62ab9d6f4f98c983223a547af44be6ca3691074c31cecced6facd3ba2dc1",
                           "ba/0b/644a2ec1a4eaba49c2939410bb1eb1d25b09d6d0582f5d2f95c537043725/cffi-2.1.1-cp310-cp310-win_amd64.whl"),
    ("cp311", "win/amd64"):                           ("cffi-2.1.1-cp311-cp311-win_amd64.whl",
                           "42f6930c31dc7f50732c9ae793c2786c7b6b044195967bbdde40bb9be81c4cc0",
                           "73/c0/77ba02423c2f7d7091143c45cd49e0e6575c4c1967394bb542bd923a9b74/cffi-2.1.1-cp311-cp311-win_amd64.whl"),
    ("cp312", "win/amd64"):                           ("cffi-2.1.1-cp312-cp312-win_amd64.whl",
                           "f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a",
                           "d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/cffi-2.1.1-cp312-cp312-win_amd64.whl"),
    ("cp313", "win/amd64"):                           ("cffi-2.1.1-cp313-cp313-win_amd64.whl",
                           "1aa5645c30469b09530c4ebca77ebf8f17618293c58f8549cb1a543a50236e7d",
                           "60/a6/8b149b2c3f2e11aaa1618ef64500b45f50f22c57a977a4dff1aff1f91042/cffi-2.1.1-cp313-cp313-win_amd64.whl"),
    ("cp314", "win/amd64"):                           ("cffi-2.1.1-cp314-cp314-win_amd64.whl",
                           "3222ba5d678f80a030e6afbcc33dc1ae5cb45facabb61cee2c7016b8432fde48",
                           "a7/06/1c3e01e3ba14c39f6d10bfbac52753b7e22259e38088e5cfe1d704918690/cffi-2.1.1-cp314-cp314-win_amd64.whl"),
}

# Pure-python wheels are identical on every platform.
WHEELS_ANY = [
         ("pycparser-2.23-py3-none-any.whl",
      "e5c6e8d3fbad53479cab09ac03729e0a9faf2bee3db8208a550daf5af81a5934",
      "a0/e3/59cd50310fc9b59512193629e1984c1f95e5c8ae6e5d8c69532ccc65a7fe/pycparser-2.23-py3-none-any.whl"),
         ("certifi-2026.7.22-py3-none-any.whl",
      "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775",
      "0b/a7/71ac2cff56fec219ed242bb11b8efb69fcc4bec75db06fb7bfe35de520e6/certifi-2026.7.22-py3-none-any.whl"),
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
    request = Request(url, headers={"User-Agent": "calibre-tolino-sync/0.9.7"})
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
    try:
        import curl_cffi  # noqa: PLC0415
        from curl_cffi.requests import Session  # noqa: PLC0415
        return Session
    except Exception:
        pass
    if root not in sys.path:
        sys.path.insert(0, root)
    for prefix in ("curl_cffi", "cffi", "_cffi_backend", "pycparser", "certifi"):
        for name in [m for m in list(sys.modules)
                     if m == prefix or m.startswith(prefix + ".")]:
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

    platform = _platform_key()
    if platform not in CURL_CFFI_WHEELS:
        raise BootstrapError(
            "No prebuilt curl_cffi wheel for this platform "
            "(sys.platform=%s, machine=%s)." % (sys.platform, _machine()))
    tag = _cffi_tag()
    if (tag, platform) not in CFFI_WHEELS:
        raise BootstrapError(
            "curl_cffi %s supports Python 3.10-3.14, but this Calibre runs "
            "Python %s. Install curl_cffi manually instead: "
            "python3 -m pip install --user curl_cffi."
            % (CURL_CFFI_VERSION, "%d.%d.%d" % sys.version_info[:3]))
    wheels = list(WHEELS_ANY) + [
        CURL_CFFI_WHEELS[platform], CFFI_WHEELS[(tag, platform)]]

    installed = []
    for filename, sha256, url_path in wheels:
        notify("Lade %s ..." % filename)
        blob = _download(_FILES + url_path, sha256)
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



def _cffi_tag():
    """CPython ABI tag of the running interpreter (cp310..cp314) or None."""
    major, minor = sys.version_info[0], sys.version_info[1]
    if major != 3 or minor < 10 or minor > 14:
        return None
    return "cp%d%d" % (major, minor)


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
