import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.eval
def test_eval_1_health_returns_ok():
    """EVAL-1 (BHV-1): every spec starts with `GET /health` -> 200 {"status": "ok"}."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
