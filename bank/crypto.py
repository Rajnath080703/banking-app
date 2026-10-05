"""Field-level encryption for sensitive customer data (encryption at rest).

PAN and mobile numbers are stored encrypted with Fernet (AES-128-CBC + HMAC).
A keyed SHA-256 hash is stored alongside so exact-match search still works
without decrypting every row.
"""
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

_fernet = None
_hash_key = None


def init_crypto(app):
    global _fernet, _hash_key
    key = os.environ.get("BANK_DATA_KEY")
    if not key:
        key_path = os.path.join(app.instance_path, "data.key")
        if os.path.exists(key_path):
            with open(key_path, "rb") as fh:
                key = fh.read().strip()
        else:
            key = Fernet.generate_key()
            with open(key_path, "wb") as fh:
                fh.write(key)
    if isinstance(key, str):
        key = key.encode()
    _fernet = Fernet(key)
    _hash_key = hashlib.sha256(b"search-index:" + key).digest()


def encrypt(value):
    if value is None or value == "":
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return "[unreadable]"


def search_hash(value):
    if not value:
        return None
    normalized = value.strip().upper().replace(" ", "")
    return hmac.new(_hash_key, normalized.encode(), hashlib.sha256).hexdigest()


def mask(value, visible=4):
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "•" * (len(value) - visible) + value[-visible:]
