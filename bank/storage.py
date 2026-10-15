"""Customer document storage.

Files are encrypted with the application data key (Fernet: AES-128-CBC + HMAC-SHA256)
before they touch disk, and stored under random names with no extension, so a copy of the
upload directory or a backup reveals nothing. Reads verify the HMAC, so tampered files are
rejected. The local-disk backend is used by default; set BANK_UPLOAD_DIR to a mounted
volume, or replace `_Backend` with an object-store implementation in a multi-server deployment.
"""
from __future__ import annotations

import os
import uuid

from cryptography.fernet import InvalidToken
from flask import current_app

from . import crypto

ALLOWED = {".pdf": ("application/pdf", (b"%PDF-",)),
           ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
           ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
           ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",))}
MAX_BYTES = 10 * 1024 * 1024


class StorageError(Exception):
    pass


def validate(filename: str, data: bytes) -> str:
    """Return the canonical extension, or raise: extension, size, and file signature must all agree."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED:
        raise StorageError("Only PDF, PNG or JPG files are accepted.")
    if not data:
        raise StorageError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise StorageError("Files must be 10 MB or smaller.")
    if not any(data.startswith(sig) for sig in ALLOWED[ext][1]):
        raise StorageError("The file's content does not match its type. Upload a real PDF, PNG or JPG.")
    if ext == ".pdf" and (b"/JavaScript" in data or b"/JS " in data or b"/Launch" in data):
        raise StorageError("PDFs containing scripts or launch actions are not accepted.")
    return ext


def mimetype_for(filename: str) -> str:
    return ALLOWED.get(os.path.splitext(filename or "")[1].lower(), ("application/octet-stream",))[0]


class _Backend:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, name):
        if not name or not name.replace("-", "").isalnum():  # no path traversal: names are uuid hex only
            raise StorageError("Invalid stored file name.")
        return os.path.join(self.root, name)

    def put(self, name, blob):
        tmp = self._path(name) + ".part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, self._path(name))

    def get(self, name):
        with open(self._path(name), "rb") as fh:
            return fh.read()


def _backend():
    return _Backend(current_app.config["UPLOAD_FOLDER"])


def save(filename: str, data: bytes) -> str:
    validate(filename, data)
    name = uuid.uuid4().hex
    _backend().put(name, crypto.encrypt_bytes(data))
    return name


def load(name: str) -> bytes:
    try:
        return crypto.decrypt_bytes(_backend().get(name))
    except FileNotFoundError as exc:
        raise StorageError("The stored file is missing.") from exc
    except InvalidToken as exc:
        raise StorageError("The stored file failed its integrity check and was not served.") from exc
