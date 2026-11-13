from fastapi import FastAPI, Request, Response

from api import auth, health
from api.errors import ApiError, handle_api_error
from common.logging import configure_logging

# uvicorn runs its dictConfig before importing this module, so its handlers exist now.
configure_logging()

app = FastAPI(title="HackEvents API", docs_url=None, redoc_url=None, lifespan=health.lifespan)
app.add_exception_handler(ApiError, handle_api_error)
app.include_router(auth.router)


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
