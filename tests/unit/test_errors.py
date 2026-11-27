from typing import Literal

from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aitrace.api.app import create_app
from aitrace.api.errors import describe_validation
from aitrace.core.errors import AitraceError
from aitrace.core.settings import Settings


def test_validation_message_uses_chinese_label_and_allowed_values() -> None:
    exc = RequestValidationError(
        [
            {
                "type": "literal_error",
                "loc": ("body", "hardware_type"),
                "msg": "Input should be 'SSD' or 'CXL'",
                "input": "HDD",
                "ctx": {"expected": "'SSD' or 'CXL'"},
            }
        ]
    )
    message, details = describe_validation(exc.errors())
    assert "硬件类型" in message
    assert "HDD" in message
    assert "合法取值" in message
    assert details[0]["field"] == "hardware_type"


def _app_with_routes() -> TestClient:
    app = create_app(Settings(service_token=SecretStr("test-token")))

    @app.get("/_test/aitrace-error")
    def _raise_aitrace_error() -> None:
        raise AitraceError("TOKEN_BUSY", "令牌繁忙", http_status=429, retryable=True, retry_after=5)

    @app.get("/_test/aitrace-error-plain")
    def _raise_plain_error() -> None:
        raise AitraceError("NOT_FOUND", "资源不存在", http_status=404)

    @app.get("/_test/probe")
    def _probe(hardware_type: Literal["SSD", "CXL"]) -> dict[str, str]:
        return {"hardware_type": hardware_type}

    return TestClient(app)


def test_unknown_query_value_returns_unified_400_body() -> None:
    with _app_with_routes() as client:
        response = client.get("/_test/probe", params={"hardware_type": "HDD"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PAYLOAD"
    assert body["retryable"] is False
    assert "硬件类型" in body["message"]
    assert "HDD" in body["message"]
    assert "合法取值" in body["message"]
    # describe_validation strips only the "body" prefix, so query errors keep their location.
    assert body["details"][0]["field"] == "query.hardware_type"


def test_aitrace_error_renders_unified_body_with_retry_after() -> None:
    with _app_with_routes() as client:
        response = client.get("/_test/aitrace-error")
    assert response.status_code == 429
    assert response.json() == {
        "code": "TOKEN_BUSY",
        "message": "令牌繁忙",
        "retryable": True,
        "retry_after": 5,
    }


def test_aitrace_error_omits_retry_after_when_absent() -> None:
    with _app_with_routes() as client:
        response = client.get("/_test/aitrace-error-plain")
    assert response.status_code == 404
    assert response.json() == {"code": "NOT_FOUND", "message": "资源不存在", "retryable": False}
