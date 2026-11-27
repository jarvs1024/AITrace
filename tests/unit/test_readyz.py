from __future__ import annotations

from fastapi.testclient import TestClient

from aitrace.api.app import create_app
from aitrace.core.settings import Settings


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_readyz_returns_200_when_ready(test_settings: Settings) -> None:
    with _client(test_settings) as client:
        response = client.get("/api/v1/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["version"]


def test_readyz_returns_503_when_stopping(test_settings: Settings) -> None:
    with _client(test_settings) as client:
        client.app.state.stopping = True
        response = client.get("/api/v1/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "stopping"}


def test_readyz_returns_503_before_lifespan_start(test_settings: Settings) -> None:
    client = _client(test_settings)  # 不进入 with：lifespan 未运行
    response = client.get("/api/v1/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "starting"}


def test_readyz_returns_503_when_db_unavailable(test_settings: Settings) -> None:
    with _client(test_settings) as client:

        def _broken_connect():
            raise RuntimeError("db down")

        client.app.state.engine.connect = _broken_connect  # type: ignore[method-assign]
        response = client.get("/api/v1/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "db_unavailable"}
