"""Audit log writer (Security doc §11, schema doc 05 `audit_log`).

`record()` adds one row to the caller's session and flushes; the caller owns the
transaction, so the audit row commits or rolls back together with the change it
describes. `meta` never stores secrets: keys named like secrets are dropped (same
key-name rule as the HE-007 log filter), string values go through `redact()`, and the
result is size-capped.
"""

import enum
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import _SECRET_KEY_RE, redact
from common.models import AuditLog

MAX_STRING = 512  # chars per string value, applied after redaction
MAX_META_BYTES = 4096  # serialised JSON; larger meta is replaced by a marker
MAX_DEPTH = 5
TRUNCATED = "[TRUNCATED]"


class AuditAction(enum.StrEnum):
    """Closed list from Security doc §11. Add a member here before logging a new action."""

    LOGIN_SUCCESS = "auth.login_success"
    LOGIN_FAIL = "auth.login_fail"
    INVITE_CREATE = "invite.create"
    INVITE_ACCEPT = "invite.accept"
    USER_ROLE_CHANGE = "user.role_change"
    USER_DEACTIVATE = "user.deactivate"
    PROVIDER_ADD = "provider.add"
    PROVIDER_EDIT = "provider.edit"
    PROVIDER_DELETE = "provider.delete"
    CAP_CHANGE = "cap.change"
    SOCIAL_SESSION_UPLOAD = "social_session.upload"
    SOCIAL_SESSION_DELETE = "social_session.delete"
    SOURCE_CREATE = "source.create"
    SOURCE_EDIT = "source.edit"
    SOURCE_DELETE = "source.delete"
    REVIEW_DECISION = "review.decision"
    BACKUP_RESTORE = "backup.restore"


def _clean(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        return {
            str(k): _clean(v, depth + 1)
            for k, v in value.items()
            if not _SECRET_KEY_RE.fullmatch(str(k))
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_clean(v, depth + 1) for v in value]
    # Redact before truncating so a cut can never turn a secret into an unmatched fragment.
    text = redact(str(value))  # str() covers UUID, datetime, enums ...
    return text if len(text) <= MAX_STRING else text[:MAX_STRING] + TRUNCATED


def sanitize_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of `meta` with no secret keys or secret-shaped values."""
    clean: dict[str, Any] = _clean(meta, 0)
    size = len(json.dumps(clean, ensure_ascii=False).encode())
    if size > MAX_META_BYTES:
        return {"_truncated": True, "_bytes": size}
    return clean


async def record(
    session: AsyncSession,
    *,
    action: AuditAction,
    actor_id: uuid.UUID | None,
    target_type: str,
    target_id: uuid.UUID | None,
    ip: str,
    meta: dict[str, Any],
) -> AuditLog:
    """Insert an audit row inside the caller's transaction (flushed, not committed).

    Raises ValueError for an action outside `AuditAction`.
    """
    row = AuditLog(
        action=AuditAction(action).value,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        ip=ip,
        meta=sanitize_meta(meta),
    )
    session.add(row)
    await session.flush()
    return row
