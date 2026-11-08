"""CryptoJS/OpenSSL AES-CBC decoding for Web Reader token blobs."""
import base64
import hashlib

from .errors import TolinoAuthError


# CryptoJS.AES.encrypt(value, VERSION.PHRASE) -> Base64 in IndexedDB
# (userToken/userInfos): OpenSSL-"Salted__"-KDF, AES-256-CBC mit der
# festen leeren Passphrase des Readers.


def _evp_bytes_to_key(password, salt):
    """OpenSSL EVP_BytesToKey (MD5) as used by CryptoJS."""
    derived = b""
    previous = b""
    while len(derived) < 48:
        previous = hashlib.md5(previous + password + salt).digest()
        derived += previous
    return derived[:32], derived[32:48]


def _pkcs7_unpad(data):
    if not data or len(data) % 16:
        return None
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]


def _aes():
    """Load AES lazily; a missing optional dependency is a user-facing error.

    PyCryptodome ist nicht in jeder Calibre-Installation vorhanden. Der
    Import darf das Plugin nicht blockieren, aber ein verschlüsselter
    Web-Reader-Token ohne Decoder ist kein «ungültiger Token»: der Nutzer
    braucht einen klaren Hinweis statt eines stillen Login-Fehlschlags.
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES
        except ImportError as exc:
            raise TolinoAuthError(
                "Der Web-Reader-Zugang ist verschlüsselt, aber PyCryptodome "
                "fehlt. Bitte PyCryptodome installieren und die Anmeldung "
                "erneut starten.",
                code="A01",
                short="Verschlüsselter Web-Reader-Zugang: PyCryptodome fehlt.",
                detail=str(exc)) from exc
    return AES


def cryptojs_decrypt(b64_text):
    """Decrypt a CryptoJS blob; return "" on mismatch, raise without AES."""
    try:
        raw = base64.b64decode("".join(str(b64_text or "").split()), validate=False)
    except ValueError:
        return ""
    if len(raw) < 32 or raw[:8] != b"Salted__":
        return ""
    salt = raw[8:16]
    ciphertext = raw[16:]
    if not ciphertext or len(ciphertext) % 16:
        return ""
    AES = _aes()
    key, iv = _evp_bytes_to_key(b"", salt)
    try:
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
    except ValueError:
        return ""
    plain = _pkcs7_unpad(plain)
    if plain is None:
        return ""
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return ""
