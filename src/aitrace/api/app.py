# src/aitrace/api/app.py
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aitrace.api.errors import install_error_handlers
from aitrace.api.routes import (
    dashboard,
    deliveries,
    feedback,
    health,
    monitor,
    session_ops,
    sessions,
)
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.core.audit import AuditService
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings, load_settings
from aitrace.observability.alerts import (
    dingtalk_text_poster,
    evaluate_alerts,
    next_alert_state,
    send_alerts,
)
from aitrace.observability.metrics import MetricsRegistry
from aitrace.observability.monitor import build_monitor_snapshot
from aitrace.profiles.loader import load_profiles
from aitrace.profiles.resolver import ProfileResolver
from aitrace.runtime.dispatcher import (
    build_ai_worker,
    build_callback_worker,
    build_cutoff_rules,
    build_notify_worker,
    run_tick,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    startup_checks: Callable[[], None] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if settings.service_token is None:
        raise RuntimeError("service_token must be resolved before create_app")
    registry = load_profiles(settings.storage.profile_domains_dir)
    engine = create_engine(
        settings.database.dsn,
        pool_pre_ping=True,
        pool_size=settings.database.pool_size,
        max_overflow=0,
        connect_args={"options": f"-c statement_timeout={settings.database.statement_timeout_ms}"},
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    stop_event = threading.Event()

    def _dispatch_loop() -> None:
        owner = f"dispatcher-{os.getpid()}"
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                with session_factory() as session:
                    run_tick(session, app.state, owner)
                    session.commit()
            except Exception:
                logger.exception("调度循环异常，本 tick 已随会话关闭回滚")
            stop_event.wait(max(settings.runtime.tick_seconds - (time.monotonic() - started), 0.1))

    monitor_stop = threading.Event()

    def _monitor_loop() -> None:
        alert_state: dict[str, Any] = {}
        while not monitor_stop.is_set():
            started = time.monotonic()
            try:
                with session_factory() as session:
                    snapshot = build_monitor_snapshot(
                        session,
                        app.state.metrics,
                        settings,
                        now=app.state.clock.now(),
                        audit=app.state.audit,
                    )
                app.state.audit_failed = snapshot["audit"]["verified"] is False
                now = app.state.clock.now()
                alerts = evaluate_alerts(snapshot, settings, now=now, prev_state=alert_state)
                alert_state = next_alert_state(snapshot, alert_state, now)
                send_alerts(
                    alerts,
                    ops_webhook=settings.monitor.ops_webhook,
                    poster=dingtalk_text_poster,
                )
            except Exception:
                logger.exception("监控巡检异常，本轮跳过（线程保持存活）")
            monitor_stop.wait(
                max(settings.monitor.check_interval_seconds - (time.monotonic() - started), 0.1)
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loop_thread: threading.Thread | None = None
        monitor_thread: threading.Thread | None = None
        if startup_checks is not None:
            startup_checks()
        app.state.ready = True
        if settings.runtime.enabled:
            loop_thread = threading.Thread(
                target=_dispatch_loop, name="aitrace-dispatcher", daemon=True
            )
            loop_thread.start()
        if settings.monitor.enabled:
            monitor_thread = threading.Thread(
                target=_monitor_loop, name="aitrace-monitor", daemon=True
            )
            monitor_thread.start()
        try:
            yield
        finally:
            app.state.stopping = True
            if monitor_thread is not None:
                monitor_stop.set()
                monitor_thread.join(timeout=10)
            if loop_thread is not None:
                stop_event.set()
                loop_thread.join(timeout=10)
            app.state.ready = False
            engine.dispose()

    app = FastAPI(
        title="AITrace",
        version="0.1.0",
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.ready = False
    app.state.stopping = False
    app.state.clock = SystemClock()
    app.state.ids = UuidIdGenerator()
    app.state.profile_resolver = ProfileResolver(registry)
    app.state.metrics = MetricsRegistry()
    app.state.audit_failed = False
    app.state.audit = AuditService(
        session_factory, blocked=lambda: bool(getattr(app.state, "audit_failed", False))
    )
    if settings.runtime.enabled:
        app.state.cutoff_rules = build_cutoff_rules(settings)
        app.state.cleaning_registry = CleaningRegistry()
        app.state.ai_worker = build_ai_worker(
            settings, ids=app.state.ids, metrics=app.state.metrics, audit=app.state.audit
        )
        app.state.callback_worker = build_callback_worker(settings, metrics=app.state.metrics)
        app.state.notify_worker = build_notify_worker(
            settings, clock=app.state.clock, metrics=app.state.metrics
        )
    install_error_handlers(app)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(deliveries.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(session_ops.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")
    app.include_router(feedback.router, prefix="/api/v1")
    app.include_router(monitor.router, prefix="/api/v1")
    return app
