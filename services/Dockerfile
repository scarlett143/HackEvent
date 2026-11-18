# One image for api, scheduler and workers; the Compose `command` picks the role.
# `--target browser` builds the Playwright server image (HE-027); the default (last) stage
# is the services image. Build context: repo root (needs pyproject.toml + uv.lock).
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.12-slim AS base
# Pick up Debian security fixes newer than the base image tag (Trivy gate in CI).
# libatomic1: Playwright's bundled Node driver needs it on arm64 (client and server).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_HTTP_TIMEOUT=120 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH=/app/.venv/bin:$PATH
RUN useradd --system --uid 10001 --no-create-home app

# Chromium + `playwright run-server`, same Playwright version as the client (uv.lock).
# No app code, no credentials; Compose keeps it on an internal-only network.
FROM base AS browser
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp
RUN playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/*
USER app
EXPOSE 3000
CMD ["playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]

FROM base AS services
COPY services ./services
# ALEMBIC_CONFIG lets `docker compose exec api alembic upgrade head` run from WORKDIR /app.
ENV PYTHONPATH=/app/services \
    ALEMBIC_CONFIG=/app/services/alembic.ini
USER app
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
