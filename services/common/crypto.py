"""AES-256-GCM envelope encryption for secrets at rest (Security doc §5).

Each value gets a fresh random 32-byte data key (DEK). The DEK encrypts the plaintext;
MASTER_KEY wraps the DEK. Blob layout (all fixed-width, total overhead 97 bytes):

    version   1  B   0x01
    key_id    8  B   fingerprint of the MASTER_KEY that wrapped the DEK
    wrap_nonce 12 B
    wrapped_dek 48 B  AESGCM(MASTER_KEY, wrap_nonce, DEK, aad=version|key_id)
    nonce     12 B
    ciphertext n+16 B AESGCM(DEK, nonce, plaintext, aad=caller AAD)

The caller AAD binds a value to its table/column/row (see `aad()`), so a ciphertext
copied to another row or column fails to decrypt. The DEK wrap is bound only to the
header, so `rewrap()` can rotate MASTER_KEY without knowing row AADs or plaintext.
"""

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common.config import get_settings, master_key_bytes

VERSION = 1
_NONCE = 12
_KEY = 32
_KEY_ID = 8
_HEADER = 1 + _KEY_ID
_WRAPPED = _KEY + 16
_PREFIX = _HEADER + _NONCE + _WRAPPED + _NONCE  # bytes before the ciphertext


class CryptoError(Exception):
    """Base class; never carries key or plaintext material."""


class WrongKeyError(CryptoError):
    """The blob was wrapped by a different MASTER_KEY than the one supplied."""


class DecryptionError(CryptoError):
    """The blob is malformed, tampered with, or the AAD does not match."""


def aad(table: str, column: str, row_id: object) -> bytes:
    """Canonical AAD for a DB column value, e.g. aad("llm_providers", "api_key_enc", id)."""
    return f"hackevents:{table}.{column}:{row_id}".encode()


def key_id(master_key: bytes) -> bytes:
    return hashlib.sha256(b"hackevents-master-key-id\x00" + master_key).digest()[:_KEY_ID]


def _default_key() -> bytes:
    key = get_settings().master_key
    if key is None:
        raise CryptoError("MASTER_KEY is not configured")
    return master_key_bytes(key)


def _check_key(master_key: bytes) -> None:
    if len(master_key) != _KEY:
        raise CryptoError("master key must be exactly 32 bytes")


def _wrap(master_key: bytes, dek: bytes) -> bytes:
    header = bytes([VERSION]) + key_id(master_key)
    wrap_nonce = os.urandom(_NONCE)
    return header + wrap_nonce + AESGCM(master_key).encrypt(wrap_nonce, dek, header)


def _unwrap(master_key: bytes, blob: bytes) -> bytes:
    if len(blob) < _PREFIX + 16 or blob[0] != VERSION:
        raise DecryptionError("unrecognised or truncated blob")
    header = blob[:_HEADER]
    if header[1:] != key_id(master_key):
        raise WrongKeyError("blob was wrapped by a different master key")
    wrap_nonce = blob[_HEADER : _HEADER + _NONCE]
    wrapped = blob[_HEADER + _NONCE : _HEADER + _NONCE + _WRAPPED]
    try:
        return AESGCM(master_key).decrypt(wrap_nonce, wrapped, header)
    except InvalidTag:
        raise DecryptionError("wrapped data key failed authentication") from None


def encrypt(plaintext: bytes, aad: bytes, master_key: bytes | None = None) -> bytes:
    master_key = _default_key() if master_key is None else master_key
    _check_key(master_key)
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(_NONCE)
    return _wrap(master_key, dek) + nonce + AESGCM(dek).encrypt(nonce, plaintext, aad)


def decrypt(blob: bytes, aad: bytes, master_key: bytes | None = None) -> bytes:
    master_key = _default_key() if master_key is None else master_key
    _check_key(master_key)
    dek = _unwrap(master_key, blob)
    nonce = blob[_PREFIX - _NONCE : _PREFIX]
    try:
        return AESGCM(dek).decrypt(nonce, blob[_PREFIX:], aad)
    except InvalidTag:
        raise DecryptionError("ciphertext failed authentication (tampered or wrong AAD)") from None


def rewrap(blob: bytes, old_key: bytes, new_key: bytes) -> bytes:
    """Re-wrap the data key under new_key; ciphertext is untouched (MASTER_KEY rotation)."""
    _check_key(old_key)
    _check_key(new_key)
    dek = _unwrap(old_key, blob)
    return _wrap(new_key, dek) + blob[_PREFIX - _NONCE :]
