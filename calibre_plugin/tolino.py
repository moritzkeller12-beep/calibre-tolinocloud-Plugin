import json
import mimetypes
import os
import platform
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TolinoError(Exception):
    pass


class TolinoAuthError(TolinoError):
    pass


class TolinoApiError(TolinoError):
    pass


PARTNERS = {
    3: {
        "name": "Thalia.de",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.de/auth/oauth2/token",
    },
    4: {"name": "Thalia.at", "client_id": "webshop01",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.at/de.buch.appservices/api/4004/oauth2/token"},
    6: {"name": "Buch.de", "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE"},
    8: {"name": "Books.ch / orellfuessli.ch", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.orellfuessli.ch/auth/oauth2/token"},
    13: {
        "name": "Hugendubel.de",
        "client_id": "4c20de744aa8b83b79b692524c7ec6ae",
        "scope": "ebook_library",
        "token_url": "https://api.hugendubel.de/rest/oauth2/token",
    },
    23: {"name": "Osiander.de", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.osiander.de/auth/oauth2/token"},
    30: {"name": "Buecher.de", "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE",
        "token_url": "https://www.buecher.de/oauth2/token"},
}

BASE_URL = "https://bosh.pageplace.de/bosh/rest"


def hardware_id():
    os_id = {"Windows": "1", "Darwin": "2", "Linux": "3"}.get(platform.system(), "x")
    return "%sxxA-00BCD-EFGHI-JKLMN-OPQRh" % os_id


class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, username=None,
                 secret=None, timeout=45):
        if partner_id not in PARTNERS:
            raise TolinoError("Unsupported Tolino partner ID: %s" % partner_id)
        self.partner_id = int(partner_id)
        self.partner = PARTNERS[self.partner_id]
        self.hardware = hardware or hardware_id()
        self.refresh = refresh
        self.username = username
        self.password = secret
        self.timeout = timeout
        self.access = None
        self.expires_at = 0

    def login(self):
        if self.refresh:
            if not self.partner.get("token_url"):
                raise TolinoAuthError(
                    "This partner has no verified refresh-token endpoint in the reference client."
                )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": self.refresh,
                "scope": self.partner["scope"],
            }
        elif self.username and self.password:
            raise TolinoAuthError(
                "Username/password login requires the partner's browser OAuth flow. "
                "Use a Web Reader refresh token for this plugin."
            )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": self.partner["scope"],
            }
        else:
            raise TolinoAuthError("Configure a refresh token or username/password.")
        try:
            data = self._request(self.partner["token_url"], "POST", payload,
                                 form=True, authenticated=False)
        except TolinoApiError as exc:
            raise TolinoAuthError("Tolino authentication failed: %s" % exc)
        if not data.get("access_token"):
            raise TolinoAuthError("Tolino token response did not contain access_token.")
        self.access = data["access_token"]
        self.refresh = data.get("refresh_token", self.refresh)
        self.expires_at = time.time() + max(0, int(data.get("expires_in", 3600)) - 60)
        return self.refresh

    def _request(self, url, method="GET", data=None, form=False,
                 authenticated=True, content_type=None, _retry=True):
        if authenticated and (not self.access or time.time() >= self.expires_at):
            self.login()
        body = None
        headers = {"User-Agent": "Calibre-Tolino-Plugin/0.1"}
        if authenticated:
            headers.update({
                "t_auth_token": self.access,
                "hardware_id": self.hardware,
                "reseller_id": str(self.partner_id),
            })
        if data is not None:
            if form:
                body = urlencode(data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif isinstance(data, bytes):
                body = data
                headers["Content-Type"] = content_type or "application/octet-stream"
            else:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = content_type or "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 401 and authenticated and self.refresh and _retry:
                self.access = None
                self.login()
                return self._request(url, method, data, form, True,
                                     content_type, _retry=False)
            if exc.code in (401, 403):
                self.access = None
                raise TolinoAuthError("Tolino rejected authentication (%s)." % exc.code)
            raise TolinoApiError("Tolino HTTP %s: %s" % (exc.code, detail))
        except (URLError, OSError) as exc:
            raise TolinoApiError("Tolino request failed: %s" % exc)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise TolinoApiError("Tolino returned invalid JSON.")

    def inventory(self):
        data = self._request(BASE_URL + "/inventory/delta?strip=true")
        inventory = data.get("PublicationInventory", {})
        return inventory.get("edata", []) + inventory.get("ebook", [])

    def inventory_ids(self):
        ids = set()
        for item in self.inventory():
            if not isinstance(item, dict):
                continue
            value = item.get("id") or item.get("deliverableId")
            if value:
                ids.add(str(value))
        return ids

    @staticmethod
    def _multipart(fields, file_path, field="file"):
        boundary = ("----CalibreTolino%s" % uuid.uuid4().hex).encode("ascii")
        chunks = []
        for key, value in fields.items():
            chunks.extend([b"--" + boundary + b"\r\n",
                           ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % key).encode(),
                           str(value).encode(), b"\r\n"])
        name = os.path.basename(file_path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        chunks.extend([b"--" + boundary + b"\r\n",
                       ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field, name)).encode(),
                       ("Content-Type: %s\r\n\r\n" % mime).encode(),
                       open(file_path, "rb").read(), b"\r\n",
                       b"--" + boundary + b"--\r\n"])
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary.decode()

    def upload(self, file_path):
        body, content_type = self._multipart({}, file_path)
        data = self._request(BASE_URL + "/upload", "POST", body,
                             content_type=content_type)
        deliverable = data.get("metadata", {}).get("deliverableId")
        if not deliverable:
            raise TolinoApiError("Upload response did not contain deliverableId.")
        return str(deliverable)

    def upload_cover(self, deliverable_id, file_path):
        body, content_type = self._multipart({"deliverableId": deliverable_id}, file_path)
        self._request(BASE_URL + "/cover", "POST", body, content_type=content_type)

    def delete(self, deliverable_id):
        self._request(BASE_URL + "/deletecontent?deliverableId=" + str(deliverable_id))
