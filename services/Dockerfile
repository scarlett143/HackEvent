# One image for api, scheduler and workers; the Compose `command` picks the role.
# Build context: repo root (needs pyproject.toml + uv.lock).
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.12-slim
# Pick up Debian security fixes newer than the base image tag (Trivy gate in CI).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
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

COPY services ./services
# ALEMBIC_CONFIG lets `docker compose exec api alembic upgrade head` run from WORKDIR /app.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/services \
    ALEMBIC_CONFIG=/app/services/alembic.ini

RUN useradd --system --uid 10001 --no-create-home app
USER app
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
