# tools/export_contracts.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import SecretStr

from aitrace.api.app import create_app
from aitrace.api.schemas import DeliveryResponse, SessionStatusResponse
from aitrace.core.settings import DatabaseSettings, Settings

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def _app():
    settings = Settings(
        service_token=SecretStr("contract-export"),
        database=DatabaseSettings(dsn="postgresql+psycopg://unused:unused@127.0.0.1:1/unused"),
    )
    return create_app(settings)


def _schema(model: type) -> str:
    return (
        json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _error_schema() -> str:
    return (
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "AitraceError",
                "type": "object",
                "required": ["code", "message", "retryable"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "retryable": {"type": "boolean"},
                    "retry_after": {"type": ["integer", "null"]},
                    "details": {"type": "array", "items": {"type": "object"}},
                },
                "additionalProperties": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render() -> dict[Path, str]:
    openapi = _app().openapi()
    return {
        CONTRACTS / "aitrace-inbound.openapi.yaml": yaml.safe_dump(
            openapi, allow_unicode=True, sort_keys=False
        ),
        CONTRACTS / "schemas/delivery-response.schema.json": _schema(DeliveryResponse),
        CONTRACTS / "schemas/session-status.schema.json": _schema(SessionStatusResponse),
        CONTRACTS / "schemas/error.schema.json": _error_schema(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render()
    if args.check:
        drift = []
        for path, content in rendered.items():
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                drift.append(str(path.relative_to(ROOT)))
        if drift:
            print(
                "契约文件与代码不一致，请运行 python tools/export_contracts.py：" + ", ".join(drift)
            )
            return 1
        return 0
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"已生成 {len(rendered)} 个契约文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
