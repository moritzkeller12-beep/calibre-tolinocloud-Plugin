"""CryptoJS/OpenSSL AES-CBC decoding for Web Reader token blobs."""
import base64
import hashlib


# --- crypto-js-kompatibles AES (nur stdlib) -------------------------------
# CryptoJS.AES.encrypt(value, VERSION.PHRASE) -> Base64 in IndexedDB
# (userToken/userInfos): OpenSSL-"Salted__"-KDF, AES-256-CBC; jede
# Passphrase wird akzeptiert, auch eine kuenftig geaenderte.

_AES_SBOX = []
_AES_INV_SBOX = []
_AES_RCON = []


def _aes_init_tables():
    """Build AES S-box / inverse S-box / round constants once (stdlib only)."""
    if _AES_SBOX:
        return

    def xtime(a):
        a <<= 1
        return ((a ^ 0x1B) & 0xFF) if a & 0x100 else a

    def mul(a, b):
        result = 0
        while b:
            if b & 1:
                result ^= a
            a = xtime(a)
            b >>= 1
        return result & 0xFF

    # S-box: multiplicative inverse in GF(2^8) + affine transform.
    sbox = [0] * 256
    inverse_sbox = [0] * 256
    # log/antilog tables over the generator 0x03
    log_table = [0] * 256
    alog_table = [0] * 256
    value = 1
    for exponent in range(255):
        log_table[value] = exponent
        alog_table[exponent] = value
        value ^= xtime(value)  # multiply by generator 3: v*3 = v ^ (v*2)
    log_table[0] = None
    for value in range(256):
        if value == 0:
            inverse = 0
        else:
            inverse = alog_table[(255 - log_table[value]) % 255]
        transformed = inverse
        result = inverse
        for _ in range(4):
            result = ((result << 1) | (result >> 7)) & 0xFF
            transformed ^= result
        sbox[value] = transformed ^ 0x63
    for value, transformed in enumerate(sbox):
        inverse_sbox[transformed] = value
    rcon = [0x01]
    for _ in range(9):
        rcon.append(xtime(rcon[-1]))
    _AES_SBOX[:] = sbox
    _AES_INV_SBOX[:] = inverse_sbox
    _AES_RCON[:] = rcon


def _aes_expand_key(key):
    """Expand a 16/24/32-byte key into 4-byte round-key words."""
    _aes_init_tables()
    nk = len(key) // 4
    words = [list(key[i:i + 4]) for i in range(0, len(key), 4)]
    rounds = {16: 10, 24: 12, 32: 14}[len(key)]
    total = 4 * (rounds + 1)
    rcon_index = 0
    while len(words) < total:
        temp = list(words[-1])
        if len(words) % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[b] for b in temp]
            temp[0] ^= _AES_RCON[rcon_index]
            rcon_index += 1
        elif len(key) == 32 and len(words) % 4 == 0:
            temp = [_AES_SBOX[b] for b in temp]
        base = list(words[-nk])
        words.append([base[j] ^ temp[j] for j in range(4)])
    return words, rounds


def _aes_rounds(state, words, rounds, decrypt=False):
    """Run the AES round function over a 4x4 state (state[r][c] = in[4c+r])."""
    _aes_init_tables()

    def add_round_key(current, round_index):
        for c in range(4):
            for r in range(4):
                current[r][c] ^= words[round_index * 4 + c][r]

    def sub_bytes(current, inverse=False):
        box = _AES_INV_SBOX if inverse else _AES_SBOX
        for r in range(4):
            for c in range(4):
                current[r][c] = box[current[r][c]]

    def shift_rows(current, inverse=False):
        for r in range(1, 4):
            row = current[r]
            if not inverse:
                current[r] = row[r:] + row[:r]
            else:
                current[r] = row[-r:] + row[:-r]

    def xtime(v):
        v <<= 1
        return ((v ^ 0x1B) & 0xFF) if v & 0x100 else v

    def mul(v, m):
        result = 0
        while m:
            if m & 1:
                result ^= v
            v = xtime(v)
            m >>= 1
        return result & 0xFF

    def mix_columns(current, inverse=False):
        for c in range(4):
            a = [current[r][c] for r in range(4)]
            if not inverse:
                current[0][c] = mul(a[0], 2) ^ mul(a[1], 3) ^ a[2] ^ a[3]
                current[1][c] = a[0] ^ mul(a[1], 2) ^ mul(a[2], 3) ^ a[3]
                current[2][c] = a[0] ^ a[1] ^ mul(a[2], 2) ^ mul(a[3], 3)
                current[3][c] = mul(a[0], 3) ^ a[1] ^ a[2] ^ mul(a[3], 2)
            else:
                current[0][c] = (mul(a[0], 14) ^ mul(a[1], 11) ^
                                 mul(a[2], 13) ^ mul(a[3], 9))
                current[1][c] = (mul(a[0], 9) ^ mul(a[1], 14) ^
                                 mul(a[2], 11) ^ mul(a[3], 13))
                current[2][c] = (mul(a[0], 13) ^ mul(a[1], 9) ^
                                 mul(a[2], 14) ^ mul(a[3], 11))
                current[3][c] = (mul(a[0], 11) ^ mul(a[1], 13) ^
                                 mul(a[2], 9) ^ mul(a[3], 14))

    if not decrypt:
        add_round_key(state, 0)
        for round_index in range(1, rounds):
            sub_bytes(state)
            shift_rows(state)
            mix_columns(state)
            add_round_key(state, round_index)
        sub_bytes(state)
        shift_rows(state)
        add_round_key(state, rounds)
    else:
        add_round_key(state, rounds)
        for round_index in range(rounds - 1, 0, -1):
            shift_rows(state, inverse=True)
            sub_bytes(state, inverse=True)
            add_round_key(state, round_index)
            mix_columns(state, inverse=True)
        shift_rows(state, inverse=True)
        sub_bytes(state, inverse=True)
        add_round_key(state, 0)
    return state


def _state_from_block(block):
    """Load one 16-byte block into FIPS-197 state layout (columns first)."""
    return [[block[4 * c + r] for c in range(4)] for r in range(4)]


def _state_to_block(state):
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = state[r][c]
    return bytes(out)


def _aes_decrypt_block(block, words, rounds):
    """Decrypt one 16-byte block with AES."""
    return _state_to_block(
        _aes_rounds(_state_from_block(block), words, rounds, decrypt=True))


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
    words, rounds = _aes_expand_key(key)
    plain = b""
    previous = iv
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset:offset + 16]
        decrypted = _aes_decrypt_block(block, words, rounds)
        plain += bytes(a ^ b for a, b in zip(decrypted, previous))
        previous = block
    plain = _pkcs7_unpad(plain)
    if plain is None:
        return ""
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return ""

