import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_mock_never_imports_aitrace() -> None:
    for path in (ROOT / "mocks/aitest/src").rglob("*.py"):
        names = _imports(path)
        assert not any(name == "aitrace" or name.startswith("aitrace.") for name in names), path


def test_persistence_does_not_import_api() -> None:
    for path in (ROOT / "src/aitrace/persistence").rglob("*.py"):
        names = _imports(path)
        forbidden = any(name == "aitrace.api" or name.startswith("aitrace.api.") for name in names)
        assert not forbidden, path
