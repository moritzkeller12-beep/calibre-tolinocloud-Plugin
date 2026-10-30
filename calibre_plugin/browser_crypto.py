"""CryptoJS/OpenSSL AES-CBC decoding for Web Reader token blobs."""
import base64
import hashlib

from Crypto.Cipher import AES


# CryptoJS.AES.encrypt(value, VERSION.PHRASE) -> Base64 in IndexedDB
# (userToken/userInfos): OpenSSL-"Salted__"-KDF, AES-256-CBC; jede
# Passphrase wird akzeptiert, auch eine kuenftig geaenderte.


def _evp_bytes_to_key(password, salt, key_len=32, iv_len=16):
    """OpenSSL EVP_BytesToKey (MD5) as used by CryptoJS."""
    derived = b""
    previous = b""
    while len(derived) < key_len + iv_len:
        previous = hashlib.md5(previous + password + salt).digest()
        derived += previous
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _pkcs7_unpad(data):
    if not data or len(data) % 16:
        return None
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]


def cryptojs_decrypt(b64_text, passphrase=""):
    """Decrypt a CryptoJS.AES.encrypt Base64 blob; return "" on any mismatch."""
    try:
        raw = base64.b64decode("".join(str(b64_text or "").split()), validate=False)
    except Exception:
        return ""
    if len(raw) < 32 or raw[:8] != b"Salted__":
        return ""
    salt = raw[8:16]
    ciphertext = raw[16:]
    if not ciphertext or len(ciphertext) % 16:
        return ""
    key, iv = _evp_bytes_to_key(str(passphrase).encode("utf-8"), salt)
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
