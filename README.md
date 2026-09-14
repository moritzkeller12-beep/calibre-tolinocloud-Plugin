# Tolino Cloud Sync for Calibre

Plugin version: **0.3.2**. Author: **moritzkeller12-beep**.

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

Use **Debug / Diagnose** beside the synchronization button to inspect the
preparation path locally without logging in or contacting Tolino. The
copyable report includes the Calibre version, database type, book-ID count,
metadata key/type shapes, format return values, optional cover type/size, and
the `plan_sync` result shape. Each step records its exception type, message,
and traceback while allowing later steps to continue. **Tolino-Antwort testen**
is a separate explicit action in that dialog; it is the only diagnostic action
that performs login/network access, and its response is redacted.

Before uploading, the plugin loads the Tolino inventory and shows a
confirmation table with local and remote status (new in Calibre, Tolino-only,
identical, or changed), title, authors, ISBN, and Tolino ID. New and changed
Calibre books are selected by default, while the user can adjust the upload
selection. UUIDs and stored Tolino IDs are preferred for matching; ISBN and
normalized author/title are readable fallback matches. Author, title, and ISBN
checkboxes control comparison/filter matching only: the current Tolino API has
no verified metadata-update endpoint, so these fields are never sent as fake
metadata requests. Actual uploads are ebook files in the selected format and,
optionally, covers. Deletions remain a separate opt-in setting.
When replacing an existing Tolino deliverable, its old ID is cleaned up after
the replacement upload to prevent duplicates; the opt-in setting controls
deleting books that are no longer present in Calibre.

The recommended authentication path is a refresh token obtained from the
partner's Web Reader network requests. Tokens, credentials, and sync state are
stored through Calibre's `JSONConfig` mechanism and are never logged.
If the explicit Tolino test reports `HTTP 400 invalid_grant` / `Invalid refresh
token`, the configured token is expired, invalid, or is not a refresh token.
For partner 8 the verified refresh request is an URL-encoded form with
`client_id=webreader`, `grant_type=refresh_token`, `scope=SCOPE_BOSH`, and
`https://www.orellfuessli.ch/auth/oauth2/token`; authenticated API requests use
the selected partner ID as `reseller_id`. A Web Reader `access_token` cannot be
used as a refresh-token fallback. Copy the Web Reader request's
`refresh_token` value, including its complete value, and paste it; surrounding
whitespace and outer quotes are removed automatically. Never include the token
itself in a diagnostic report. The report shows only its category, length, a
four-character prefix, normalization flags, partner configuration, and HTTP
status/error text.

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
on publicly documented web-reader behavior and compatibility testing.
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
