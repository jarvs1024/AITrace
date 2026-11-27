# src/aitrace/runtime/dispatcher.py
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from aitrace.ai.qoder import CnSdkAdapter, FakeQoder
from aitrace.ai.worker import AIWorker
from aitrace.callback.worker import CallbackWorker
from aitrace.cleaning.rules import RuleSet
from aitrace.cleaning.worker import CleaningWorker
from aitrace.core.settings import Settings, SettingsError
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.batch_close import close_batch_if_done
from aitrace.scheduling.pause import abandon_stalled, recover_paused
from aitrace.scheduling.release import sweep_waiting
from aitrace.scheduling.router import route_ready_cleans

logger = logging.getLogger(__name__)


def build_cutoff_rules(settings: Settings) -> RuleSet:
    return RuleSet.load(settings.storage.cutoff_rules_file)


def build_ai_worker(
    settings: Settings,
    *,
    ids: Any,
    token_transport: httpx.BaseTransport | None = None,
) -> AIWorker:
    adapter: Any
    if settings.ai.edition == "fake":
        scenario = settings.ai.fake_scenario_file
        if scenario is None:
            raise SettingsError("AI edition=fake 时必须配置 ai.fake_scenario_file")
        events = json.loads(scenario.read_text(encoding="utf-8")).get("events", [])
        adapter = FakeQoder(events)
    else:
        adapter = CnSdkAdapter(settings.ai)
    return AIWorker(
        settings=settings.ai,
        aitest=settings.aitest,
        adapter=adapter,
        token_transport=token_transport,
        base_dir=settings.storage.data_dir,
        query_timeout=float(settings.ai.query_timeout_seconds),
        ids=ids,
    )


def build_callback_worker(settings: Settings) -> CallbackWorker:
    read_token = settings.aitest.read_token.get_secret_value() if settings.aitest.read_token else ""
    return CallbackWorker(
        client_factory=lambda: httpx.Client(
            timeout=settings.aitest.timeout_seconds, trust_env=False
        ),
        base_url=settings.aitest.base_url,
        read_token=read_token,
    )


def run_tick(session: Session, state: Any, owner: str) -> None:
    """一次调度 tick：清洗 → 路由 → AI → 释放 → 放弃/恢复 → 回调 → 关账。

    放弃在恢复之前执行：暂停过期已超过上限的队列（调度循环长时间停摆后重启的场景）
    按 TOKEN_REQUIRED 放弃任务；其余到期队列走正常恢复。
    """
    settings: Settings = state.settings
    now = state.clock.now()
    ids = state.ids
    queue = QueueRepository(session)

    CleaningWorker(queue, state.cleaning_registry, state.cutoff_rules).run_once(
        session, clock=state.clock, ids=ids, data_dir=settings.storage.data_dir, owner=owner
    )

    route_ready_cleans(session, queue, ids, now, settings.ai)

    if settings.aitest.read_token is not None:
        state.ai_worker.run_once(session, now, owner)

    sweep_waiting(session, queue, ids, now, settings.ai)
    abandon_stalled(session, queue, ids, now, cap_seconds=settings.ai.pause_abandon_seconds)
    recover_paused(session, queue, ids, now)

    if settings.aitest.read_token is not None:
        state.callback_worker.run_once(session, now, owner)

    close_batch_if_done(session, queue, ids, now)
