from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from api import (
    admin,
    admin_audit,
    admin_changes,
    admin_llm,
    admin_review,
    admin_sources,
    admin_telemetry,
    auth,
    dashboard,
    events,
    google,
    health,
    invites,
    me,
    notifications,
    totp,
)
from api.csrf import CSRFMiddleware
from api.errors import ApiError, handle_api_error, handle_validation_error
from common.logging import configure_logging

# uvicorn runs its dictConfig before importing this module, so its handlers exist now.
configure_logging()

app = FastAPI(title="HackEvents API", docs_url=None, redoc_url=None, lifespan=health.lifespan)
app.add_exception_handler(ApiError, handle_api_error)
app.add_exception_handler(RequestValidationError, handle_validation_error)
app.add_middleware(CSRFMiddleware, session_cookie=auth.SESSION_COOKIE)
app.include_router(auth.router)
app.include_router(google.router)
app.include_router(totp.router)
app.include_router(admin.router)
app.include_router(admin_sources.router)
app.include_router(admin_telemetry.router)
app.include_router(admin_llm.router)
app.include_router(admin_review.router)
app.include_router(admin_audit.router)
app.include_router(admin_changes.router)
app.include_router(invites.router)
app.include_router(events.router)
app.include_router(me.router)
app.include_router(notifications.router)
app.include_router(dashboard.router)


@app.get("/api/v1/health/live")
def live() -> dict[str, str]:
    """Process liveness only; used by the Docker healthcheck."""
    return {"status": "ok"}


@app.get("/api/v1/health")
async def ready(request: Request, response: Response) -> dict[str, str]:
    """Dependency status. 503 if any dependency is down."""
    deps = request.app.state.deps
    result = {
        "db": await health.check_db(deps),
        "redis": await health.check_redis(deps),
        "workers": await health.check_workers(deps),
    }
    if not all(result.values()):
        response.status_code = 503
    return {k: "ok" if v else "down" for k, v in result.items()}
