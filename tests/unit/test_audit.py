import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from aitrace.core.audit import AuditService


@pytest.fixture(autouse=True)
def _isolate_audit_log(db_engine: Engine) -> None:
    # 链校验对跨测试残留行敏感，每个测试前清空 audit_log 并重置自增 id。
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))


def test_append_rejects_float_detail(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    service.append(actor="a", action="ok", target="t", detail={"ok": True})
    for bad in (
        {"latency_s": 1.0},
        {"meta": {"ratio": 0.5}},
        {"items": [1, 2.0, 3]},
    ):
        with pytest.raises(ValueError) as caught:
            service.append(actor="a", action="bad", target="t", detail=bad)
        assert "浮点数" in str(caught.value)
    # 被拒绝的明细不能写入，也不能扰动已有哈希链。
    assert service.verify_chain() is True


def test_append_accepts_nested_jsonb_stable_detail(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    detail = {
        "nested": {"s": "ok", "i": 1, "b": True, "n": None},
        "items": [1, "two", False, None],
    }
    service.append(actor="a", action="x", target="t", detail=detail)
    assert service.verify_chain() is True
