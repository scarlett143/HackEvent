"""Management dashboard API (HE-110, App Flow F9, doc 06 A6).

`GET /dashboard/summary`: Management + Admin only (doc 07 §3 "College dashboard"); a
professor gets 403. The numbers are college-wide by design: doc 07 §3 lets these two
roles see other users' registrations. Body: common.dashboard.summary.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.roles import require_role
from common.dashboard import Summary, summary
from common.models import User, UserRole

router = APIRouter(prefix="/api/v1")

Viewer = Annotated[User, Depends(require_role(UserRole.management, UserRole.admin))]
Db = Annotated[AsyncSession, Depends(get_db)]


@router.get("/dashboard/summary")
async def dashboard_summary(user: Viewer, db: Db) -> Summary:  # role: management, admin
    return await summary(db)
