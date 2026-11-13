from fastapi import FastAPI

app = FastAPI(title="HackEvents API", docs_url=None, redoc_url=None)


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    # ponytail: liveness only; HE-002 adds db/redis/worker checks (Runbook Part D).
    return {"status": "ok"}
