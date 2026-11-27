from __future__ import annotations

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.audit import AuditService
from aitrace.core.errors import AitraceError
from aitrace.persistence.models import AuditLog


def _service(db_engine: Engine) -> AuditService:
    return AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))


def test_append_blocked_raises_503(db_engine: Engine, db_session: Session) -> None:
    svc = _service(db_engine)
    with pytest.raises(AitraceError) as err:
        svc.append(actor="t", action="x", target="y", detail={}, blocked=lambda: True)
    assert err.value.code == "AUDIT_CHAIN_BROKEN"
    assert err.value.http_status == 503
    assert err.value.message.startswith("审计哈希链校验失败")
    with sessionmaker(bind=db_engine)() as session:
        assert session.execute(select(AuditLog)).scalars().first() is None


def test_append_blocked_false_writes(db_engine: Engine, db_session: Session) -> None:
    svc = _service(db_engine)
    digest = svc.append(
        actor="t",
        action="x",
        target="y",
        detail={"k": "v"},
        blocked=lambda: False,
    )
    assert len(digest) == 64
    with sessionmaker(bind=db_engine)() as session:
        assert session.execute(select(AuditLog)).scalars().first() is not None


def test_constructor_blocked_injection(db_engine: Engine, db_session: Session) -> None:
    svc = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False), blocked=lambda: True)
    with pytest.raises(AitraceError) as err:
        svc.append(actor="t", action="x", target="y", detail={})
    assert err.value.code == "AUDIT_CHAIN_BROKEN"


def test_append_param_overrides_constructor_blocked(db_engine: Engine, db_session: Session) -> None:
    svc = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False), blocked=lambda: True)
    svc.append(actor="t", action="x", target="y", detail={}, blocked=lambda: False)
    with sessionmaker(bind=db_engine)() as session:
        assert session.execute(select(AuditLog)).scalars().first() is not None


def test_verify_chain_result_cached_60s(db_engine: Engine, db_session: Session) -> None:
    svc = _service(db_engine)
    svc.append(actor="t", action="x", target="y", detail={})
    assert svc.verify_chain() is True
    # audit_log 表有 append-only 触发器，UPDATE 被数据库拒绝；用插入断链行模拟篡改
    with sessionmaker(bind=db_engine)() as session:
        session.execute(
            text(
                "INSERT INTO audit_log (ts, actor, action, target, detail, prev_hash, hash) "
                "VALUES (now(), 'evil', 'tamper', 'x', '{}'::jsonb, :prev, :hash)"
            ),
            {"prev": "1" * 64, "hash": "2" * 64},
        )
        session.commit()
    assert svc.verify_chain() is True  # 60s 内复用缓存
    svc._last_verify_at = None  # 模拟缓存过期
    assert svc.verify_chain() is False
