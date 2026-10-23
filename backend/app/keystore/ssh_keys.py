"""Разбор, нормализация и генерация приватных SSH-ключей."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from app.keystore.errors import InvalidKeyError

MAX_KEY_SIZE = 64 * 1024
MIN_RSA_BITS = 2048
GeneratedKeyType = Literal["ed25519", "rsa"]
SupportedKey = ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey


@dataclass(frozen=True)
class ParsedKey:
    key_type: str  # "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"
    bits: int
    public_key: str  # "ssh-ed25519 AAAA..." без комментария
    fingerprint: str  # "SHA256:...", как в `ssh-keygen -lf`
    private_key: bytes  # OpenSSH-формат без парольной фразы, для шифрования Fernet


def parse_private_key(data: bytes, passphrase: bytes | None = None) -> ParsedKey:
    """Читает ключ в формате OpenSSH или PEM (PKCS#1/PKCS#8/SEC1).

    Ключ с парольной фразой расшифровывается и сохраняется без неё: synex_worker
    принимает только путь к файлу и парольные фразы не поддерживает. Защиту даёт
    шифрование хранилища.
    """
    if len(data) > MAX_KEY_SIZE:
        raise InvalidKeyError("Private key is too large")
    text = data.replace(b"\r\n", b"\n").strip() + b"\n"

    if b"PuTTY-User-Key-File" in text:
        raise InvalidKeyError("PuTTY .ppk keys are not supported: export the key as OpenSSH")
    if b"BEGIN OPENSSH PRIVATE KEY" in text:
        loader = serialization.load_ssh_private_key
    elif b"-----BEGIN" in text and b"PRIVATE KEY-----" in text:
        loader = serialization.load_pem_private_key
    else:
        raise InvalidKeyError("Not a private key in OpenSSH or PEM format")

    try:
        try:
            key = loader(text, passphrase)
        except TypeError:
            if passphrase is None:
                raise InvalidKeyError(
                    "The key is protected by a passphrase: pass it in `passphrase`"
                ) from None
            key = loader(text, None)  # ключ без фразы, а фразу всё равно передали
    except InvalidKeyError:
        raise
    except ValueError as e:
        if passphrase is not None:
            raise InvalidKeyError("Wrong passphrase or corrupted key") from e
        raise InvalidKeyError(f"Cannot read private key: {e}") from e
    except UnsupportedAlgorithm as e:
        raise InvalidKeyError(f"Unsupported key: {e}") from e

    return describe(_supported(key))


def generate_private_key(key_type: GeneratedKeyType, bits: int | None = None) -> ParsedKey:
    if key_type == "ed25519":
        return describe(ed25519.Ed25519PrivateKey.generate())
    return describe(rsa.generate_private_key(public_exponent=65537, key_size=bits or 4096))


def describe(key: SupportedKey) -> ParsedKey:
    public_key = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    if isinstance(key, ed25519.Ed25519PrivateKey):
        bits = 256
    else:
        bits = key.key_size
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    return ParsedKey(
        key_type=public_key.split(" ", 1)[0],
        bits=bits,
        public_key=public_key,
        fingerprint=fingerprint(public_key),
        private_key=private_key,
    )


def fingerprint(public_key: str) -> str:
    blob = base64.b64decode(public_key.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


SSH_CURVES = frozenset({"secp256r1", "secp384r1", "secp521r1"})


def _supported(key: object) -> SupportedKey:
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in SSH_CURVES:
            raise InvalidKeyError(f"Unsupported ECDSA curve {key.curve.name}")
        return key
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < MIN_RSA_BITS:
            raise InvalidKeyError(f"RSA keys shorter than {MIN_RSA_BITS} bits are not accepted")
        return key
    raise InvalidKeyError(f"Unsupported key type {type(key).__name__}: use Ed25519, RSA or ECDSA")
