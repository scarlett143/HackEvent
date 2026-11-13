"""HE-006: AES-256-GCM envelope crypto (Security doc §5). Keys are random per test run."""

import base64
import os

import pytest
from pydantic import ValidationError

from common import crypto
from common.config import Settings
from common.crypto import CryptoError, DecryptionError, WrongKeyError, decrypt, encrypt, rewrap

KEY = os.urandom(32)
OTHER = os.urandom(32)
AAD = crypto.aad("llm_providers", "api_key_enc", 42)
SECRET = b"fake-provider-api-key"


def test_round_trip() -> None:
    blob = encrypt(SECRET, AAD, KEY)
    assert SECRET not in blob
    assert blob[0] == crypto.VERSION
    assert len(blob) == len(SECRET) + 97
    assert decrypt(blob, AAD, KEY) == SECRET


def test_round_trip_empty_plaintext() -> None:
    assert decrypt(encrypt(b"", AAD, KEY), AAD, KEY) == b""


def test_wrong_master_key_fails() -> None:
    with pytest.raises(WrongKeyError):
        decrypt(encrypt(SECRET, AAD, KEY), AAD, OTHER)


def test_wrong_aad_fails() -> None:
    blob = encrypt(SECRET, AAD, KEY)
    for other in (
        crypto.aad("llm_providers", "api_key_enc", 43),
        crypto.aad("social_sessions", "cookies_enc", 42),
    ):
        with pytest.raises(DecryptionError):
            decrypt(blob, other, KEY)


# byte offsets: wrap_nonce 9, wrapped key 21, data nonce 69, ciphertext 81
@pytest.mark.parametrize("offset", [9, 21, 50, 69, 81, -1], ids=str)
def test_tampering_fails(offset: int) -> None:
    blob = bytearray(encrypt(SECRET, AAD, KEY))
    blob[offset] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt(bytes(blob), AAD, KEY)


def test_tampered_header_or_truncated_fails() -> None:
    blob = encrypt(SECRET, AAD, KEY)
    with pytest.raises(DecryptionError):
        decrypt(b"\x02" + blob[1:], AAD, KEY)
    with pytest.raises(WrongKeyError):
        decrypt(blob[:1] + bytes([blob[1] ^ 1]) + blob[2:], AAD, KEY)
    with pytest.raises(DecryptionError):
        decrypt(blob[:90], AAD, KEY)


def test_same_plaintext_encrypts_differently() -> None:
    a, b = encrypt(SECRET, AAD, KEY), encrypt(SECRET, AAD, KEY)
    assert a != b
    assert a[9:81] != b[9:81]  # fresh wrap nonce, data key and nonce


def test_rewrap_rotates_master_key() -> None:
    blob = encrypt(SECRET, AAD, KEY)
    new = rewrap(blob, KEY, OTHER)
    assert new[81:] == blob[81:]  # ciphertext untouched
    assert decrypt(new, AAD, OTHER) == SECRET
    with pytest.raises(WrongKeyError):
        decrypt(new, AAD, KEY)
    with pytest.raises(WrongKeyError):
        rewrap(blob, OTHER, KEY)


def test_bad_key_length_rejected() -> None:
    with pytest.raises(CryptoError):
        encrypt(SECRET, AAD, KEY[:16])


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.mark.parametrize("value", [_b64(os.urandom(16)), _b64(os.urandom(33)), "not base64!", ""])
def test_settings_rejects_bad_master_key(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"master_key": value})


def test_settings_master_key_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MASTER_KEY", _b64(KEY))  # loaded from the environment like prod
    settings = Settings()
    monkeypatch.setattr(crypto, "get_settings", lambda: settings)
    blob = encrypt(SECRET, AAD)
    assert decrypt(blob, AAD, KEY) == SECRET


def test_missing_master_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: Settings(master_key=None))
    with pytest.raises(CryptoError):
        encrypt(SECRET, AAD)


def test_master_key_not_in_repr_or_str() -> None:
    value = _b64(KEY)
    settings = Settings.model_validate({"master_key": value})
    for text in (
        repr(settings),
        str(settings),
        repr(settings.master_key),
        str(settings.model_dump()),
    ):
        assert value not in text
