"""Host identity: a persistent Ed25519 keypair and signed descriptors.

The private key never leaves the host's per-user data directory. Signatures
cover the room code, versions, protocol, TLS fingerprint, capabilities and a
timestamp/nonce so a directory entry can be tied to the host that served it.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

APP_DIR = "Blendermp"


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def identity_path() -> Path:
    return data_dir() / "identity.json"


def _crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None
    return Ed25519PrivateKey, Ed25519PublicKey, serialization


def load_or_create() -> dict | None:
    """Return ``{private, public, created}`` base64 strings, or None."""
    modules = _crypto()
    if modules is None:
        return None
    Ed25519PrivateKey, _Ed25519PublicKey, serialization = modules
    path = identity_path()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        private = base64.b64decode(str(body["private"]).encode("ascii"))
        public = base64.b64decode(str(body["public"]).encode("ascii"))
        if len(private) == 32 and len(public) == 32:
            return body
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    private_key = Ed25519PrivateKey.generate()
    private = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    body = {
        "version": 1,
        "created": round(time.time(), 3),
        "private": base64.b64encode(private).decode("ascii"),
        "public": base64.b64encode(public).decode("ascii"),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)
    return body


def canonical(descriptor: dict) -> bytes:
    return json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(identity: dict, descriptor: dict) -> str:
    modules = _crypto()
    if modules is None or not identity:
        return ""
    Ed25519PrivateKey, _Ed25519PublicKey, _serialization = modules
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(identity["private"]))
    return base64.b64encode(key.sign(canonical(descriptor))).decode("ascii")


def verify(public_key: str, descriptor: dict, signature: str) -> bool:
    modules = _crypto()
    if modules is None:
        return False
    _Ed25519PrivateKey, Ed25519PublicKey, _serialization = modules
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key.encode("ascii")))
        key.verify(base64.b64decode(signature.encode("ascii")), canonical(descriptor))
        return True
    except Exception:  # noqa: BLE001
        return False


def descriptor(code: str, app: str, addon: str, protocol: int, fingerprint: str, caps: int) -> dict:
    return {
        "code": str(code)[:64],
        "app": str(app)[:32],
        "addon": str(addon)[:32],
        "protocol": int(protocol),
        "cert_fingerprint": str(fingerprint)[:64],
        "caps": int(caps),
        "ts": int(time.time()),
        "nonce": base64.b16encode(os.urandom(8)).decode("ascii").lower(),
    }
