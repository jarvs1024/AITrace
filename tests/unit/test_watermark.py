from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from aitrace.core.settings import Settings, StorageSettings
from aitrace.persistence.models import Batch, Round, SessionModel
from aitrace.storage.watermark import WatermarkLevel, disk_fraction, evict, level_for


def _settings(tmp_path: Path, **storage: object) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    storage_defaults: dict[str, object] = {"data_dir": data_dir}
    storage_defaults.update(storage)
    return Settings(storage=StorageSettings(**storage_defaults))


def test_level_for_boundaries() -> None:
    assert level_for(0.69, 0.70, 0.85) is WatermarkLevel.OK
    assert level_for(0.70, 0.70, 0.85) is WatermarkLevel.WARN
    assert level_for(0.849, 0.70, 0.85) is WatermarkLevel.WARN
    assert level_for(0.85, 0.70, 0.85) is WatermarkLevel.FORCE


def test_disk_fraction_uses_quota_over_physical_disk(tmp_path: Path) -> None:
    settings = _settings(tmp_path, l2_quota_bytes=1000)
    (settings.storage.data_dir / "rawlogs").mkdir(parents=True)
    (settings.storage.data_dir / "rawlogs" / "f.log").write_bytes(b"x" * 300)
    fraction = disk_fraction(
        settings.storage.data_dir, settings.storage.data_dir / "rawlogs", quota_bytes=1000
    )
    assert 0.29 < fraction < 0.31


def test_disk_fraction_without_quota_reflects_physical(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.storage.data_dir / "rawlogs").mkdir(parents=True)
    (settings.storage.data_dir / "rawlogs" / "f.log").write_bytes(b"x" * 300)
    fraction = disk_fraction(settings.storage.data_dir, settings.storage.data_dir / "l2", None)
    assert 0.0 < fraction < 1.0


def test_disk_fraction_missing_data_dir_returns_zero(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data_dir = settings.storage.data_dir
    data_dir.rmdir()
    fraction = disk_fraction(data_dir, data_dir / "rawlogs", None)
    assert fraction == 0.0


def _seed_round(
    tmp_path: Path,
    db_session: Session,
    db_engine: Engine,
    settings: Settings,
    *,
    session_id: str,
    batch_id: str,
    round_id: str,
    round_status: str,
    batch_terminal: bool,
    finished_days_ago: int,
) -> None:
    now = datetime.now(UTC)
    db_session.add(
        SessionModel(
            session_id=session_id,
            product_id="ssd_9560",
            batch_id=batch_id,
            case_id=f"CASE-{session_id}",
            domain_id="d1",
            profile_version="v1",
            trigger_user_id="u1",
            hardware_type="SSD",
            env_topology={},
            git_context={},
            latest_round_no=1,
            status="RUNNING",
            created_at=now,
        )
    )
    db_session.add(
        Batch(
            batch_id=batch_id,
            product_id="ssd_9560",
            trigger_user_id="u1",
            batch_task_id=f"T-{batch_id}",
            failed_cases=[],
            passed_cases=[],
            batch_state="COMPLETED" if batch_terminal else "RUNNING",
            updated_at=now,
        )
    )
    # 显式先落地 session/batch，规避 ORM 同批 flush 时 round 与 session 的
    # 插入排序不确定（生产链路同样先写 session 再写 round）。
    db_session.flush()
    db_session.add(
        Round(
            round_id=round_id,
            session_id=session_id,
            round_no=1,
            trigger_user_id="u1",
            queue_key="u1",
            stage="COMPLETED",
            round_status=round_status,
            started_at=now,
            finished_at=now - timedelta(days=finished_days_ago),
        )
    )
    db_session.commit()
    round_dir = settings.storage.data_dir / "evidence" / session_id / "round_1"
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "summary.json").write_text("{}", encoding="utf-8")
    mtime = (now - timedelta(days=finished_days_ago)).timestamp()
    os.utime(round_dir / "summary.json", (mtime, mtime))
    os.utime(round_dir, (mtime, mtime))


def test_evict_warn_mode_only_removes_ttl_class(
    tmp_path: Path, db_session: Session, db_engine: Engine
) -> None:
    settings = _settings(tmp_path, l1_ttl_days=10, l2_ttl_days=10)
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-TTL",
        batch_id="B-TTL",
        round_id="R-TTL",
        round_status="VALID",
        batch_terminal=True,
        finished_days_ago=40,
    )
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-AWAIT",
        batch_id="B-AWAIT",
        round_id="R-AWAIT",
        round_status="AWAITING_FEEDBACK",
        batch_terminal=True,
        finished_days_ago=1,
    )
    report = evict(db_session, settings, force=False, now=datetime.now(UTC))
    assert report.by_reason == {"ttl_expired": 1}
    assert not (settings.storage.data_dir / "evidence" / "S-TTL").exists()
    assert (settings.storage.data_dir / "evidence" / "S-AWAIT" / "round_1").exists()
    assert report.freed_bytes > 0


def test_evict_force_priority_order(tmp_path: Path, db_session: Session, db_engine: Engine) -> None:
    settings = _settings(tmp_path, l1_ttl_days=10, l2_ttl_days=10)
    now = datetime.now(UTC)
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-AWAIT",
        batch_id="B-AWAIT",
        round_id="R-AWAIT",
        round_status="AWAITING_FEEDBACK",
        batch_terminal=True,
        finished_days_ago=1,
    )
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-INVALID",
        batch_id="B-INVALID",
        round_id="R-INVALID",
        round_status="INVALID",
        batch_terminal=True,
        finished_days_ago=1,
    )
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-VALID",
        batch_id="B-VALID",
        round_id="R-VALID",
        round_status="VALID",
        batch_terminal=True,
        finished_days_ago=1,
    )
    report = evict(db_session, settings, force=True, now=now)
    # 淘汰优先级：未评价 < 无效 < 有效（超TTL类为空数据场景下不出现）
    assert list(report.by_reason) == ["unreviewed", "invalid", "valid"]
    assert report.removed_dirs == 3


def test_evict_never_touches_running_batch(
    tmp_path: Path, db_session: Session, db_engine: Engine
) -> None:
    settings = _settings(tmp_path, l1_ttl_days=1, l2_ttl_days=1)
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-RUN",
        batch_id="B-RUN",
        round_id="R-RUN",
        round_status="VALID",
        batch_terminal=False,
        finished_days_ago=40,
    )
    report = evict(db_session, settings, force=True, now=datetime.now(UTC))
    assert report.removed_dirs == 0
    assert (settings.storage.data_dir / "evidence" / "S-RUN" / "round_1").exists()


def test_evict_rawlogs_old_dir_for_terminal_batch(
    tmp_path: Path, db_session: Session, db_engine: Engine
) -> None:
    settings = _settings(tmp_path, l1_ttl_days=30, l2_ttl_days=10)
    _seed_round(
        tmp_path,
        db_session,
        db_engine,
        settings,
        session_id="S-RL",
        batch_id="B-RL",
        round_id="R-RL",
        round_status="VALID",
        batch_terminal=True,
        finished_days_ago=1,
    )
    rawlogs_dir = settings.storage.data_dir / "rawlogs" / "B-RL"
    rawlogs_dir.mkdir(parents=True)
    (rawlogs_dir / "x.log").write_bytes(b"x" * 100)
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(rawlogs_dir / "x.log", (old, old))
    os.utime(rawlogs_dir, (old, old))
    report = evict(db_session, settings, force=False, now=datetime.now(UTC))
    assert report.by_reason == {"ttl_expired": 1}
    assert not rawlogs_dir.exists()
