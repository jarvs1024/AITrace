from __future__ import annotations

from datetime import datetime

from aitrace.cleaning.plugins import CleaningPlugin
from aitrace.cleaning.plugins.dmesg import DmesgPlugin
from aitrace.cleaning.plugins.generic import GenericPlugin
from aitrace.cleaning.plugins.scheduler import SchedulerPlugin
from aitrace.cleaning.plugins.testlog import TestlogPlugin
from aitrace.cleaning.plugins.uart import UartPlugin
from aitrace.cleaning.types import ParsedSource, SourceKind


class CleaningRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, CleaningPlugin] = {
            plugin.key: plugin
            for plugin in (
                TestlogPlugin(),
                UartPlugin(),
                DmesgPlugin(),
                SchedulerPlugin(),
                GenericPlugin(),
            )
        }

    def parse(self, source: str, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        if source == "image":
            return ParsedSource(
                SourceKind.IMAGE, tuple(text.splitlines()), metrics={"passthrough": True}
            )
        plugin = self._plugins.get(source)
        if plugin is None:
            raise ValueError(
                f"日志源不受支持：{source!r}；合法取值：test/uart/dmesg/scheduler/generic/image"
            )
        return plugin.parse(text, boot_utc=boot_utc)
