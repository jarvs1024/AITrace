from fastapi.testclient import TestClient
from pydantic import SecretStr

from aitrace.api.app import create_app
from aitrace.core.settings import Settings


def test_health_returns_ok() -> None:
    app = create_app(Settings(service_token=SecretStr("test-token")))
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "env": "dev"}
