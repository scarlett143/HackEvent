"""Role checks and row ownership (HE-013, Security doc §3).

- `require_role(*roles)`: route dependency; a signed-in user outside `roles` gets 403.
  Use `current_user` for "any signed-in user". Admin routes use `api.totp.require_admin_2fa`
  (admin role via this dependency, plus a TOTP-verified session, doc 07 §4). Every /api route needs one of these (or a
  place on the public allow-list in tests/test_access.py), enforced by that test.
- `owned(stmt, model, user)`: scopes a select to `model.user_id == user.id`. Pass
  `bypass=` only where doc 07 §3 lets a role see other users' rows (e.g. Management and
  Admin see other users' registrations). `get_owned()` loads one row; other users' rows
  are 404, never 403, so ids don't leak (doc 07 §3).
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import current_user
from api.errors import ApiError
from common.models import User, UserRole


def require_role(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    if not roles:
        raise ValueError("require_role needs at least one role")
    allowed = frozenset(roles)

    async def check_role(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in allowed:
            raise ApiError(403, "forbidden", "You do not have access to this.")
        return user

    return check_role


def owned[T](
    stmt: Select[T], model: type[Any], user: User, *, bypass: tuple[UserRole, ...] = ()
) -> Select[T]:
    """Restrict `stmt` to rows of `model` owned by `user`, unless their role is in `bypass`."""
    if user.role in bypass:
        return stmt
    return stmt.where(model.user_id == user.id)


async def get_owned[M](
    db: AsyncSession,
    model: type[M],
    row_id: uuid.UUID,
    user: User,
    *,
    bypass: tuple[UserRole, ...] = (),
) -> M:
    """Load `model` row `row_id` if `user` may see it; 404 otherwise."""
    m: Any = model  # any ORM model with `id` and `user_id` columns
    row: M | None = await db.scalar(owned(select(m).where(m.id == row_id), m, user, bypass=bypass))
    if row is None:
        raise ApiError(404, "not_found", "Not found.")
    return row
