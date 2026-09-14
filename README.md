# Tolino Calibre Sync

Synchronize your Calibre library with the Tolino Cloud.

## Features

- Uploads new books from Calibre to Tolino Cloud.
- Uploads covers for uploaded books.
- Uses Calibre UUIDs to track synced books and avoid duplicates.
- (Optional) Deletes books from Tolino Cloud if they are removed from Calibre.
- Supports Calibre servers requiring HTTP Digest.

## Setup

1.  **Prerequisites:** Docker and Docker Compose installed on your system. Calibre Content Server running and accessible using a Username + Password.
2.  **Configure:**
    - Copy the `.env.example` file to `.env`.
    - Edit the `.env` file with your specific Calibre and Tolino details (see comments within the file).
3.  **Start the setup:**
      ```bash
      docker-compose up
      ```

## Native Calibre plugin

This repository also contains a native Python plugin in `calibre_plugin/`. It
syncs directly with the open Calibre library and does not require the Docker,
Bun, or Content Server setup above.

1. Run `python build_plugin.py` from this repository.
2. In Calibre choose **Preferences > Plugins > Load plugin from file** and
   select `tolino_cloud_sync.zip`, then restart Calibre.
3. Use the **Tolino Cloud Sync** toolbar/menu action to configure the partner,
   a Tolino refresh token (recommended) or login, preferred EPUB/PDF formats,
   cover uploads, and the optional deletion switch.

Settings and the UUID-to-Tolino-ID state mapping are stored through Calibre's
`JSONConfig` plugin configuration mechanism. Tokens are never written to logs;
protect the Calibre configuration directory with normal OS permissions. The
plugin matches books by Calibre UUID and records a content fingerprint so new
and changed books are uploaded. Deletions are disabled by default and only
run when explicitly enabled. **Abort** stops before the next operation; an
already completed upload is retained in the state mapping.

The Tolino endpoints are not officially documented and differ between
partners. The plugin currently ships endpoint/auth settings for Thalia.de and
the partner IDs documented by the reference client (Thalia, Thalia.at,
Buch.de, books.ch/Orell Füssli, Hugendubel, Osiander, and Buecher.de);
refresh-token login is the reliable path. Partner API changes,
token expiry, rate limits, and server-side metadata differences remain known
risks. No external Tolino calls are made by the local tests:

```text
python -m unittest calibre_plugin.test_sync
```

### Reference implementation comparison

The native client was checked against
[darkphoenix/tolino-calibre-sync](https://github.com/darkphoenix/tolino-calibre-sync)
(`tolinocloud.py`) and this repository's
[Node client](https://github.com/poesterlin/tolino-calibre-sync/blob/main/src/tolino-cloud.js).
It uses the same `pageplace.de` inventory, upload, cover, and
`deletecontent` endpoints, the same `t_auth_token`/`hardware_id`/`reseller_id`
headers, multipart `file` uploads, `deliverableId` cover field, and refresh
token grant. The state matcher intentionally improves on the legacy title
matching by using Calibre UUID plus a fingerprint.

The reference clients also expose metadata updates, collections, device
registration, and cloud downloads. Those operations are deliberately not
called by this one-way Calibre-to-cloud plugin; implementing them would add
destructive or ambiguous behavior unrelated to library synchronization.
Username/password browser OAuth is likewise not faked as an OAuth password
grant: the native plugin requires a refresh token and reports this limitation
explicitly. Deletion additionally re-checks Tolino inventory before issuing a
delete request.

## Configuration (`.env` file)

See the comments in the `.env.example` file for detailed explanations of each setting. Key sections include:

- **Calibre Server:** `CALIBRE_BASE_URL`, `CALIBRE_LIBRARY_ID`, `CALIBRE_USERNAME`, `CALIBRE_PASSWORD`.
- **Tolino Cloud:** `TOLINO_PARTNER_ID`.
- **Tolino Authentication:** Choose **one** method (Username/Password OR Device/Refresh Token).
- **Sync Behavior:** `SYNC_STATE_FILE`, `SYNC_DOWNLOAD_DIR`, `SYNC_ENABLE_DELETIONS`, `SYNC_UPLOAD_COVERS`.

## Authentication Methods

You need to configure how the script authenticates with your Tolino Cloud account. Choose **one** of the following methods in your `.env` file.

###  Device Login / Refresh Token (Workaround for Bot Blocking)

This method uses credentials (a `hardware_id` and a `refresh_token`) obtained from a successful login session in your web browser. 

**Steps to Obtain Credentials:**

1.  Open your Tolino partner's **Web Reader** in your web browser (e.g., Chrome, Firefox).
2.  **Before logging in**, open your browser's **Developer Tools** (usually by pressing `F12`).
3.  Navigate to the **Network** tab within the Developer Tools. Ensure network recording is active (usually a red circle or similar). You might want to check the "Preserve log" option.
4.  Now, **log in** to the Tolino Web Reader using your normal username and password through the web interface.
5.  Look through the network requests list in the Developer Tools. You need to find two specific pieces of information:
    - **Hardware ID:** Use the search/filter bar in the Network tab and search for `registerhw`. Look in the **Request Headers** section for a header named `hardware_id`. Copy its value (it will look something like `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` or similar).
    - **Refresh Token:** Use the search/filter bar again and search for `token`. Find the request made to an endpoint like `/auth/oauth2/token`. Click on this request. Look in the **Response** or **Preview** tab for the JSON response body. Find the key named `refresh_token` and copy its value (it will be a long string, often resembling a UUID).
6.  **Important:** Do not explicitly log out of the Web Reader session in your browser while performing these steps, although the refresh token should ideally remain valid even after you close the browser.

**Configure the Script:**

1.  Edit your `.env` file.
2.  Set `TOLINO_HARDWARE_ID` to the `hardware_id` value you copied.
3.  Set `TOLINO_REFRESH_TOKEN` to the `refresh_token` value you copied.
4.  **Ensure `TOLINO_USERNAME` and `TOLINO_PASSWORD` are commented out or empty.**
5.  Make sure the `TOLINO_PARTNER_ID` is set correctly for the account you logged into in the Web Reader.

The script will now use the `hardware_id` and `refresh_token` to authenticate directly with the Tolino API, bypassing the interactive login page.

**Note:** Refresh tokens can eventually expire (though they often last a long time). If the script starts failing with authentication errors using this method after a while, you may need to repeat the process above to obtain a new refresh token.

The sync process with run every hour and will check for new books in your Calibre library and upload them to the Tolino Cloud.

## Development

### Prerequisites

- Bun.js installed. Get it from [bun.sh](https://bun.sh/).
- `bun install` to install dependencies.
- `bun run dev` to start the development watcher.

## Credits

I used the [tolino-calibre-sync project from darkphoenix](https://github.com/darkphoenix/tolino-calibre-sync) to write Javascript version that is using the calibre content server instead of calibre directly. 
