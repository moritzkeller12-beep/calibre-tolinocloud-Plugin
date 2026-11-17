"""Entschluesselung echter Web-Reader-Bloecke (CryptoJS/OpenSSL).

Die gueltigen Fixtures stammen NICHT aus den hier geprueften Funktionen: sie
wurden mit dem OpenSSL-Kommando erzeugt

    openssl enc -aes-256-cbc -md md5 -pass pass: -S <salt-hex>

und anschliessend nur um den CryptoJS-Container ``Salted__`` + Salt ergaenzt.
Ein Roundtrip durch `_evp_bytes_to_key()` wuerde denselben Fehler auf beiden
Seiten verstecken.
"""
import base64
import unittest
from unittest.mock import patch

from ..browser_crypto import cryptojs_decrypt
from ..errors import TolinoAuthError
from ..token_candidates import (_decrypt_reader_blob, _hardware_from_reader_blob,
                                _refresh_field_text)

# Salt 0011223344556677, Klartext "refresh-token-0123456789-abcdef".
TOKEN_BLOB = ("U2FsdGVkX18AESIzRFVmd+krvaEq0dFWf6an5omQdUciNDVjZt51GgBA"
              "xhlwp3qq")
TOKEN_PLAIN = "refresh-token-0123456789-abcdef"

# Salt 8899aabbccddeeff, Klartext {"hardwareId":"hw-42","refresh":"tok-9"}.
USERINFOS_BLOB = ("U2FsdGVkX1+Imaq7zN3u/6GuqtezSYk7MbXcjB5gXA7E0wV+xfrT/"
                  "GJbwJBo7BDZwAN7nJi+rA2rZJ6ialFTCw==")
USERINFOS_PLAIN = '{"hardwareId":"hw-42","refresh":"tok-9"}'

# Salt 0102030405060708, Klartext \xff\xfe\xfa: gueltiges Padding, kein UTF-8.
BAD_UTF8_BLOB = "U2FsdGVkX18BAgMEBQYHCKOyTjr4WBTk5NFNgncrn6U="

# Letzter Ciphertext-Block ersetzt: das Padding passt nicht mehr.
BAD_PADDING_BLOB = base64.b64encode(
    base64.b64decode(TOKEN_BLOB)[:32] + b"\x00" * 16).decode()


def no_crypto():
    """Steht fuer eine Installation ohne PyCryptodome."""
    return patch("calibre_plugin.browser_crypto._aes",
                 side_effect=TolinoAuthError("PyCryptodome fehlt", code="A01"))


class DecryptTests(unittest.TestCase):
    """Ein gueltiger Block wird exakt gelesen, ein defekter nie."""

    def test_a_real_refresh_blob_is_decrypted_exactly(self):
        self.assertEqual(TOKEN_PLAIN, cryptojs_decrypt(TOKEN_BLOB))
        # Auch der Weg, den die Browser-Anmeldung nimmt.
        self.assertEqual(TOKEN_PLAIN, _decrypt_reader_blob(TOKEN_BLOB))

    def test_a_real_userinfos_blob_yields_device_id_and_refresh(self):
        self.assertEqual(USERINFOS_PLAIN, cryptojs_decrypt(USERINFOS_BLOB))
        self.assertEqual("hw-42", _hardware_from_reader_blob(USERINFOS_BLOB))
        self.assertEqual("tok-9", _refresh_field_text(USERINFOS_PLAIN))

    def test_malformed_blobs_are_rejected_without_an_exception(self):
        cases = (
            ("kein base64", "!!!!"),
            ("falscher Header",
             base64.b64encode(b"NotSalted" + b"x" * 32).decode()),
            ("kein Ciphertext",
             base64.b64encode(b"Salted__" + b"12345678").decode()),
            ("falsche Laenge",
             base64.b64encode(b"Salted__" + b"12345678" + b"\x00" * 15).decode()),
            ("falsches Padding", BAD_PADDING_BLOB),
            ("kein UTF-8", BAD_UTF8_BLOB),
            ("leer", ""),
        )
        for label, blob in cases:
            with self.subTest(label):
                self.assertEqual("", cryptojs_decrypt(blob))

    def test_without_the_decoder_only_real_blobs_raise(self):
        with no_crypto():
            with self.assertRaises(TolinoAuthError) as caught:
                _decrypt_reader_blob(TOKEN_BLOB)
            self.assertEqual("A01", caught.exception.code)
            # Ein danebenliegender Klartext ist kein Blob und braucht keinen
            # Decoder -- er bleibt unveraendert verwendbar.
            self.assertEqual("", _decrypt_reader_blob(TOKEN_PLAIN))


if __name__ == "__main__":
    unittest.main()
