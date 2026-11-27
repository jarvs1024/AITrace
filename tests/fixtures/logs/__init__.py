from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load_golden(name: str) -> str:
    path = _DIR / f"{name}.txt"
    if not path.is_file():
        raise ValueError(f"golden 日志缺失：{name}；期望文件 {path}")
    return path.read_text(encoding="utf-8", errors="replace")
