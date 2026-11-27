# tests/unit/test_package.py
import aitrace


def test_package_exposes_version() -> None:
    assert aitrace.__version__ == "0.1.0"
