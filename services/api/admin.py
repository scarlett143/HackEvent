"""Admin API (doc 06 A7). The router-level `require_admin_2fa` gates every route (HE-014)."""

from fastapi import APIRouter, Depends

from api.totp import require_admin_2fa

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])


@router.get("/ping")
async def ping() -> dict[str, str]:  # role: admin + 2FA (router dependency)
    """Placeholder proving the /admin gate; real admin routes come with their tickets."""
    return {"status": "ok"}
