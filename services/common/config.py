"""Runtime settings loaded from the environment (TRD §6). Never log these values."""

import base64
import binascii
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+asyncpg://hackevents:hackevents@localhost:5432/hackevents"
    redis_url: str = "redis://localhost:6379/0"
    # `openssl rand -base64 32`. Optional so services that never touch secrets (health,
    # migrations) boot without it; common.crypto refuses to run when it is unset.
    master_key: SecretStr | None = None

    @field_validator("master_key")
    @classmethod
    def _master_key_is_32_bytes(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None:
            master_key_bytes(v)
        return v


def master_key_bytes(key: SecretStr) -> bytes:
    """Decode a base64 MASTER_KEY; raise ValueError unless it is exactly 32 bytes."""
    try:
        raw = base64.b64decode(key.get_secret_value(), validate=True)
    except binascii.Error:
        raise ValueError("MASTER_KEY must be base64 (openssl rand -base64 32)") from None
    if len(raw) != 32:
        raise ValueError("MASTER_KEY must decode to exactly 32 bytes")
    return raw


@lru_cache
def get_settings() -> Settings:
    return Settings()
