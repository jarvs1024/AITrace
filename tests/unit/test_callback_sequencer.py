from aitrace.callback.sequencer import next_batch_seq, next_session_seq


def test_session_seq_monotonic(db_session) -> None:
    assert next_session_seq(db_session, "s1", 1) == 1
    assert next_session_seq(db_session, "s1", 1) == 2
    assert next_session_seq(db_session, "s1", 2) == 1  # 不同轮次独立


def test_batch_seq_monotonic(db_session) -> None:
    assert next_batch_seq(db_session, "B1") == 1
    assert next_batch_seq(db_session, "B1") == 2
    assert next_batch_seq(db_session, "B2") == 1
