"""Lichte, machine-gebonden versleuteling voor gevoelige instellingen.

Dit is GEEN echte beveiliging: het doel is dat wachtwoorden en API-keys niet
leesbaar in ebookarr.db staan als iemand er toevallig in kijkt. De sleutel
wordt afgeleid van de Windows MachineGuid, dus een gekopieerde database is op
een andere pc niet te ontsleutelen (dan val je terug op opnieuw invoeren).

Alleen standaardbibliotheek — geen extra dependencies.
"""
import base64
import hashlib
import hmac
import os

_PREFIX = "enc:v1:"
_APP_SALT = b"ebookarr.v2.settings.salt"


def _machine_secret() -> bytes:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(guid).encode()
    except OSError:
        # Niet-Windows of geen toegang: vaste fallback (nog steeds "niet leesbaar
        # in één oogopslag", maar niet machine-gebonden).
        return b"ebookarr-no-machine-guid-fallback"


def _derive_key() -> bytes:
    return hashlib.pbkdf2_hmac("sha256", _machine_secret(), _APP_SALT, 200_000)


def _keystream(length: int, nonce: bytes, label: bytes) -> bytes:
    key = _derive_key()
    out = b""
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key, label + nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        out += block
        counter += 1
    return out[:length]


def encrypt(plaintext: str) -> str:
    if plaintext is None or plaintext == "":
        return ""
    data = plaintext.encode("utf-8")
    nonce = os.urandom(12)
    stream = _keystream(len(data), nonce, b"data")
    ciphertext = bytes(a ^ b for a, b in zip(data, stream, strict=True))
    mac = hmac.new(
        _keystream(32, nonce, b"mac"), ciphertext, hashlib.sha256
    ).digest()[:16]
    return _PREFIX + base64.b64encode(nonce + mac + ciphertext).decode("ascii")


def decrypt(value: str) -> str:
    if not value or not value.startswith(_PREFIX):
        # Oud, nog niet versleuteld opgeslagen wachtwoord: geef ongewijzigd terug.
        return value or ""
    raw = base64.b64decode(value[len(_PREFIX):])
    nonce, mac, ciphertext = raw[:12], raw[12:28], raw[28:]
    expected = hmac.new(
        _keystream(32, nonce, b"mac"), ciphertext, hashlib.sha256
    ).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise ValueError(
            "Kan een opgeslagen waarde niet ontsleutelen (database van een "
            "andere pc?). Vul de betreffende instelling opnieuw in."
        )
    stream = _keystream(len(ciphertext), nonce, b"data")
    return bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True)).decode("utf-8")


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)
