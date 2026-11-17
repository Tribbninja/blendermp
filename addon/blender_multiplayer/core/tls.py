"""Standard TLS 1.3 for every session connection.

There is deliberately no application-level encryption in this project. TLS does
the confidentiality, integrity and nonce handling; we only generate a
self-signed certificate per host and pin its SHA-256 fingerprint on the client.

Trust model
-----------
* The host owns a long-lived self-signed certificate in its per-user data
  directory. The private key is written with owner-only permissions.
* A joiner pins the certificate fingerprint on first use (TOFU) and shows a short
  verification code. Later connections to the same room code must present the
  same fingerprint or are rejected.
* An invite (QR/link) may carry the expected fingerprint, removing TOFU entirely.
* No key material is derived from the room code, and no secret is hardcoded.

The fingerprint is checked immediately after the TLS handshake and before any
application frame is written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import ssl
from pathlib import Path

try:  # pragma: no cover - exercised through the optional dependency
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    AVAILABLE = True
except Exception:  # noqa: BLE001
    x509 = None  # type: ignore[assignment]
    hashes = None  # type: ignore[assignment]
    serialization = None  # type: ignore[assignment]
    ec = None  # type: ignore[assignment]
    NameOID = None  # type: ignore[assignment]
    AVAILABLE = False

ALPN = "bmp/1"
CERT_NAME = "host-cert.pem"
KEY_NAME = "host-key.pem"
FINGERPRINT_CHARS = 16


class TlsError(RuntimeError):
    pass


def fingerprint(der: bytes) -> str:
    """SHA-256 of the DER certificate, lowercase hex."""
    return hashlib.sha256(der).hexdigest()


def short_fingerprint(value: str) -> str:
    """A human-comparable chunk of the fingerprint, e.g. ``A1B2 C3D4 E5F6 7890``."""
    text = value.replace(":", "").upper()[:FINGERPRINT_CHARS]
    return " ".join(text[i : i + 4] for i in range(0, len(text), 4))


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows ACLs already scope %LOCALAPPDATA% to the current user.
        pass


def load_or_create_certificate(directory: Path, common_name: str = "blendermp") -> tuple[Path, Path, str]:
    """Return ``(cert_path, key_path, fingerprint)``, creating a cert if needed.

    Certificates are valid for 30 days and rotated when fewer than 7 days remain,
    so TOFU pinning stays stable for normal use without keeping a key forever.
    """
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CERT_NAME
    key_path = directory / KEY_NAME

    if cert_path.exists() and key_path.exists():
        try:
            der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
            current = x509.load_der_x509_certificate(der)
            remaining = current.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
            if remaining > dt.timedelta(days=7):
                return cert_path, key_path, fingerprint(der)
        except Exception:  # noqa: BLE001 - regenerate on any parse problem
            pass

    if not AVAILABLE:
        raise TlsError(
            "certificate generation needs the 'cryptography' package; "
            "install it into Blender's Python (see README)"
        )

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    _restrict(key_path)
    _restrict(cert_path)
    der = certificate.public_bytes(serialization.Encoding.DER)
    return cert_path, key_path, fingerprint(der)


def server_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    context.set_alpn_protocols([ALPN])
    return context


def client_context() -> ssl.SSLContext:
    """TLS 1.3 client context.

    Verification mode is disabled here only because the peer certificate is
    pinned by fingerprint immediately after the handshake, before any data is
    sent. That is standard certificate pinning, not a disabled security check.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols([ALPN])
    return context


def peer_fingerprint(writer) -> str | None:
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    der = ssl_object.getpeercert(binary_form=True)
    return fingerprint(der) if der else None


def verify_fingerprint(actual: str | None, expected: str) -> bool:
    if not actual or not expected:
        return False
    return hmac.compare_digest(actual.lower(), expected.replace(":", "").lower())


# --------------------------------------------------------------------------- TOFU
def _known_hosts_path(directory: Path) -> Path:
    return directory / "known-hosts.json"


def known_fingerprint(directory: Path, code: str) -> str | None:
    path = _known_hosts_path(directory)
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    value = body.get(code.replace("-", "").upper())
    return value if isinstance(value, str) and len(value) == 64 else None


def remember_fingerprint(directory: Path, code: str, value: str) -> None:
    path = _known_hosts_path(directory)
    body: dict[str, str] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                body = {k: v for k, v in existing.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            body = {}
    body[code.replace("-", "").upper()] = value.lower()
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    _restrict(path)


def forget_fingerprint(directory: Path, code: str) -> None:
    path = _known_hosts_path(directory)
    if not path.exists():
        return
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(body, dict):
        body.pop(code.replace("-", "").upper(), None)
        path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
