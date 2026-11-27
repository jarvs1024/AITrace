from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

CORE_TABLES = {
    "session",
    "round",
    "report",
    "batch",
    "task_queue",
    "user_queue",
    "case_library",
    "case_contribution",
    "memory_hot_file",
    "profile",
    "attachment",
    "audit_log",
}


def test_all_core_tables_exist(db_engine: Engine) -> None:
    assert set(inspect(db_engine).get_table_names()) >= CORE_TABLES


def test_task_idempotency_indexes(db_session: Session) -> None:
    db_session.execute(
        text(
            "INSERT INTO session (session_id, product_id, batch_id, case_id, domain_id, "
            "profile_version, trigger_user_id, hardware_type, latest_round_no, status, "
            "env_topology, git_context, created_at) VALUES "
            "('S1','p','b','c','d','v','u','SSD',1,'RUNNING','{}','{}',now())"
        )
    )
    for task_id, round_no in (("T1", 1), ("T2", 2)):
        db_session.execute(
            text(
                "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no, "
                "product_id, case_id, task_type, queue_key, queue_state, priority, attempts, "
                "max_attempts, lease_generation, lease_until, callback_status, payload, "
                "created_at, updated_at) VALUES "
                "(:task_id,'b','S1','R1',:round_no,'p','c','case_clean','u','PENDING',1,0,3,0,"
                "'epoch','PENDING','{}',now(),now())"
            ),
            {"task_id": task_id, "round_no": round_no},
        )
    db_session.commit()
    try:
        db_session.execute(
            text(
                "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no, "
                "product_id, case_id, task_type, queue_key, queue_state, priority, attempts, "
                "max_attempts, lease_generation, lease_until, callback_status, payload, "
                "created_at, updated_at) VALUES "
                "('T3','b','S1','R1',1,'p','c','case_clean','u','PENDING',1,0,3,0,"
                "'epoch','PENDING','{}',now(),now())"
            )
        )
        db_session.commit()
    except IntegrityError:
        db_session.rollback()
    else:
        raise AssertionError("duplicate (session_id, round_no, task_type) must be rejected")
