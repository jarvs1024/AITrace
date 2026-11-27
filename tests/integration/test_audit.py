from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from aitrace.core.audit import GENESIS_HASH, AuditService


@pytest.fixture(autouse=True)
def _isolate_audit_log(db_engine: Engine) -> None:
    # brief 测试直接用 db_engine（无 db_session 清理），为避免跨测试/跨轮次残留
    # 行（尤其伪造行）污染链校验，每个测试前清空 audit_log 并重置自增 id。
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))


def test_appends_form_a_valid_chain(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    first = service.append(
        actor="zhangsan", action="token.fetch", target="p/b/c", detail={"result": "ok"}
    )
    second = service.append(
        actor="zhangsan", action="token.fetch", target="p/b/c", detail={"result": "expired"}
    )
    assert first != second
    assert service.verify_chain() is True

    with db_engine.connect() as conn:
        rows = conn.execute(text("SELECT prev_hash, hash FROM audit_log ORDER BY id")).all()
    assert rows[0].prev_hash == GENESIS_HASH
    assert rows[1].prev_hash == rows[0].hash


def test_update_is_rejected_by_database_trigger(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    service.append(actor="a", action="x", target="t", detail={})
    with pytest.raises(DBAPIError) as caught, db_engine.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor='evil' WHERE id=1"))
    assert "append-only" in str(caught.value)


def test_verify_detects_a_forged_row(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    service.append(actor="a", action="x", target="t", detail={})
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (ts, actor, action, target, detail, prev_hash, hash) "
                "VALUES (:ts, 'forger', 'x', 't', '{}'::jsonb, :prev, repeat('0', 64))"
            ),
            {"ts": datetime.now(UTC), "prev": "0" * 64},
        )
    assert service.verify_chain() is False
