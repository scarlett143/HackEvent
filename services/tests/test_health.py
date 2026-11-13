from fastapi.testclient import TestClient

from api.main import app


def test_health() -> None:
    r = TestClient(app).get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
