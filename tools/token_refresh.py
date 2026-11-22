#!/usr/bin/env python3
"""Gespeicherte tolino-Zugaenge ohne geoeffnetes Calibre erneuern.

Calibre kann tagelang geschlossen sein; dann laeuft auch der Keep-alive des
Plugins nicht, und ein Refresh-Token laeuft mit der Zeit ab. Dieses Skript
startet Calibres eigene Python (``calibre-debug``), laedt das installierte
Plugin aus seinem ZIP und tauscht faellige Konten -- dieselbe Logik, dieselben
Sperren und derselbe Einstellungsspeicher wie im Plugin.

Aufruf::

    python3 tools/token_refresh.py [--plugin PFAD.zip] [--config-dir DIR]

Gedacht fuer einen systemd-User-Timer, Cron oder die Windows-Aufgabenplanung.
Ohne faelliges Konto passiert nichts (kein Netzaufruf); der Takt darf deshalb
kurz sein.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Calibre legt das installierte Plugin unter seinem Plugin-Namen ab.
PLUGIN_ZIP = "Tolino Cloud Sync.zip"
PACKAGE = "calibre_plugins"
MODULE = "tolino_cloud_sync"
# Laeuft in Calibres Python: dort gibt es calibre.utils.config, das die
# Einstellungen des Plugins liest und schreibt.
RUNNER = """
import sys
sys.path.insert(0, {root!r})
from calibre_plugins.tolino_cloud_sync.token_vault import TOKEN_VAULT
rotated = TOKEN_VAULT.keepalive_refresh(only_due=True)
print("tolino: %d Konto/Konten erneuert" % rotated)
"""


def config_dir(explicit=None):
    """Calibre-Konfigurationsverzeichnis wie Calibre selbst es bestimmt."""
    if explicit:
        return Path(explicit)
    from_env = os.environ.get("CALIBRE_CONFIG_DIRECTORY")
    if from_env:
        return Path(from_env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "calibre"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "calibre"
    return Path(os.environ.get("XDG_CONFIG_HOME")
                or Path.home() / ".config") / "calibre"


def plugin_zip(explicit, directory):
    """Pfad zum installierten Plugin-ZIP."""
    if explicit:
        return Path(explicit)
    candidate = Path(directory) / "plugins" / PLUGIN_ZIP
    if candidate.is_file():
        return candidate
    raise SystemExit("Plugin-ZIP nicht gefunden: %s -- bitte --plugin angeben."
                     % candidate)


def unpack(archive, target):
    """ZIP als importierbares Paket entpacken (ohne Calibre anzufassen)."""
    package = Path(target) / PACKAGE
    module = package / MODULE
    module.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    with zipfile.ZipFile(archive) as source:
        source.extractall(module)
    return Path(target)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Faellige tolino-Zugaenge ohne Calibre erneuern.")
    parser.add_argument("--plugin", help="Pfad zum installierten Plugin-ZIP")
    parser.add_argument("--config-dir", dest="config",
                        help="Calibre-Konfigurationsverzeichnis")
    args = parser.parse_args(argv)
    runner = shutil.which("calibre-debug")
    if not runner:
        raise SystemExit("calibre-debug nicht gefunden: Calibre muss "
                         "installiert sein.")
    directory = config_dir(args.config)
    archive = plugin_zip(args.plugin, directory)
    with tempfile.TemporaryDirectory(prefix="tolino-refresh-") as tmp:
        root = unpack(archive, tmp)
        environment = dict(os.environ)
        # Dieselben Einstellungen wie das Plugin, auch wenn Calibre sonst
        # woanders sucht.
        environment["CALIBRE_CONFIG_DIRECTORY"] = str(directory)
        return subprocess.run(
            [runner, "-c", RUNNER.format(root=str(root))],
            env=environment, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
