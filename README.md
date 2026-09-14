# Tolino Cloud Sync for Calibre

Native Python plugin for synchronizing an open Calibre library with the
Tolino Cloud. It runs directly inside Calibre as a standalone plugin.

## Installation

From the repository root, run:

```text
python3 build_plugin.py
```
This creates `tolino_cloud_sync.zip`. In Calibre 7.6.0 or newer, choose
**Preferences > Plugins > Load plugin from file**, select that ZIP, and
restart Calibre. The archive contains the Calibre metadata and implementation
files at its top level:

```text
__init__.py
plugin-import-name-tolino_cloud_sync.txt
ui.py
config.py
sync.py
tolino.py
```

Calibre requires the top-level `__init__.py`; do not install the source
directory itself. The empty marker is required by Calibre's plugin loader to
map the archive to the `tolino_cloud_sync` namespace. Calibre's
`CalibrePluginFinder` then maps these root-relative files to
`calibre_plugins.tolino_cloud_sync.*`, so the implementation's relative
imports work normally.

The plugin entry point is `TolinoSyncPlugin` in the root `__init__.py` with
`actual_plugin =
"calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"`, matching the
physical package path in the archive.

## Configuration and use

Open the **Tolino Cloud Sync** toolbar/menu action and configure:

- Tolino partner ID
- stable hardware ID (generated automatically when empty)
- Web Reader refresh token
- preferred formats, normally EPUB and PDF
- optional cover upload
- optional deletion of cloud books no longer present in Calibre

The action opens a dashboard rather than a one-shot prompt. It shows partner
and login status, masks the refresh token, offers **Im Browser anmelden /
Sign in in browser**, and keeps the format, cover, and deletion settings
together. **Synchronisierung starten / Start synchronization** prepares the
library plan, then performs network work in a Qt worker thread with a live
progress bar, status text, and **Abbrechen / Abort** button. Authentication,
preparation, and API failures appear in visible bilingual error dialogs. No
screenshots are included because the UI is rendered by Calibre's own Qt
widgets and depends on the installed Calibre theme.

The recommended authentication path is a refresh token obtained from the
partner's Web Reader network requests. Tokens, credentials, and sync state are
stored through Calibre's `JSONConfig` mechanism and are never logged.

The dialog also provides **Im Browser anmelden**. The reference
implementations register partner-specific Web Reader URLs, not loopback
redirects. Therefore the current partners deliberately report that a local
browser callback is not supported instead of pretending an unregistered OAuth
flow works. The implementation retains loopback-only callback validation with
random state and a five-minute expiry for a partner only when its configuration
explicitly proves support.

## Synchronization behavior

Books are matched by Calibre UUID and a deterministic content fingerprint.
New or changed supported-format books are uploaded. Covers are optional.
Deletion is disabled by default and, when enabled, re-checks the Tolino
inventory before deleting a recorded deliverable ID. Cancellation stops before
the next operation; completed operations remain in the persisted state.

The Tolino API is unofficial and partner-specific. The implementation is based
on the reference projects
[darkphoenix/tolino-calibre-sync](https://github.com/darkphoenix/tolino-calibre-sync)
and [poesterlin/tolino-calibre-sync](https://github.com/poesterlin/tolino-calibre-sync).
Metadata updates, collections, device registration, cloud downloads, and
partner-specific browser OAuth are intentionally outside this one-way sync.
Partner endpoint changes, token expiry, rate limits, and response-format
changes remain possible.

## Local validation

The tests make no Tolino requests:

```text
python3 -m unittest calibre_plugin.test_sync
python3 -m py_compile calibre_plugin/*.py build_plugin.py
python3 build_plugin.py
```
