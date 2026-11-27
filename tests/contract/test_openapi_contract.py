# tests/contract/test_openapi_contract.py
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_contract_files_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "tools/export_contracts.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_paths_are_frozen() -> None:
    openapi_text = (ROOT / "contracts/aitrace-inbound.openapi.yaml").read_text(encoding="utf-8")
    assert "/api/v1/aitrace/tasks:" in openapi_text
    assert "/api/v1/aitrace/tasks/{session_id}:" in openapi_text
    assert "/api/v1/health:" in openapi_text


def _keys(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key).lower())
            found |= _keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= _keys(value)
    return found


def test_no_token_body_fields() -> None:
    forbidden = {"qoder_access_token", "access_token", "token_value", "token_body"}
    for path in (ROOT / "contracts/schemas").glob("*.json"):
        assert not (forbidden & _keys(json.loads(path.read_text(encoding="utf-8")))), path.name
    openapi = (ROOT / "contracts/aitrace-inbound.openapi.yaml").read_text(encoding="utf-8")
    for name in forbidden:
        assert name not in openapi
