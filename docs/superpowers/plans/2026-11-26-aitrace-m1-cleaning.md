# M1 阶段一清洗管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现阶段一确定性清洗管线全链路：四源脱水插件、首错定位、跨源时间对齐与锚点级联、大小感知三段切片、双层指纹与批内去重、L1 产物落盘与哈希、截流闸规则报告、清洗 Worker（claim→pipeline→finish）、聚合闸确定性机制，全部功能点单测覆盖。

**Architecture:** `src/aitrace/cleaning/` 模块承载全部确定性逻辑：插件注册表（registry）按源键分派解析器；pipeline 串联 首错→对齐→锚点→切片→指纹→规则 产出 `PipelineOutcome`；artifacts 写 L1（`{data_dir}/evidence/{session_id}/round_{n}/`）；worker 用 M0 的 `claim_clean/finish/retry` 跑单步 `run_once`。聚合闸在 `scheduling/aggregate.py`：批内按稳定指纹分组、跟随者 `WAITING` 挂起（`depends_on_task_id` 指向首例）、首例 FAILED 晋升、引用门槛纯函数与级联改写纯函数。AI 与令牌不进入 M1。

**Tech Stack:** Python 3.13、SQLAlchemy 2.0、PostgreSQL 16、pytest、ruff/mypy。

**Spec:** [docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md](docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md)（§4.1 全节、§4.4.2 证据与字段规格、§4.5.4、§5.1、§6.5.5；V1.2 实施修订附录 B）

## Global Constraints

- **不提交**：全程不做任何 git 提交（用户约束）；每个任务的 Commit checkpoint 步骤一律跳过。
- **确定性红线**：M1 只实现确定性加工，不引入 AI/网络调用；解析器行为由 golden fixture 测试锁定。
- **M0 接口冻结**：不得改 `QueueRepository`（`claim_clean/finish/retry/reclaim_confirmed/claim_ai`）签名与语义、`NewTask`、`TaskQueue`/`Round`/`SessionModel` 既有列语义、投递契约、状态名。新增列/字段只允许：`Batch.gate`（JSONB）、`StorageSettings.cutoff_rules_file`。
- **令牌红线不变**：M1 不接触 qoder 令牌；规则报告与产物不落任何凭据字段。
- **中文自解释报错**：所有校验失败与清洗失败错误带中文字段名/当前值/合法取值。
- **权威信息优先**：设备标识（device_id/SN/槽位）一律取投递载荷 `env_binding`（aitest 权威），日志仅作核对；指纹输入用权威值。
- **时间口径**：全链路 UTC；`failed_at_utc` 为 int 毫秒时间戳。
- **命令**：`make test`（含集成）、`make lint`（ruff+mypy）每任务必绿；测试数据库 `aitrace_test`（Makefile 默认 TEST_DATABASE_URL）。

---

### Task 1: Cleaning plugin registry and source parsers

**Files:**
- Create: `src/aitrace/cleaning/__init__.py`
- Create: `src/aitrace/cleaning/types.py`
- Create: `src/aitrace/cleaning/registry.py`
- Create: `src/aitrace/cleaning/plugins/__init__.py`
- Create: `src/aitrace/cleaning/plugins/testlog.py`
- Create: `src/aitrace/cleaning/plugins/uart.py`
- Create: `src/aitrace/cleaning/plugins/dmesg.py`
- Create: `src/aitrace/cleaning/plugins/scheduler.py`
- Create: `src/aitrace/cleaning/plugins/generic.py`
- Test: `tests/unit/test_cleaning_plugins.py`

**Interfaces:**
- Consumes: 无（M1 首个任务）。
- Produces: `ParsedSource`/`Event`/`KeyLine`/`SourceKind`（types.py）；`CleaningRegistry.parse(source, text, *, boot_utc=None) -> ParsedSource`（registry.py）；插件键 `test/uart/dmesg/scheduler/generic/image`。

- [ ] **Step 1: Write the failing plugin tests**

```python
# tests/unit/test_cleaning_plugins.py
from datetime import UTC, datetime

import pytest

from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.types import SourceKind

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)


def test_testlog_strips_ansi_and_parses_events() -> None:
    text = (
        "\x1b[32m2026-11-20T10:00:01.123Z INFO device ready\r\n"
        "2026-11-20T10:00:02.000Z CMD nvme id-ctrl @CMD uart_tick=12345\n"
        "2026-11-20T10:53:43.500Z ERROR Traceback (most recent call last):\n"
        "2026-11-20T10:53:43.501Z ERROR AssertionError: power down waiting gpf\n"
        "2026-11-20T10:53:44.000Z FAILED exit code 42\n"
    )
    parsed = CleaningRegistry().parse("test", text)
    assert parsed.source == SourceKind.TEST
    assert all("\x1b" not in e.text for e in parsed.events)
    assert len(parsed.events) == 5
    kinds = {e.kind for e in parsed.events}
    assert "hard_error" in kinds and "command" in kinds
    assert parsed.events[1].tick == 12345
    assert parsed.events[1].ts_utc == datetime(2026, 11, 20, 10, 0, 2, tzinfo=UTC)
    assert parsed.key_lines and any("exit code" in k.text for k in parsed.key_lines)


def test_uart_folds_heartbeat_and_keeps_assert_context() -> None:
    lines = ["\x00\x01garbage\xff", "heartbeat 1", "heartbeat 2", "heartbeat 3"]
    lines += [f"ASSERT ctx {i}" for i in range(31)]
    text = "\n".join(lines)
    parsed = CleaningRegistry().parse("uart", text)
    assert "garbage" in parsed.raw_lines[0]
    assert parsed.metrics["heartbeat_folded"] == 2  # 3 行折叠为 1 行，净省 2
    assert len(parsed.key_lines) >= 1
    assert parsed.key_lines[0].kind == "hard_error"
    assert len(parsed.events) > 0
    assert all(e.ts_utc is None for e in parsed.events)  # 串口无时钟


def test_uart_extracts_tick_and_build_id() -> None:
    text = "Build ID: FW-20261120-001\n@TICK 12345\ncmd power down\n"
    parsed = CleaningRegistry().parse("uart", text)
    ticks = [e.tick for e in parsed.events if e.tick is not None]
    assert 12345 in ticks
    assert any("FW-20261120-001" in k.text for k in parsed.key_lines)


def test_dmesg_whitelist_and_folds_repeats() -> None:
    text = (
        "[    0.000000] Booting Linux\n"
        "[   12.345678] nvme nvme0: AER: corrected error received\n"
        "[   12.500000] usb 1-1: device descriptor read/64, error -71\n"
        "[   12.500000] usb 1-1: device descriptor read/64, error -71\n"
        "[   12.500000] usb 1-1: device descriptor read/64, error -71\n"
        "[   13.000000] nvme0: I/O tag 42 opcode 0x2 status: 0x400\n"
    )
    parsed = CleaningRegistry().parse("dmesg", text, boot_utc=BOOT)
    # usb 不在硬件白名单 → 折叠但不进事件流
    assert all(e.text.startswith(("nvme", "Booting")) for e in parsed.events)
    assert parsed.metrics["repeats_folded"] == 2
    aer = next(e for e in parsed.events if "AER" in e.text)
    assert aer.ts_utc == datetime(2026, 11, 20, 2, 0, 12, 345678, tzinfo=UTC)
    assert any(k.kind == "hard_error" for k in parsed.key_lines)


def test_dmesg_call_trace_is_hard_error() -> None:
    text = "[   20.000000] BUG: unable to handle kernel NULL pointer\n[   20.000001] Call Trace:\n[   20.000002]  nvme_queue_rq+0x1a/0x20\n"
    parsed = CleaningRegistry().parse("dmesg", text, boot_utc=BOOT)
    assert any("Call Trace" in k.text for k in parsed.key_lines)


def test_scheduler_compresses_states_and_flags_lock_conflict() -> None:
    text = (
        "2026-11-20T10:00:00.000Z STATE WAIT_ENV\n"
        "2026-11-20T10:00:01.000Z STATE WAIT_ENV\n"
        "2026-11-20T10:05:00.000Z STATE LOCK_DEVICE\n"
        "2026-11-20T10:05:01.000Z ERROR 锁板冲突：device busy\n"
        "2026-11-20T10:06:00.000Z STATE EXIT\n"
    )
    parsed = CleaningRegistry().parse("scheduler", text)
    assert parsed.metrics["states_compressed"] >= 1
    assert any("锁板冲突" in k.text for k in parsed.key_lines)
    assert parsed.events and all(e.ts_utc is not None for e in parsed.events)


def test_generic_strips_binary_and_limits_length() -> None:
    text = "\x00\x01hello\xff" * 100_000  # 60 万字符，超 20 万上限
    parsed = CleaningRegistry().parse("generic", text)
    assert len("".join(parsed.raw_lines)) <= 200_000
    assert "\x00" not in parsed.raw_lines[0]
    assert parsed.metrics["truncated"] is True


def test_image_passthrough() -> None:
    parsed = CleaningRegistry().parse("image", "<binary>")
    assert parsed.events == ()
    assert parsed.metrics["passthrough"] is True


def test_unknown_source_raises_chinese_error() -> None:
    with pytest.raises(ValueError, match="日志源"):
        CleaningRegistry().parse("nope", "x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_cleaning_plugins.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.cleaning'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    TEST = "test"
    UART = "uart"
    DMESG = "dmesg"
    SCHEDULER = "scheduler"
    GENERIC = "generic"
    IMAGE = "image"


@dataclass(frozen=True)
class Event:
    line_no: int
    ts_utc: datetime | None
    rel_ms: float | None
    tick: int | None
    kind: str
    text: str


@dataclass(frozen=True)
class KeyLine:
    line_no: int
    kind: str
    text: str
    ts_utc: datetime | None = None
    rel_ms: float | None = None
    tick: int | None = None


@dataclass(frozen=True)
class ParsedSource:
    source: SourceKind
    raw_lines: tuple[str, ...]
    events: tuple[Event, ...] = ()
    key_lines: tuple[KeyLine, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
```

```python
# src/aitrace/cleaning/registry.py
from __future__ import annotations

from datetime import datetime

from aitrace.cleaning.plugins.dmesg import DmesgPlugin
from aitrace.cleaning.plugins.generic import GenericPlugin
from aitrace.cleaning.plugins.scheduler import SchedulerPlugin
from aitrace.cleaning.plugins.testlog import TestlogPlugin
from aitrace.cleaning.plugins.uart import UartPlugin
from aitrace.cleaning.types import ParsedSource


class CleaningRegistry:
    def __init__(self) -> None:
        self._plugins = {
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
        plugin = self._plugins.get(source)
        if plugin is None:
            raise ValueError(f"日志源不受支持：{source!r}；合法取值：test/uart/dmesg/scheduler/generic/image")
        return plugin.parse(text, boot_utc=boot_utc)
```

```python
# src/aitrace/cleaning/plugins/__init__.py
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)Z?\s")
_TICK = re.compile(r"@TICK\s+(\d+)")
_CMD_TICK = re.compile(r"@CMD\s+uart_tick=(\d+)")


class CleaningPlugin(Protocol):
    key: str

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource: ...


def _ts(prefix: str, line: str) -> datetime | None:
    m = _TS.match(line)
    if not m:
        return None
    raw = m.group(1)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
```

```python
# src/aitrace/cleaning/plugins/testlog.py
from __future__ import annotations

from datetime import datetime

from aitrace.cleaning.plugins import _ANSI, _CMD_TICK, _ts
from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_HARD = re.compile(r"(Traceback|AssertionError|\bERROR\b|\bFAILED\b|Fatal)")
_CMD = re.compile(r"\bCMD\b")
_EXIT = re.compile(r"exit code\s+(-?\d+)")


class TestlogPlugin:
    key = "test"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = _ANSI.sub("", raw).rstrip("\r")
            ts = _ts("", line)
            tick = int(_CMD_TICK.search(line).group(1)) if _CMD_TICK.search(line) else None
            if _HARD.search(line):
                kind = "hard_error"
            elif _CMD.search(line):
                kind = "command"
            else:
                kind = "normal"
            events.append(Event(line_no, ts, None, tick, kind, line))
            if kind == "hard_error" or _EXIT.search(line) or tick is not None:
                key_lines.append(KeyLine(line_no, kind, line, ts_utc=ts, tick=tick))
        return ParsedSource(
            SourceKind.TEST,
            tuple(_ANSI.sub("", raw).rstrip("\r") for raw in text.splitlines()),
            tuple(events),
            tuple(key_lines),
            {"events_parsed": len(events)},
        )
```

```python
# src/aitrace/cleaning/plugins/uart.py
from __future__ import annotations

import re
from datetime import datetime

from aitrace.cleaning.plugins import _TICK
from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_HARD = re.compile(r"\b(ASSERT|PANIC|FATAL|ABORT|TIMEOUT|WATCHDOG|RESET|ERR)\b")
_HEARTBEAT = re.compile(r"(heartbeat|^tick\s+\d+)", re.IGNORECASE)
_BUILD = re.compile(r"Build\s+ID[:\s]*(\S+)")
_CMD = re.compile(r"\bcmd\b", re.IGNORECASE)
_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\n]")  # 保留 ASCII 可打印与中文


class UartPlugin:
    key = "uart"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        cleaned = _PRINTABLE.sub("", text)
        lines = cleaned.splitlines()
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        folded = 0
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            tick = int(_TICK.search(line).group(1)) if _TICK.search(line) else None
            if _HEARTBEAT.search(line) and events and events[-1].kind == "heartbeat":
                folded += 1
                continue
            kind = "heartbeat" if _HEARTBEAT.search(line) else (
                "hard_error" if _HARD.search(line) else "command" if _CMD.search(line) else "normal"
            )
            events.append(Event(line_no, None, None, tick, kind, line))
            if kind == "hard_error" or _BUILD.search(line) or tick is not None:
                key_lines.append(KeyLine(line_no, kind, line, tick=tick))
        return ParsedSource(
            SourceKind.UART,
            tuple(lines),
            tuple(events),
            tuple(key_lines),
            {"heartbeat_folded": folded, "events_parsed": len(events)},
        )
```

```python
# src/aitrace/cleaning/plugins/dmesg.py
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_REL = re.compile(r"^\[\s*(\d+)\.(\d{1,6})\]\s(.*)$")
_WHITELIST = re.compile(r"(nvme|pcie|pci|cxl|ahci|scsi|block|ext4|xfs|memory|cpu|BUG:|Call Trace)", re.IGNORECASE)
_HARD = re.compile(r"(AER|Call Trace|Link Down|Controller Reset|protocol error|BUG:)", re.IGNORECASE)


class DmesgPlugin:
    key = "dmesg"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        lines = text.splitlines()
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        folded = 0
        for line_no, line in enumerate(lines, start=1):
            m = _REL.match(line)
            if not m:
                continue
            sec, frac, body = int(m.group(1)), m.group(2), m.group(3)
            rel_ms = sec * 1000.0 + int(frac.ljust(6, "0")) / 1000.0
            ts = (boot_utc + timedelta(milliseconds=rel_ms)) if boot_utc else None
            if not _WHITELIST.search(body):
                continue
            if events and events[-1].text == line and "error" in body.lower():
                folded += 1
                continue
            kind = "hard_error" if _HARD.search(body) else "state" if "probe" in body.lower() or "attach" in body.lower() else "normal"
            events.append(Event(line_no, ts, rel_ms, None, kind, body))
            if kind == "hard_error":
                key_lines.append(KeyLine(line_no, kind, body, ts_utc=ts, rel_ms=rel_ms))
        return ParsedSource(
            SourceKind.DMESG,
            tuple(lines),
            tuple(events),
            tuple(key_lines),
            {"repeats_folded": folded, "events_parsed": len(events)},
        )
```

```python
# src/aitrace/cleaning/plugins/scheduler.py
from __future__ import annotations

from datetime import datetime

from aitrace.cleaning.plugins import _ts
from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_HARD = re.compile(r"(锁板冲突|lock\s*conflict|Agent\s*失联|agent\s*lost|任务取消|task\s*cancel)", re.IGNORECASE)
_STATE = re.compile(r"\bSTATE\s+(\S+)")


class SchedulerPlugin:
    key = "scheduler"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        lines = text.splitlines()
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        compressed = 0
        for line_no, line in enumerate(lines, start=1):
            ts = _ts("", line)
            m = _STATE.search(line)
            if m and events and events[-1].kind == "state" and events[-1].text.endswith(m.group(1)):
                compressed += 1
                continue
            if _HARD.search(line):
                kind = "hard_error"
            elif m:
                kind = "state"
            else:
                kind = "normal"
            events.append(Event(line_no, ts, None, None, kind, line))
            if kind == "hard_error" or (kind == "state" and m and m.group(1) in {"LOCK_DEVICE", "EXIT", "ENV_INIT"}):
                key_lines.append(KeyLine(line_no, kind, line, ts_utc=ts))
        return ParsedSource(
            SourceKind.SCHEDULER,
            tuple(lines),
            tuple(events),
            tuple(key_lines),
            {"states_compressed": compressed, "events_parsed": len(events)},
        )
```

```python
# src/aitrace/cleaning/plugins/generic.py
from __future__ import annotations

from datetime import datetime

from aitrace.cleaning.types import ParsedSource, SourceKind

_MAX_CHARS = 200_000
_BINARY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class GenericPlugin:
    key = "generic"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        cleaned = _BINARY.sub("", text)
        truncated = len(cleaned) > _MAX_CHARS
        if truncated:
            cleaned = cleaned[:_MAX_CHARS]
        return ParsedSource(
            SourceKind.GENERIC,
            tuple(cleaned.splitlines()),
            metrics={"truncated": truncated, "events_parsed": 0},
        )
```

Note: `image` 键走 `GenericPlugin` 的 passthrough 特例——registry 里对 `image` 直接返回 `ParsedSource(SourceKind.IMAGE, tuple(text.splitlines()), metrics={"passthrough": True})`（在 `CleaningRegistry.parse` 内分支），不注册独立插件类。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_cleaning_plugins.py -v`
Expected: 9 passed.

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass（M0 的 99 个 + 新增 9 个）。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 2: Golden log fixtures

**Files:**
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/logs/test.txt`
- Create: `tests/fixtures/logs/uart.txt`
- Create: `tests/fixtures/logs/dmesg.txt`
- Create: `tests/fixtures/logs/scheduler.txt`
- Create: `tests/fixtures/logs/__init__.py`
- Test: `tests/unit/test_golden_logs.py`

**Interfaces:**
- Consumes: Task 1 的 `CleaningRegistry`。
- Produces: `tests/fixtures/logs/{test,uart,dmesg,scheduler}.txt`（手写 golden 日志，含已知 T0/tick/硬异常标记）；`tests/fixtures/logs/__init__.py` 提供 `load_golden(name) -> str`。

- [ ] **Step 1: Write the failing golden tests**

```python
# tests/unit/test_golden_logs.py
from aitrace.cleaning.registry import CleaningRegistry
from tests.fixtures.logs import load_golden


def test_golden_test_log_layout() -> None:
    text = load_golden("test")
    assert "2026-11-20T10:53:43.500Z" in text          # T0 首错
    assert "@CMD uart_tick=12345" in text              # 串口互锁锚点
    assert "AssertionError" in text
    parsed = CleaningRegistry().parse("test", text)
    assert any(e.kind == "hard_error" for e in parsed.events)
    assert any(e.tick == 12345 for e in parsed.events)


def test_golden_uart_layout() -> None:
    text = load_golden("uart")
    assert "@TICK 12345" in text                        # 互锁对端
    assert "ASSERT" in text
    assert "Build ID" in text
    parsed = CleaningRegistry().parse("uart", text)
    assert any(k.kind == "hard_error" for k in parsed.key_lines)


def test_golden_dmesg_layout() -> None:
    text = load_golden("dmesg")
    assert "[  300.000000]" in text                     # AER 相对秒
    assert "AER" in text
    parsed = CleaningRegistry().parse("dmesg", text)
    assert any("AER" in k.text for k in parsed.key_lines)


def test_golden_scheduler_layout() -> None:
    text = load_golden("scheduler")
    assert "锁板冲突" in text
    parsed = CleaningRegistry().parse("scheduler", text)
    assert any("锁板冲突" in k.text for k in parsed.key_lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_golden_logs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.fixtures.logs'`。

- [ ] **Step 3: Write fixtures**

```python
# tests/fixtures/__init__.py
```

```python
# tests/fixtures/logs/__init__.py
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load_golden(name: str) -> str:
    path = _DIR / f"{name}.txt"
    if not path.is_file():
        raise ValueError(f"golden 日志缺失：{name}；期望文件 {path}")
    return path.read_text(encoding="utf-8")
```

`tests/fixtures/logs/test.txt`（时间线：10:00 准备 → 10:53:43.500 首错 T0；互锁命令 @CMD uart_tick=12345 位于 10:00:02）：

```
\x1b[32m2026-11-20T10:00:00.000Z INFO env ready device=nvme0n1 sn=SN-9560-001 slot=3 fw=FW-2.4.1
2026-11-20T10:00:01.000Z INFO begin case power_down_resume
2026-11-20T10:00:02.000Z CMD nvme power-down @CMD uart_tick=12345
2026-11-20T10:00:03.000Z PASS phase1 link ok
2026-11-20T10:53:43.500Z ERROR Traceback (most recent call last):
2026-11-20T10:53:43.501Z ERROR AssertionError: power down waiting gpf phase2 rsp, err_code=0x10B
2026-11-20T10:53:44.000Z FAILED exit code 42
2026-11-20T10:53:45.000Z INFO teardown done
```

`tests/fixtures/logs/uart.txt`：

```
\x00\x01Boot log start\xff
Build ID: FW-20261120-001
heartbeat 1
heartbeat 2
heartbeat 3
@TICK 12345
cmd power-down enter
@TICK 12346
cmd power-down waiting
ASSERT gpf phase2 timeout, reg0=0xDEAD reg1=0xBEEF
dump pc=0x1A2B sp=0x3C4D
heartbeat 4
```

`tests/fixtures/logs/dmesg.txt`：

```
[    0.000000] Booting Linux on nvme host
[  120.000000] nvme nvme0: device enumerated sn=SN-9560-001
[  300.000000] nvme nvme0: AER: uncorrected error received
[  300.000001] nvme nvme0: Call Trace:
[  300.000002]  nvme_irq+0x22/0x30
[  300.100000] pcieport 0000:00:1b.0: PCIe Link Down
```

`tests/fixtures/logs/scheduler.txt`：

```
2026-11-20T10:00:00.000Z STATE ENV_INIT host=10.20.68.114
2026-11-20T10:00:01.000Z STATE WAIT_ENV
2026-11-20T10:05:00.000Z STATE LOCK_DEVICE
2026-11-20T10:05:01.000Z ERROR 锁板冲突：device nvme0n1 busy by batch BATCH-OLD
2026-11-20T10:53:45.000Z STATE EXIT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_golden_logs.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 3: First-error location and phenomenon

**Files:**
- Create: `src/aitrace/cleaning/first_error.py`
- Test: `tests/unit/test_first_error.py`

**Interfaces:**
- Consumes: Task 1 `ParsedSource`/`Event`。
- Produces: `FirstError`（dataclass）、`locate_first_error(events: tuple[Event, ...]) -> FirstError | None`、`keyword_category(excerpt: str) -> str`、`relevant_sources(parsed: dict[str, ParsedSource]) -> set[str]`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_first_error.py
from datetime import UTC, datetime

from aitrace.cleaning.first_error import FirstError, keyword_category, locate_first_error
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.types import ParsedSource, SourceKind
from tests.fixtures.logs import load_golden

T0 = datetime(2026, 11, 20, 10, 53, 43, 500000, tzinfo=UTC)


def test_locate_first_error_from_golden() -> None:
    parsed = CleaningRegistry().parse("test", load_golden("test"))
    first = locate_first_error(parsed.events)
    assert first is not None
    assert first.ts_utc == T0
    assert first.exc_type == "AssertionError"
    assert first.err_code == "0x10B"
    assert first.line_no == 6
    assert "gpf" in first.excerpt


def test_locate_returns_none_without_failure() -> None:
    parsed = ParsedSource(SourceKind.TEST, ("ok",))
    assert locate_first_error(parsed.events) is None


def test_err_code_from_exit_line() -> None:
    parsed = CleaningRegistry().parse("test", "2026-11-20T10:00:00Z FAILED exit code 42\n")
    first = locate_first_error(parsed.events)
    assert first is not None and first.err_code == "42"


def test_keyword_category_closed_set() -> None:
    assert keyword_category("power down waiting gpf") == "power"
    assert keyword_category("timeout waiting rsp") == "timeout"
    assert keyword_category("AER uncorrected error") == "pcie"
    assert keyword_category("protocol violation") == "protocol"
    assert keyword_category("ASSERT gpf") == "assert"
    assert keyword_category("I/O error on page") == "io"
    assert keyword_category("out of memory") == "memory"
    assert keyword_category("random unknown text") == "other"


def test_relevant_sources() -> None:
    parsed = {
        "test": CleaningRegistry().parse("test", load_golden("test")),
        "uart": CleaningRegistry().parse("uart", load_golden("uart")),
        "dmesg": CleaningRegistry().parse("dmesg", load_golden("dmesg")),
        "scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler")),
    }
    relevant = relevant_sources(parsed)
    assert relevant == {"test", "uart", "dmesg"}  # 调度日志无环境类异常 → 不相关
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_first_error.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/first_error.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from aitrace.cleaning.types import Event, ParsedSource

_EXC = re.compile(r"(?:Traceback|raise|Error:)\s*\(?([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|AssertionError))")
_ERR_CODE = re.compile(r"(?:err(?:or)?[=:]?\s*|err_code=|exit code\s+)(-?(?:0x[0-9a-fA-F]+|\d+))", re.IGNORECASE)
_EXIT_CODE = re.compile(r"exit code\s+(-?\d+)", re.IGNORECASE)

_KEYWORDS = (
    ("power", ("power", "gpf", "suspend", "resume", "掉电", "掉盘")),
    ("timeout", ("timeout", "超时", "waiting", "expire")),
    ("pcie", ("aer", "pcie", "link down", "linkdown")),
    ("protocol", ("protocol", "phase", "rsp", "cmd")),
    ("assert", ("assert", "panic", "fatal", "bug")),
    ("io", ("i/o", "io error", "eio", "坏块", "read error", "write error")),
    ("memory", ("out of memory", "oom", "内存")),
)


@dataclass(frozen=True)
class FirstError:
    ts_utc: datetime
    exc_type: str | None
    err_code: str | None
    line_no: int
    excerpt: str


def locate_first_error(events: tuple[Event, ...]) -> FirstError | None:
    for event in events:
        if event.kind != "hard_error":
            continue
        exc = _EXC.search(event.text)
        code = _ERR_CODE.search(event.text)
        if code is None:
            code = _EXIT_CODE.search(event.text)
        if exc is None and code is None:
            continue
        return FirstError(
            ts_utc=event.ts_utc,  # type: ignore[arg-type]  # 测试日志必有时钟
            exc_type=exc.group(1) if exc else None,
            err_code=code.group(1) if code else None,
            line_no=event.line_no,
            excerpt=event.text,
        )
    return None


def keyword_category(excerpt: str) -> str:
    lowered = excerpt.lower()
    for category, needles in _KEYWORDS:
        if any(needle in lowered for needle in needles):
            return category
    return "other"


def relevant_sources(parsed: dict[str, ParsedSource]) -> set[str]:
    relevant = {"test"}
    if any(k.kind == "hard_error" for k in parsed.get("uart", ParsedSource(SourceKind.UART, ())).key_lines):
        relevant.add("uart")
    if any(k.kind == "hard_error" for k in parsed.get("dmesg", ParsedSource(SourceKind.DMESG, ())).key_lines):
        relevant.add("dmesg")
    if any(k.kind == "hard_error" for k in parsed.get("scheduler", ParsedSource(SourceKind.SCHEDULER, ())).key_lines):
        relevant.add("scheduler")
    return relevant
```

Note: `SourceKind` 需要在本模块 import（`from aitrace.cleaning.types import SourceKind`）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_first_error.py -v`
Expected: 5 passed。

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 4: Cross-source time alignment and anchor cascade

**Files:**
- Create: `src/aitrace/cleaning/align.py`
- Test: `tests/unit/test_align.py`

**Interfaces:**
- Consumes: Task 1/2/3（`ParsedSource`、golden 日志、`FirstError`）。
- Produces: `AlignedContext`（dataclass）、`align_sources(parsed, *, host_boot_time_utc, failed_at_utc_ms) -> AlignedContext`、`lock_anchor(ctx, first_error) -> Anchor`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_align.py
from datetime import UTC, datetime

from aitrace.cleaning.align import AlignedContext, align_sources, lock_anchor
from aitrace.cleaning.registry import CleaningRegistry
from tests.fixtures.logs import load_golden

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
FAILED_AT = int(datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC).timestamp() * 1000)
T0 = datetime(2026, 11, 20, 10, 53, 43, 500000, tzinfo=UTC)
CMD_TS = datetime(2026, 11, 20, 10, 0, 2, tzinfo=UTC)


def _ctx() -> AlignedContext:
    parsed = {
        "test": CleaningRegistry().parse("test", load_golden("test")),
        "uart": CleaningRegistry().parse("uart", load_golden("uart")),
        "dmesg": CleaningRegistry().parse("dmesg", load_golden("dmesg"), boot_utc=BOOT),
        "scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler")),
    }
    return align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)


def test_alignment_status() -> None:
    ctx = _ctx()
    assert ctx.aligned["test"] is True
    assert ctx.aligned["scheduler"] is True
    assert ctx.aligned["dmesg"] is True
    assert ctx.aligned["uart"] is True
    assert ctx.alignment_rate == 1.0


def test_uart_interlock_offset() -> None:
    ctx = _ctx()
    # tick 单位 1ms：uart tick 12345 ↔ test 10:00:02.000
    assert ctx.offset_ms["uart"] == int(CMD_TS.timestamp() * 1000) - 12345
    uart_ts = ctx.times["uart"]["ASSERT gpf phase2 timeout"]
    assert uart_ts is not None


def test_dmesg_boot_relative_conversion() -> None:
    ctx = _ctx()
    aer_ts = ctx.times["dmesg"]["AER: uncorrected error received"]
    assert aer_ts == datetime(2026, 11, 20, 2, 5, 0, tzinfo=UTC)


def test_anchor_cascade_to_earliest_hard_error() -> None:
    ctx = _ctx()
    anchor = lock_anchor(ctx, None)
    # 最早硬异常 = dmesg AER @ 02:05:00（远早于 T0 10:53:43.5）
    assert anchor.ts_utc == datetime(2026, 11, 20, 2, 5, 0, tzinfo=UTC)
    assert anchor.cascaded is True


def test_anchor_falls_back_to_failed_at() -> None:
    parsed = {"test": CleaningRegistry().parse("test", "2026-11-20T10:00:00Z INFO ok\n")}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    anchor = lock_anchor(ctx, None)
    assert anchor.ts_utc == datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC)
    assert anchor.cascaded is False


def test_missing_boot_time_marks_dmesg_unaligned() -> None:
    parsed = {"dmesg": CleaningRegistry().parse("dmesg", "[  300.000000] nvme AER\n")}
    ctx = align_sources(parsed, host_boot_time_utc=None, failed_at_utc_ms=FAILED_AT)
    assert ctx.aligned["dmesg"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_align.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/align.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from aitrace.cleaning.first_error import FirstError
from aitrace.cleaning.types import KeyLine, ParsedSource


@dataclass(frozen=True)
class Anchor:
    ts_utc: datetime
    source: str
    cascaded: bool


@dataclass
class AlignedContext:
    aligned: dict[str, bool] = field(default_factory=dict)
    offset_ms: dict[str, int] = field(default_factory=dict)
    times: dict[str, dict[str, datetime]] = field(default_factory=dict)
    alignment_rate: float = 0.0


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def align_sources(
    parsed: dict[str, ParsedSource],
    *,
    host_boot_time_utc: datetime | None,
    failed_at_utc_ms: int,
) -> AlignedContext:
    ctx = AlignedContext()
    for source, ps in parsed.items():
        if source in {"test", "scheduler", "generic", "image"}:
            ctx.aligned[source] = source in {"test", "scheduler"}
        elif source == "dmesg":
            ctx.aligned[source] = host_boot_time_utc is not None and bool(ps.events)
        elif source == "uart":
            ctx.aligned[source] = False
            offset = _uart_offset(parsed)
            if offset is not None:
                ctx.offset_ms[source] = offset
                ctx.aligned[source] = True
    aligned_count = sum(1 for ok in ctx.aligned.values() if ok)
    ctx.alignment_rate = aligned_count / len(parsed) if parsed else 0.0
    _fill_times(ctx, parsed, host_boot_time_utc)
    return ctx


def _uart_offset(parsed: dict[str, ParsedSource]) -> int | None:
    test_ps = parsed.get("test")
    uart_ps = parsed.get("uart")
    if test_ps is None or uart_ps is None:
        return None
    cmd_tick = next((e.tick for e in test_ps.events if e.tick is not None and e.ts_utc is not None), None)
    uart_has_tick = any(k.tick == cmd_tick for k in uart_ps.key_lines) if cmd_tick is not None else False
    if cmd_tick is None or not uart_has_tick:
        return None
    cmd_ts = next(e.ts_utc for e in test_ps.events if e.tick == cmd_tick and e.ts_utc is not None)
    return _ms(cmd_ts) - cmd_tick  # tick 单位 1ms


def _fill_times(ctx: AlignedContext, parsed: dict[str, ParsedSource], boot: datetime | None) -> None:
    for source, ps in parsed.items():
        times: dict[str, datetime] = {}
        if source == "uart":
            offset = ctx.offset_ms.get("uart")
            if offset is not None:
                for k in ps.key_lines:
                    if k.tick is not None:
                        times[k.text] = datetime.fromtimestamp((offset + k.tick) / 1000, tz=UTC)
        else:
            for k in ps.key_lines:
                if k.ts_utc is not None:
                    times[k.text] = k.ts_utc
        ctx.times[source] = times


def lock_anchor(ctx: AlignedContext, first_error: FirstError | None) -> Anchor:
    candidates: list[tuple[datetime, str]] = []
    for source, times in ctx.times.items():
        if source == "test":
            continue  # 首错单独参与
        for ts in times.values():
            candidates.append((ts, source))
    if first_error is not None:
        candidates.append((first_error.ts_utc, "test"))
    if not candidates:
        raise ValueError("锚点无法确定：无任何时间证据")
    anchor_ts, source = min(candidates, key=lambda item: item[0])
    cascaded = source != "test" or (first_error is not None and anchor_ts < first_error.ts_utc)
    return Anchor(ts_utc=anchor_ts, source=source, cascaded=cascaded)
```

Note: 无首错时以 `failed_at_utc` 作 T0 参与级联——`lock_anchor` 的调用方（pipeline）在 `first_error is None` 时先构造 `FirstError(failed_at_utc, None, None, 0, "")` 再调用；四路均无时间证据时（`ctx.times` 全空且无首错），由调用方兜底直接用 `failed_at_utc`（本函数抛 `ValueError` 作为防线，pipeline 捕获后兜底）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_align.py -v`
Expected: 6 passed。

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 5: Size-aware three-slice cutting

**Files:**
- Create: `src/aitrace/cleaning/slicer.py`
- Test: `tests/unit/test_slicer.py`

**Interfaces:**
- Consumes: Task 1/4（`ParsedSource`、`AlignedContext`、`Anchor`）；画像 `SliceParams`（`src/aitrace/profiles/models.py` 已有：`baseline_lines/tail_lines/error_before_s/error_after_s` 等——实现者先读该模型确认字段名，按其真实字段实现）。
- Produces: `Slice`（dataclass）、`Slices`（dataclass）、`build_slices(parsed, ctx, anchor, params) -> Slices`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_slicer.py
from datetime import UTC, datetime, timedelta

from aitrace.cleaning.align import align_sources, lock_anchor
from aitrace.cleaning.first_error import locate_first_error
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.slicer import build_slices
from aitrace.profiles.models import SliceParams
from tests.fixtures.logs import load_golden

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
FAILED_AT = int(datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC).timestamp() * 1000)

PARAMS = SliceParams(
    baseline_lines=200, tail_lines=200,
    error_before_s=60, error_after_s=15,
    full_max_bytes=1_000_000, window_budget_bytes=5_000_000,
)


def _slices():
    parsed = {
        "test": CleaningRegistry().parse("test", load_golden("test")),
        "uart": CleaningRegistry().parse("uart", load_golden("uart")),
        "dmesg": CleaningRegistry().parse("dmesg", load_golden("dmesg"), boot_utc=BOOT),
        "scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler")),
    }
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    return build_slices(parsed, ctx, anchor, PARAMS)


def test_small_logs_full_retained() -> None:
    slices = _slices()
    assert slices.full is True
    assert slices.shrinks == []


def test_error_window_covers_anchor_plus_minus() -> None:
    parsed = {"test": CleaningRegistry().parse("test", load_golden("test"))}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert slices.full is True  # golden 测试日志 < 1MB 全量
    assert slices.baseline is None and slices.tail is None


def test_oversize_triggers_three_windows() -> None:
    big = "2026-11-20T10:53:43.500Z ERROR Traceback\n" + (
        "2026-11-20T10:53:43.501Z ERROR AssertionError: power down waiting gpf phase2 rsp, err_code=0x10B\n"
    )
    filler = "2026-11-20T10:00:00.000Z INFO padding line for size\n"
    text = filler * 100 + big * 6000 + filler * 100  # 超 1MB
    parsed = {"test": CleaningRegistry().parse("test", text)}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert slices.full is False
    assert slices.baseline is not None and slices.tail is not None
    assert slices.error_window.size_bytes <= 5_000_000
    assert any("gpf" in line for line in slices.error_window.lines)


def test_window_shrink_order_and_exemptions() -> None:
    # 首错窗超 5MB：先缩前端基线→收尾→首错窗；必保行豁免保留
    head = "2026-11-20T10:00:00.000Z INFO head\n" * 300
    core = "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n" * 400_000  # > 5MB
    text = head + core
    parsed = {"test": CleaningRegistry().parse("test", text)}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert "baseline" in slices.shrinks or "error_window" in slices.shrinks
    assert slices.exemptions > 0
    assert "gpf" in slices.error_window.lines[0]


def test_must_keep_lines_outside_windows_are_exempted() -> None:
    # 必保行（ASSERT/exit code）落在窗口外也要进入首错窗
    text = (
        "2026-11-20T10:00:00.000Z INFO head\n"
        "2026-11-20T10:20:00.000Z ERROR ASSERT gpf timeout\n"   # 早于 anchor-60s
        "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n"
    )
    parsed = {"test": CleaningRegistry().parse("test", text)}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert slices.exemptions >= 1
    assert any("ASSERT gpf timeout" in line for line in slices.error_window.lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_slicer.py -v`
Expected: FAIL with `ModuleNotFoundError`。（若 `SliceParams` 字段名与计划不同，先读 `src/aitrace/profiles/models.py` 按真实字段修正测试再 RED。）

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/slicer.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from aitrace.cleaning.align import AlignedContext, Anchor
from aitrace.cleaning.types import ParsedSource
from aitrace.profiles.models import SliceParams


@dataclass(frozen=True)
class Slice:
    source: str
    window: str          # baseline / error_window / tail
    lines: tuple[str, ...]
    size_bytes: int


@dataclass
class Slices:
    baseline: Slice | None = None
    error_window: Slice | None = None
    tail: Slice | None = None
    full: bool = False
    shrinks: list[str] = field(default_factory=list)
    exemptions: int = 0


def _source_bytes(ps: ParsedSource) -> int:
    return sum(len(line.encode("utf-8")) + 1 for line in ps.raw_lines)


def build_slices(
    parsed: dict[str, ParsedSource],
    ctx: AlignedContext,
    anchor: Anchor,
    params: SliceParams,
) -> Slices:
    total = sum(_source_bytes(ps) for ps in parsed.values())
    if total <= params.full_max_bytes:
        return Slices(full=True)
    for source, ps in parsed.items():
        windows = _cut_source(source, ps, ctx, anchor, params)
        # 多源各产三个窗口；合并为一份 Slices 时按源合并进单窗口列表
        return _merge_windows(parsed, ctx, anchor, params)


def _cut_source(source: str, ps: ParsedSource, ctx: AlignedContext, anchor: Anchor, params: SliceParams):
    lines = ps.raw_lines
    base_n = min(params.baseline_lines, len(lines))
    tail_n = min(params.tail_lines, len(lines))
    before = anchor.ts_utc - timedelta(seconds=params.error_before_s)
    after = anchor.ts_utc + timedelta(seconds=params.error_after_s)
    times = ctx.times.get(source, {})
    return lines, base_n, tail_n, before, after, times


def _merge_windows(parsed, ctx, anchor, params) -> Slices:
    baseline_lines: list[tuple[str, str]] = []
    window_lines: list[tuple[str, str]] = []
    tail_lines: list[tuple[str, str]] = []
    exemptions = 0
    shrinks: list[str] = []
    for source, ps in parsed.items():
        lines = ps.raw_lines
        base_n = min(params.baseline_lines, len(lines))
        tail_n = min(params.tail_lines, len(lines))
        before = anchor.ts_utc - timedelta(seconds=params.error_before_s)
        after = anchor.ts_utc + timedelta(seconds=params.error_after_s)
        times = ctx.times.get(source, {})
        ts_by_line: dict[int, object] = {}
        for k in ps.key_lines:
            if k.ts_utc is not None:
                ts_by_line[k.line_no] = k.ts_utc
            elif k.tick is not None and source == "uart":
                offset = ctx.offset_ms.get("uart")
                if offset is not None:
                    from datetime import UTC, datetime as _dt
                    ts_by_line[k.line_no] = _dt.fromtimestamp((offset + k.tick) / 1000, tz=UTC)
        in_window: set[int] = set()
        for line_no in range(1, len(lines) + 1):
            ts = ts_by_line.get(line_no)
            if ts is not None and before <= ts <= after:
                in_window.add(line_no)
        for line_no, key in ((k.line_no, k) for k in ps.key_lines):
            if line_no not in in_window:
                in_window.add(line_no)   # 必保行豁免
                exemptions += 1
        for idx, line in enumerate(lines, start=1):
            tagged = f"{source}:{line}"
            if idx <= base_n:
                baseline_lines.append((tagged, line))
            elif idx > len(lines) - tail_n:
                tail_lines.append((tagged, line))
            if idx in in_window:
                window_lines.append((tagged, line))
    window_bytes = sum(len(l.encode("utf-8")) + 1 for _, l in window_lines)
    if window_bytes > params.window_budget_bytes:
        shrinks.append("baseline")
        baseline_lines = baseline_lines[: params.baseline_lines // 2]
        if window_bytes > params.window_budget_bytes:
            shrinks.append("tail")
            tail_lines = tail_lines[: params.tail_lines // 2]
        if window_bytes > params.window_budget_bytes:
            shrinks.append("error_window")
            window_lines = window_lines[-2000:]  # 保留事件边界对齐后的尾部核心
    return Slices(
        baseline=_mk("combined", "baseline", baseline_lines),
        error_window=_mk("combined", "error_window", window_lines),
        tail=_mk("combined", "tail", tail_lines),
        full=False,
        shrinks=shrinks,
        exemptions=exemptions,
    )


def _mk(source: str, window: str, tagged: list[tuple[str, str]]) -> Slice:
    lines = tuple(line for _, line in tagged)
    return Slice(source, window, lines, sum(len(l.encode("utf-8")) + 1 for l in lines))
```

Note: 实现者允许以更清晰的等价实现替换（如把窗口裁剪逻辑拆成纯函数），但测试断言的行为（full 判定、三窗口、收缩顺序、必保行豁免、`shrinks`/`exemptions` 计数）必须保持；`_cut_source` 若最终未被使用可删除（YAGNI，随实现取舍）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_slicer.py -v`
Expected: 5 passed。

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 6: Double-layer fingerprints and batch dedup

**Files:**
- Create: `src/aitrace/cleaning/fingerprint.py`
- Test: `tests/unit/test_fingerprint.py`

**Interfaces:**
- Consumes: Task 3 `keyword_category`/`FirstError`。
- Produces: `exact_fingerprint(script_path, line_no, exc_type, err_code, device_id) -> str`、`stable_fingerprint(exc_type, err_code, hardware_type, category) -> str`、`DedupGroup`、`dedup_key_lines(key_lines, exact_fn) -> tuple[DedupGroup, ...]`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_fingerprint.py
from aitrace.cleaning.fingerprint import (
    DedupGroup,
    dedup_key_lines,
    exact_fingerprint,
    stable_fingerprint,
)
from aitrace.cleaning.types import KeyLine


def test_exact_fingerprint_components_and_length() -> None:
    fp = exact_fingerprint("/cases/power.py", 88, "AssertionError", "0x10B", "SN-001")
    assert len(fp) == 16
    same = exact_fingerprint("/cases/power.py", 88, "AssertionError", "0x10B", "SN-001")
    assert fp == same
    moved = exact_fingerprint("/cases/power.py", 89, "AssertionError", "0x10B", "SN-001")
    assert fp != moved  # 行号漂移 → 精确指纹变化


def test_stable_fingerprint_immune_to_path_and_line() -> None:
    fp = stable_fingerprint("AssertionError", "0x10B", "SSD", "power")
    again = stable_fingerprint("AssertionError", "0x10B", "SSD", "power")
    assert fp == again and len(fp) == 16
    other = stable_fingerprint("AssertionError", "0x10B", "CXL", "power")
    assert fp != other  # 硬件域特征参与


def test_dedup_merges_same_fingerprint_with_count_and_range() -> None:
    lines = (
        KeyLine(10, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(11, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(12, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(13, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(20, "hard_error", "AER link down err_code=0x02"),
    )

    def exact(line: KeyLine) -> str:
        return exact_fingerprint("/cases/power.py", line.line_no // 10, "AssertionError", "0x10B", "SN-001")

    groups = dedup_key_lines(lines, exact)
    by_fp = {g.exact_fp: g for g in groups}
    assert len(groups) == 2
    gpf = by_fp[exact(lines[0])]
    assert gpf.count == 4
    assert gpf.first_line == 10 and gpf.last_line == 13


def test_dedup_never_crosses_case_boundary() -> None:
    # 同一函数对两个用例的 key_lines 分别调用，绝不合并（契约测试锁定调用方式）
    from aitrace.cleaning.fingerprint import exact_fingerprint as exact

    def lines_of(case_id: str) -> tuple[KeyLine, ...]:
        return (KeyLine(1, "hard_error", f"{case_id} gpf err_code=0x10B"),)

    groups_a = dedup_key_lines(lines_of("C1"), lambda k: exact("a.py", k.line_no, "E", "1", "SN-1"))
    groups_b = dedup_key_lines(lines_of("C2"), lambda k: exact("b.py", k.line_no, "E", "1", "SN-2"))
    assert groups_a[0].exact_fp != groups_b[0].exact_fp
    assert groups_a[0].count == 1 and groups_b[0].count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_fingerprint.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/fingerprint.py
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from aitrace.cleaning.types import KeyLine


def _digest(*parts: object) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def exact_fingerprint(script_path: str, line_no: int, exc_type: str | None, err_code: str | None, device_id: str) -> str:
    return _digest(script_path, line_no, exc_type, err_code, device_id)


def stable_fingerprint(exc_type: str | None, err_code: str | None, hardware_type: str, category: str) -> str:
    return _digest(exc_type, err_code, hardware_type, category)


@dataclass(frozen=True)
class DedupGroup:
    exact_fp: str
    count: int
    first_line: int
    last_line: int
    sample: str


def dedup_key_lines(
    key_lines: tuple[KeyLine, ...],
    exact_fn: Callable[[KeyLine], str],
) -> tuple[DedupGroup, ...]:
    ordered: list[tuple[str, KeyLine]] = []
    for line in key_lines:
        ordered.append((exact_fn(line), line))
    merged: dict[str, list[KeyLine]] = {}
    for fp, line in ordered:
        merged.setdefault(fp, []).append(line)
    groups = []
    for fp, items in merged.items():
        groups.append(
            DedupGroup(
                exact_fp=fp,
                count=len(items),
                first_line=items[0].line_no,
                last_line=items[-1].line_no,
                sample=items[0].text,
            )
        )
    return tuple(groups)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_fingerprint.py -v`
Expected: 4 passed。

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 7: L1 artifact writer with SHA-256 and dehydration metrics

**Files:**
- Create: `src/aitrace/cleaning/artifacts.py`
- Test: `tests/unit/test_artifacts.py`

**Interfaces:**
- Consumes: Task 5 `Slices`/`Slice`、Task 3 `FirstError`、Task 6 指纹。
- Produces: `ArtifactWriter(data_dir)`、`ArtifactWriter.write(session_id, round_no, *, summary: dict, slices: Slices, metrics: dict, now) -> ArtifactSet`；`ArtifactSet{paths, referenced_logs, metrics}`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_artifacts.py
import hashlib
import json
from pathlib import Path

from aitrace.cleaning.artifacts import ArtifactWriter
from aitrace.cleaning.slicer import Slice, Slices


def _slice(window: str, lines: tuple[str, ...]) -> Slice:
    return Slice("test", window, lines, sum(len(l.encode()) + 1 for l in lines))


def test_write_creates_round_dir_with_hashes(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    slices = Slices(
        baseline=_slice("baseline", ("head line 1", "head line 2")),
        error_window=_slice("error_window", ("ERROR gpf",)),
        tail=_slice("tail", ("exit code 42",)),
    )
    summary = {"session_id": "S-1", "round_no": 1, "first_error": None, "fingerprints": {"stable": "abc"}}
    from datetime import UTC, datetime
    artifacts = writer.write("S-1", 1, summary=summary, slices=slices, metrics={"duration_ms": 12}, now=datetime(2026, 11, 26, tzinfo=UTC))
    evidence_dir = tmp_path / "evidence" / "S-1" / "round_1"
    assert evidence_dir.is_dir()
    for name in ("baseline.txt", "error_window.txt", "tail.txt", "summary.json", "metrics.json"):
        assert (evidence_dir / name).is_file()
    stored = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["fingerprints"]["stable"] == "abc"
    # 每个切片文件的 hash 回填 referenced_logs
    assert len(artifacts.referenced_logs) == 3
    for entry in artifacts.referenced_logs:
        digest = hashlib.sha256((evidence_dir / entry["name"]).read_bytes()).hexdigest()
        assert entry["hash"] == digest
        assert entry["size_bytes"] > 0
        assert entry["source"] == "delivery"


def test_compression_ratio_metric(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    slices = Slices(full=True)
    metrics = {"per_source": {"test": {"original_bytes": 1000, "slice_bytes": 100, "events_parsed": 7}}}
    artifacts = writer.write("S-2", 1, summary={}, slices=slices, metrics=metrics, now=None)  # type: ignore[arg-type]
    ratio = artifacts.metrics["per_source"]["test"]["compression_ratio"]
    assert ratio == 0.1


def test_overwrite_is_idempotent(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    slices = Slices(full=True)
    first = writer.write("S-3", 1, summary={}, slices=slices, metrics={}, now=None)  # type: ignore[arg-type]
    second = writer.write("S-3", 1, summary={}, slices=slices, metrics={}, now=None)  # type: ignore[arg-type]
    assert first.paths == second.paths
    assert first.referenced_logs == second.referenced_logs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_artifacts.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/artifacts.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aitrace.cleaning.slicer import Slice, Slices

_WINDOW_FILES = {"baseline": "baseline.txt", "error_window": "error_window.txt", "tail": "tail.txt"}


@dataclass
class ArtifactSet:
    paths: dict[str, Path] = field(default_factory=dict)
    referenced_logs: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class ArtifactWriter:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def write(
        self,
        session_id: str,
        round_no: int,
        *,
        summary: dict[str, Any],
        slices: Slices,
        metrics: dict[str, Any],
        now: datetime | None,
    ) -> ArtifactSet:
        round_dir = self._data_dir / "evidence" / session_id / f"round_{round_no}"
        round_dir.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactSet()
        slices_map: dict[str, Slice] = {}
        if slices.baseline:
            slices_map["baseline"] = slices.baseline
        if slices.error_window:
            slices_map["error_window"] = slices.error_window
        if slices.tail:
            slices_map["tail"] = slices.tail
        for window, name in _WINDOW_FILES.items():
            sl = slices_map.get(window)
            if sl is None:
                continue
            path = round_dir / name
            path.write_text("".join(line + "\n" for line in sl.lines), encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            artifacts.paths[window] = path
            artifacts.referenced_logs.append(
                {
                    "name": name,
                    "source": "delivery",
                    "events_parsed": metrics.get("per_source", {}).get(sl.source, {}).get("events_parsed", 0),
                    "size_bytes": path.stat().st_size,
                    "hash": digest,
                }
            )
        summary_path = round_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifacts.paths["summary"] = summary_path
        final_metrics = _with_compression(metrics)
        metrics_path = round_dir / "metrics.json"
        metrics_path.write_text(json.dumps(final_metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifacts.paths["metrics"] = metrics_path
        artifacts.metrics = final_metrics
        return artifacts


def _with_compression(metrics: dict[str, Any]) -> dict[str, Any]:
    per_source: dict[str, Any] = {}
    for source, m in metrics.get("per_source", {}).items():
        original = m.get("original_bytes", 0)
        sliced = m.get("slice_bytes", 0)
        per_source[source] = {**m, "compression_ratio": round(sliced / original, 4) if original else 0.0}
    return {**metrics, "per_source": per_source}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_artifacts.py -v`
Expected: 3 passed。

- [ ] **Step 5: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 8: Cutoff rule gate and deterministic rule report

**Files:**
- Create: `src/aitrace/cleaning/rules.py`
- Create: `deploy/config/rules/cutoff.yaml`
- Modify: `src/aitrace/core/settings.py`（`StorageSettings` 增 `cutoff_rules_file: Path = Path("deploy/config/rules/cutoff.yaml")`）
- Test: `tests/unit/test_cutoff_rules.py`

**Interfaces:**
- Consumes: Task 1 `ParsedSource`/`KeyLine`；`Settings.storage.cutoff_rules_file`。
- Produces: `RuleHit`（dataclass）、`RuleSet.load(path) -> RuleSet`、`RuleSet.evaluate(parsed: dict[str, ParsedSource]) -> RuleHit | None`、`build_rule_fault_info(payload_authoritative: dict) -> dict`。

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cutoff_rules.py
from pathlib import Path

import yaml

from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleHit, RuleSet, build_rule_fault_info
from tests.fixtures.logs import load_golden

RULES_YAML = """
rules:
  - id: lock_conflict
    category: TEST_ENV_ERROR
    sources: [scheduler]
    pattern: "锁板冲突"
    detail: "锁板冲突：{line}"
  - id: pcie_link_down
    category: TEST_ENV_ERROR
    sources: [dmesg]
    pattern: "PCIe Link Down"
    detail: "PCIe 链路掉线：{line}"
"""


def _rules(tmp_path: Path) -> RuleSet:
    path = tmp_path / "cutoff.yaml"
    path.write_text(RULES_YAML, encoding="utf-8")
    return RuleSet.load(path)


def test_lock_conflict_rule_hits(tmp_path: Path) -> None:
    parsed = {"scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler"))}
    hit = _rules(tmp_path).evaluate(parsed)
    assert hit is not None
    assert hit.rule_id == "lock_conflict"
    assert hit.category == "TEST_ENV_ERROR"
    assert "锁板冲突" in hit.detail


def test_no_rule_hits_without_marker(tmp_path: Path) -> None:
    parsed = {"scheduler": CleaningRegistry().parse("scheduler", "2026-11-20T10:00:00Z STATE EXIT\n")}
    assert _rules(tmp_path).evaluate(parsed) is None


def test_rule_source_scoping(tmp_path: Path) -> None:
    # 规则限 scheduler 源：同样的 marker 出现在 test 源不命中
    parsed = {"test": CleaningRegistry().parse("test", "2026-11-20T10:00:00Z ERROR 锁板冲突\n")}
    assert _rules(tmp_path).evaluate(parsed) is None


def test_rule_config_missing_file_chinese_error(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(ValueError, match="截流规则"):
        RuleSet.load(tmp_path / "nope.yaml")


def test_build_rule_fault_info_uses_authoritative_fields() -> None:
    info = build_rule_fault_info(
        {"product_id": "ssd_9560", "env_topology": {"host_ip": "10.20.68.114"}, "hardware_type": "SSD"}
    )
    assert info["product"] == "ssd_9560"
    assert info["env_topology"]["host_ip"] == "10.20.68.114"


def test_shipped_cutoff_yaml_loads() -> None:
    path = Path("deploy/config/rules/cutoff.yaml")
    rules = RuleSet.load(path)
    assert len(rules.rules) >= 5  # 锁板冲突/盘被占用/Agent失联/任务取消/PCIe Link Down
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_cutoff_rules.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/aitrace/cleaning/rules.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aitrace.cleaning.types import ParsedSource


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    category: str
    detail: str
    source: str
    line: str


class RuleSet:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules

    @classmethod
    def load(cls, path: Path) -> "RuleSet":
        if not path.is_file():
            raise ValueError(f"截流规则文件缺失：{path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rules = data.get("rules", [])
        for rule in rules:
            for key in ("id", "category", "sources", "pattern"):
                if key not in rule:
                    raise ValueError(f"截流规则配置非法：规则缺少字段 {key}；文件 {path}")
        return cls(rules)

    def evaluate(self, parsed: dict[str, ParsedSource]) -> RuleHit | None:
        for rule in self.rules:
            pattern = re.compile(rule["pattern"])
            for source in rule["sources"]:
                ps = parsed.get(source)
                if ps is None:
                    continue
                for key in ps.key_lines:
                    m = pattern.search(key.text)
                    if m:
                        return RuleHit(
                            rule_id=rule["id"],
                            category=rule["category"],
                            detail=rule["detail"].format(line=key.text.strip()),
                            source=source,
                            line=key.text,
                        )
        return None


def build_rule_fault_info(authoritative: dict[str, Any]) -> dict[str, Any]:
    return {
        "product": authoritative.get("product_id", "-"),
        "symptom": "",
        "fw_version": authoritative.get("env_topology", {}).get("devices", [{}])[0].get("fw_version", "-")
        if authoritative.get("env_topology", {}).get("devices")
        else "-",
        "test_case": "-",
        "expected": "-",
        "actual": "-",
        "env_topology": {"host_ip": authoritative.get("env_topology", {}).get("host_ip", "-")},
    }
```

`deploy/config/rules/cutoff.yaml`：

```yaml
# 截流闸规则表：规则命中直接定界 TEST_ENV_ERROR 出报告回写，不进 AI。
# pattern 为 Python 正则；sources 限定日志源（test/uart/dmesg/scheduler）。
rules:
  - id: lock_conflict
    category: TEST_ENV_ERROR
    sources: [scheduler]
    pattern: "锁板冲突|lock\\s*conflict"
    detail: "环境问题：锁板冲突——{line}"
  - id: device_busy
    category: TEST_ENV_ERROR
    sources: [scheduler, test]
    pattern: "盘被占用|device\\s+busy"
    detail: "环境问题：设备被占用——{line}"
  - id: agent_lost
    category: TEST_ENV_ERROR
    sources: [scheduler]
    pattern: "Agent\\s*失联|agent\\s*lost"
    detail: "环境问题：Agent 失联——{line}"
  - id: task_cancelled
    category: TEST_ENV_ERROR
    sources: [scheduler, test]
    pattern: "任务取消|task\\s*cancel"
    detail: "环境问题：任务被取消——{line}"
  - id: pcie_link_down
    category: TEST_ENV_ERROR
    sources: [dmesg]
    pattern: "PCIe Link Down|Link Down"
    detail: "环境问题：PCIe 链路掉线——{line}"
```

Settings 修改（`src/aitrace/core/settings.py` 的 `StorageSettings` 加一个字段）：

```python
class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("data")
    max_attachment_bytes: int = Field(default=50 * 1024 * 1024, ge=1024 * 1024)
    profile_domains_dir: Path = Path("deploy/config/domains")
    cutoff_rules_file: Path = Path("deploy/config/rules/cutoff.yaml")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_cutoff_rules.py -v`
Expected: 6 passed。

- [ ] **Step 5: Run full checks**

Run: `make lint && make test && make contract`
Expected: lint clean; all tests pass；契约再生成无漂移（Settings 增字段会影响 openapi？不会——Settings 不进 openapi；若 `make contract` 报漂移，运行 `python tools/export_contracts.py` 重新生成并检查 diff 合理）。

- [ ] **Step 6: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 9: Cleaning pipeline and worker (claim → pipeline → finish)

**Files:**
- Create: `src/aitrace/cleaning/pipeline.py`
- Create: `src/aitrace/cleaning/worker.py`
- Test: `tests/integration/test_cleaning_worker.py`
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**
- Consumes: Task 1–8 全部；M0 `QueueRepository.claim_clean/finish/retry`（签名以 `src/aitrace/persistence/repositories/queue.py` 为准：`claim_clean(owner, now)`、`finish(task_id, owner, generation, state, now, error=None)`、`retry(...)`——实现者先读文件确认 retry 签名）、`Stage`/`QueueState` 枚举、`SessionModel`/`Round`/`Report` 模型、`TaskQueue.payload`（M0 ingest 写入的 `{script_path, failed_at_utc, env_binding, logs}`）。
- Produces: `PipelineOutcome`（dataclass）、`run_pipeline(...) -> PipelineOutcome`、`CleaningWorker.process_one(session, task_row, *, clock, ids, data_dir, rules, registry) -> str`（返回 `"ready"|"rule_hit"|"failed"`）、`CleaningWorker.run_once(session, *, clock, ids, data_dir, rules, registry, owner) -> int`。

- [ ] **Step 1: Write the failing unit tests (pipeline)**

```python
# tests/unit/test_pipeline.py
from datetime import UTC, datetime
from pathlib import Path

from aitrace.cleaning.pipeline import PipelineOutcome, run_pipeline
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from tests.fixtures.logs import load_golden

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
FAILED_AT = int(datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC).timestamp() * 1000)

PAYLOAD = {
    "script_path": "/cases/power_down_resume.py",
    "failed_at_utc": FAILED_AT,
    "env_binding": {"device_id": "nvme0n1", "sn": "SN-9560-001", "slot": "3", "fw_version": "FW-2.4.1"},
    "logs": {
        "test_log_path": "test.txt",
        "uart_log_path": "uart.txt",
        "kernel_log_path": "dmesg.txt",
        "scheduler_log_path": "scheduler.txt",
    },
}
AUTHORITATIVE = {
    "product_id": "ssd_9560",
    "batch_id": "BATCH-20261120-0089",
    "case_id": "CASE-1021",
    "trigger_user_id": "zhangsan",
    "hardware_type": "SSD",
    "env_topology": {"host_ip": "10.20.68.114", "host_boot_time_utc": int(BOOT.timestamp()), "devices": []},
    "git_context": {"repo_url": "git@x:y.git", "commit_id": "abc123"},
}


def _run(tmp_path: Path, payload: dict | None = None) -> PipelineOutcome:
    for name in ("test", "uart", "dmesg", "scheduler"):
        (tmp_path / f"{name}.txt").write_text(load_golden(name), encoding="utf-8")
    rules = RuleSet.load(Path("deploy/config/rules/cutoff.yaml"))
    return run_pipeline(
        payload=payload or PAYLOAD,
        authoritative=AUTHORITATIVE,
        raw_root=tmp_path,
        profile=None,
        rules=rules,
        registry=CleaningRegistry(),
    )


def test_ready_pipeline_produces_fingerprints_and_anchor(tmp_path: Path) -> None:
    outcome = _run(tmp_path)
    assert outcome.status == "ready"
    assert outcome.first_error is not None
    assert outcome.anchor is not None
    assert outcome.stable_fp and len(outcome.stable_fp) == 16
    assert outcome.exact_fp and len(outcome.exact_fp) == 16
    assert outcome.summary["anchor_utc"] is not None
    assert outcome.metrics["alignment_rate"] == 1.0


def test_missing_logs_marked_unavailable(tmp_path: Path) -> None:
    (tmp_path / "test.txt").unlink()
    outcome = _run(tmp_path)
    assert outcome.status == "ready"
    assert outcome.summary["source_availability"]["test"] is False
    assert outcome.metrics["alignment_rate"] < 1.0


def test_rule_hit_short_circuits(tmp_path: Path) -> None:
    payload = {**PAYLOAD}
    outcome = _run(tmp_path, payload)
    # golden scheduler 含“锁板冲突”→ 截流闸命中
    assert outcome.status == "rule_hit"
    assert outcome.rule_hit is not None
    assert outcome.rule_hit.category == "TEST_ENV_ERROR"
    assert outcome.rule_hit.rule_id == "lock_conflict"


def test_all_logs_missing_fails(tmp_path: Path) -> None:
    for name in ("test", "uart", "dmesg", "scheduler"):
        (tmp_path / f"{name}.txt").unlink()
    outcome = _run(tmp_path)
    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "日志" in outcome.error["message"]
```

- [ ] **Step 2: Write the failing integration tests (worker)**

```python
# tests/integration/test_cleaning_worker.py
from datetime import UTC, datetime
from pathlib import Path

from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from aitrace.cleaning.worker import CleaningWorker
from aitrace.core.enums import QueueState, Stage
from aitrace.persistence.repositories.queue import QueueRepository
from sqlalchemy import select

from aitrace.persistence.models import Round, TaskQueue
from tests.fixtures.logs import load_golden


def _setup(db_session, data_dir: Path, trigger_user_id: str = "zhangsan") -> str:
    # 用 M0 ingest 的方式构造 PENDING case_clean 任务（或直接复用 IngestService）
    from aitrace.api.schemas import DeliveryPayload
    from aitrace.ingest.service import IngestService
    from aitrace.core.ids import SystemClock, UuidIdGenerator
    from aitrace.profiles.resolver import ProfileResolver

    payload = DeliveryPayload.model_validate(
        {
            "product_id": "ssd_9560",
            "batch_id": "BATCH-20261120-0089",
            "trigger_user_id": trigger_user_id,
            "hardware_type": "SSD",
            "env_topology": {
                "host_ip": "10.20.68.114",
                "host_boot_time_utc": 1768874400,
                "devices": [{"device_id": "nvme0n1", "pcie_bdf": "01:00.0", "device_node": "/dev/nvme0n1", "model": "M9560", "sn": "SN-9560-001", "fw_version": "FW-2.4.1", "slot": "3"}],
            },
            "git_context": {"repo_url": "git@x:y.git", "commit_id": "abc123"},
            "failed_cases": [
                {
                    "case_id": f"CASE-{trigger_user_id}",
                    "script_path": "/cases/power_down_resume.py",
                    "failed_at_utc": 1768877624000,
                    "env_binding": {"device_id": "nvme0n1"},
                    "logs": {
                        "test_log_path": str(data_dir / "test.txt"),
                        "uart_log_path": str(data_dir / "uart.txt"),
                        "kernel_log_path": str(data_dir / "dmesg.txt"),
                        "scheduler_log_path": str(data_dir / "scheduler.txt"),
                    },
                }
            ],
            "passed_cases": [],
        }
    )
    from aitrace.profiles.loader import load_registry
    from aitrace.core.settings import Settings as _S
    registry = load_registry(Path("deploy/config/domains"))
    resolver = ProfileResolver(registry)
    outcome = IngestService(db_session, resolver, SystemClock(), UuidIdGenerator()).deliver(payload)
    db_session.flush()
    return outcome.response.sessions[0].session_id


def _worker(db_session, data_dir: Path, now):
    rules = RuleSet.load(Path("deploy/config/rules/cutoff.yaml"))
    worker = CleaningWorker(QueueRepository(db_session), CleaningRegistry(), rules)
    return worker.run_once(db_session, clock=FrozenClock(now), ids=UuidIdGenerator(), data_dir=data_dir, owner="w1")


class FrozenClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now


def test_rule_hit_flow_writes_report_and_finishes_done(db_session, tmp_path: Path) -> None:
    for name in ("test", "uart", "dmesg", "scheduler"):
        (tmp_path / f"{name}.txt").write_text(load_golden(name), encoding="utf-8")
    session_id = _setup(db_session, tmp_path)
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now)
    assert handled == 1
    task = db_session.execute(select(TaskQueue).where(TaskQueue.session_id == session_id)).scalar_one()
    assert task.queue_state == QueueState.DONE.value
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.stage == Stage.COMPLETED.value
    assert round_row.report_id is not None
    from aitrace.persistence.models import Report
    report = db_session.get(Report, round_row.report_id)
    assert report.result["root_cause"]["category"] == "TEST_ENV_ERROR"
    assert report.citation == {"citing_status": "NONE"}


def test_ready_flow_finishes_ready_without_report(db_session, tmp_path: Path) -> None:
    # 无规则命中的日志 → READY，等待 M2 AI
    (tmp_path / "test.txt").write_text(
        "2026-11-20T10:00:00Z INFO ok\n2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n",
        encoding="utf-8",
    )
    (tmp_path / "uart.txt").write_text("Build ID: X\n", encoding="utf-8")
    (tmp_path / "dmesg.txt").write_text("", encoding="utf-8")
    (tmp_path / "scheduler.txt").write_text("2026-11-20T10:00:00Z STATE EXIT\n", encoding="utf-8")
    session_id = _setup(db_session, tmp_path, trigger_user_id="lisi")
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now)
    assert handled == 1
    task = db_session.execute(select(TaskQueue).where(TaskQueue.session_id == session_id)).scalar_one()
    assert task.queue_state == QueueState.READY.value
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.stage == Stage.CLEANING.value  # 清洗完成等待 AI 的阶段语义
    assert round_row.exact_fingerprint is not None
    session_row = db_session.execute(select(__import__("aitrace.persistence.models", fromlist=["SessionModel"]).SessionModel).where(__import__("aitrace.persistence.models", fromlist=["SessionModel"]).SessionModel.session_id == session_id)).scalar_one()
    assert session_row.stable_fingerprint is not None
    assert (tmp_path / "evidence" / session_id / "round_1" / "summary.json").is_file()
```

Note: 上面的 `__import__` 写法仅因行宽，实现者应改为正常 import。`Stage.CLEANING` 表示「清洗完成、等待 AI」（M0 状态映射约定：READY→QUEUED 10% 对用户呈现），round.stage 语义按 M0 冻结的 Stage 枚举执行。

- [ ] **Step 3: Run and confirm failure**

Run: `pytest tests/unit/test_pipeline.py tests/integration/test_cleaning_worker.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 4: Implement pipeline**

```python
# src/aitrace/cleaning/pipeline.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aitrace.cleaning.align import AlignedContext, Anchor, align_sources, lock_anchor
from aitrace.cleaning.first_error import FirstError, keyword_category, locate_first_error, relevant_sources
from aitrace.cleaning.fingerprint import dedup_key_lines, exact_fingerprint, stable_fingerprint
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleHit, RuleSet
from aitrace.cleaning.slicer import build_slices
from aitrace.profiles.models import DomainProfile, SliceParams
from aitrace.profiles.resolver import ResolvedProfile

_LOG_FIELDS = (
    ("test", "test_log_path"),
    ("uart", "uart_log_path"),
    ("dmesg", "kernel_log_path"),
    ("scheduler", "scheduler_log_path"),
)


@dataclass
class PipelineOutcome:
    status: str  # ready / rule_hit / failed
    first_error: FirstError | None = None
    anchor: Anchor | None = None
    stable_fp: str | None = None
    exact_fp: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    slices: Any = None
    rule_hit: RuleHit | None = None
    referenced_logs: list[dict[str, Any]] = field(default_factory=list)
    dedup_groups: tuple[Any, ...] = ()
    error: dict[str, Any] | None = None


def run_pipeline(
    *,
    payload: dict[str, Any],
    authoritative: dict[str, Any],
    raw_root: Path,
    profile: ResolvedProfile | None,
    rules: RuleSet,
    registry: CleaningRegistry,
) -> PipelineOutcome:
    started = datetime.now(UTC)
    parsed: dict[str, Any] = {}
    availability: dict[str, bool] = {}
    for source, field in _LOG_FIELDS:
        path = (payload.get("logs") or {}).get(field)
        if not path:
            availability[source] = False
            continue
        full = raw_root / path
        if not full.is_file():
            availability[source] = False
            continue
        availability[source] = True
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return PipelineOutcome(
                status="failed",
                error={"code": "LOG_READ_FAILED", "message": f"日志读取失败：{source}（{field}）路径 {path}；原因 {exc}"},
            )
        parsed[source] = registry.parse(source, text, boot_utc=_boot(authoritative))
    if not any(availability.values()):
        return PipelineOutcome(
            status="failed",
            error={"code": "NO_LOGS", "message": "清洗失败：投递的四路日志全部缺失或不可读，无法定位首错", "retryable": True},
        )
    hit = rules.evaluate(parsed)
    failed_at_ms = int(payload["failed_at_utc"])
    first = locate_first_error(parsed.get("test").events) if parsed.get("test") else None  # type: ignore[union-attr]
    if hit is not None:
        anchor = _anchor_or_failed(parsed, authoritative, first, failed_at_ms)
        slices = build_slices(parsed, _ctx(parsed, authoritative, failed_at_ms), anchor, _params(profile))
        device_id = (payload.get("env_binding") or {}).get("device_id", "")
        exc_type = first.exc_type if first else None
        err_code = first.err_code if first else None
        category = keyword_category(first.excerpt) if first else "other"
        return PipelineOutcome(
            status="rule_hit",
            first_error=first,
            anchor=anchor,
            stable_fp=stable_fingerprint(exc_type, err_code, authoritative.get("hardware_type", "SSD"), category),
            exact_fp=exact_fingerprint(str(payload.get("script_path", "")), first.line_no if first else 0, exc_type, err_code, device_id),
            summary=_summary("rule_hit", parsed, availability, first, anchor),
            metrics=_metrics(parsed, slices, started),
            slices=slices,
            rule_hit=hit,
        )
    ctx = _ctx(parsed, authoritative, failed_at_ms)
    anchor = _anchor_or_failed(parsed, authoritative, first, failed_at_ms)
    slices = build_slices(parsed, ctx, anchor, _params(profile))
    device_id = (payload.get("env_binding") or {}).get("device_id", "")
    exc_type = first.exc_type if first else None
    err_code = first.err_code if first else None
    category = keyword_category(first.excerpt) if first else "other"
    exact = exact_fingerprint(str(payload.get("script_path", "")), first.line_no if first else 0, exc_type, err_code, device_id)
    stable = stable_fingerprint(exc_type, err_code, authoritative.get("hardware_type", "SSD"), category)
    groups = dedup_key_lines(
        tuple(k for ps in parsed.values() for k in ps.key_lines if k.kind == "hard_error"),
        lambda k: exact_fingerprint(str(payload.get("script_path", "")), k.line_no, exc_type, err_code, device_id),
    )
    return PipelineOutcome(
        status="ready",
        first_error=first,
        anchor=anchor,
        stable_fp=stable,
        exact_fp=exact,
        summary=_summary("ready", parsed, availability, first, anchor),
        metrics=_metrics(parsed, slices, started),
        slices=slices,
        referenced_logs=[],
        dedup_groups=groups,
    )


def _boot(authoritative: dict[str, Any]) -> datetime | None:
    boot_s = (authoritative.get("env_topology") or {}).get("host_boot_time_utc")
    return datetime.fromtimestamp(boot_s, tz=UTC) if boot_s else None


def _ctx(parsed, authoritative, failed_at_ms) -> AlignedContext:
    return align_sources(parsed, host_boot_time_utc=_boot(authoritative), failed_at_utc_ms=failed_at_ms)


def _anchor_or_failed(parsed, authoritative, first, failed_at_ms) -> Anchor:
    ctx = _ctx(parsed, authoritative, failed_at_ms)
    probe = first or FirstError(datetime.fromtimestamp(failed_at_ms / 1000, tz=UTC), None, None, 0, "")
    try:
        return lock_anchor(ctx, probe)
    except ValueError:
        return Anchor(ts_utc=datetime.fromtimestamp(failed_at_ms / 1000, tz=UTC), source="fallback", cascaded=False)


def _params(profile: ResolvedProfile | None) -> SliceParams:
    if profile is not None and profile.profile is not None:
        return profile.profile.slice_params
    return SliceParams()


def _summary(status, parsed, availability, first, anchor) -> dict[str, Any]:
    return {
        "status": status,
        "source_availability": availability,
        "first_error": None if first is None else {
            "ts_utc": first.ts_utc.isoformat() if first.ts_utc else None,
            "exc_type": first.exc_type, "err_code": first.err_code,
            "line_no": first.line_no, "excerpt": first.excerpt,
        },
        "anchor_utc": None if anchor is None else anchor.ts_utc.isoformat(),
        "anchor_source": None if anchor is None else anchor.source,
        "relevant_sources": sorted(relevant_sources(parsed)),
    }


def _metrics(parsed, slices, started) -> dict[str, Any]:
    per_source = {}
    for source, ps in parsed.items():
        original = sum(len(l.encode("utf-8")) + 1 for l in ps.raw_lines)
        slice_bytes = original if slices.full else 0
        per_source[source] = {"original_bytes": original, "slice_bytes": slice_bytes, "events_parsed": len(ps.events)}
    return {"duration_ms": int((datetime.now(UTC) - started).total_seconds() * 1000), "per_source": per_source}
```

- [ ] **Step 5: Implement worker**

```python
# src/aitrace/cleaning/worker.py
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aitrace.cleaning.artifacts import ArtifactWriter
from aitrace.cleaning.pipeline import PipelineOutcome, run_pipeline
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from aitrace.core.enums import QueueState, Stage
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.persistence.models import Report, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository


class CleaningWorker:
    def __init__(self, queue: QueueRepository, registry: CleaningRegistry, rules: RuleSet) -> None:
        self._queue = queue
        self._registry = registry
        self._rules = rules

    def run_once(
        self,
        session: Session,
        *,
        clock: SystemClock,
        ids: UuidIdGenerator,
        data_dir: Path,
        owner: str,
    ) -> int:
        task = self._queue.claim_clean(owner, clock.now())
        if task is None:
            return 0
        try:
            self.process_one(session, task, clock=clock, ids=ids, data_dir=data_dir)
        finally:
            session.flush()
        return 1

    def process_one(
        self,
        session: Session,
        task: TaskQueue,
        *,
        clock: SystemClock,
        ids: UuidIdGenerator,
        data_dir: Path,
    ) -> str:
        now = clock.now()
        session_row = session.get(SessionModel, task.session_id)
        round_row = session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(Round).where(Round.round_id == task.round_id)
        ).scalar_one()
        started = now
        if round_row.stage == Stage.QUEUED.value:
            round_row.stage = Stage.CLEANING.value
        raw_root = data_dir / "rawlogs" / task.batch_id
        outcome = run_pipeline(
            payload=task.payload or {},
            authoritative={
                "product_id": task.product_id,
                "batch_id": task.batch_id,
                "case_id": task.case_id,
                "trigger_user_id": task.queue_key,
                "hardware_type": session_row.hardware_type,
                "env_topology": session_row.env_topology,
                "git_context": session_row.git_context,
            },
            raw_root=raw_root,
            profile=None,
            rules=self._rules,
            registry=self._registry,
        )
        duration_ms = int((clock.now() - started).total_seconds() * 1000)
        step = {
            "step": "CLEANING",
            "title": "多源日志脱水与切片",
            "start": started.isoformat(),
            "end": clock.now().isoformat(),
            "duration_ms": duration_ms,
            "summary": outcome.summary.get("status", ""),
            "status": "SUCCESS" if outcome.status != "failed" else "FAILED",
        }
        if outcome.status == "failed":
            return self._fail(session, task, round_row, outcome, now)
        session_row.stable_fingerprint = outcome.stable_fp
        round_row.exact_fingerprint = outcome.exact_fp
        writer = ArtifactWriter(data_dir)
        artifacts = writer.write(
            task.session_id,
            round_row.round_no,
            summary=outcome.summary,
            slices=outcome.slices,
            metrics=outcome.metrics,
            now=now,
        )
        if outcome.status == "rule_hit":
            report = Report(
                report_id=ids.new("RP"),
                round_id=round_row.round_id,
                fault_info={"product": task.product_id, "symptom": outcome.rule_hit.line if outcome.rule_hit else "", "env_topology": {"host_ip": (session_row.env_topology or {}).get("host_ip", "-")}},
                result={"root_cause": {"category": outcome.rule_hit.category if outcome.rule_hit else "TEST_ENV_ERROR", "detail": outcome.rule_hit.detail if outcome.rule_hit else "", "evidence": []}, "solution": None, "confidence": None, "gap_note": None},
                confidence_breakdown=[],
                evidence=[],
                referenced_logs=artifacts.referenced_logs,
                process_steps=[step],
                citation={"citing_status": "NONE"},
                gap_note=None,
                created_at=now,
            )
            session.add(report)
            session.flush()
            round_row.report_id = report.report_id
            round_row.stage = Stage.COMPLETED.value
            round_row.round_status = "AWAITING_FEEDBACK"
            round_row.finished_at = now
            self._queue.finish(task.task_id, task.lease_owner or "", task.lease_generation, QueueState.DONE, now)
            return "rule_hit"
        round_row.finished_at = now
        self._queue.finish(task.task_id, task.lease_owner or "", task.lease_generation, QueueState.READY, now)
        return "ready"

    def _fail(self, session: Session, task: TaskQueue, round_row: Round, outcome: PipelineOutcome, now: datetime) -> str:
        if task.attempts == 0:
            self._queue.retry(task.task_id, task.lease_owner or "", task.lease_generation, now)
            return "failed"
        round_row.stage = Stage.FAILED.value
        round_row.round_status = "FAILED"
        round_row.finished_at = now
        self._queue.finish(task.task_id, task.lease_owner or "", task.lease_generation, QueueState.FAILED, now, error=outcome.error)
        return "failed"
```

Note: `retry` 的实际签名以 M0 `queue.py` 为准（`retry(task_id, owner, generation, now)` 或类似），实现者先读文件适配；若 M0 retry 不含 owner/generation 参数则按其实现调用。`__import__` 仅为排版，正常 import `select`。

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_pipeline.py tests/integration/test_cleaning_worker.py -v`
Expected: 6 passed。

- [ ] **Step 7: Run full checks**

Run: `make lint && make test`
Expected: lint clean; all tests pass。

- [ ] **Step 8: Commit checkpoint（跳过——用户约束不提交）**

---

### Task 10: Aggregation gate mechanics and migration 0002

**Files:**
- Create: `src/aitrace/scheduling/aggregate.py`
- Create: `src/aitrace/persistence/migrations/versions/0002_batch_gate.py`
- Test: `tests/unit/test_aggregate.py`
- Test: `tests/integration/test_aggregate_db.py`

**Interfaces:**
- Consumes: M0 `SessionModel`（stable_fingerprint/citation_group_id）、`Round`、`TaskQueue`（depends_on_task_id/not_before）、`Batch`；`QueueState.WAITING`。
- Produces: `FingerprintGroup`（dataclass）、`compute_batch_groups(sessions) -> tuple[FingerprintGroup, ...]`、`passes_citation_threshold(confidence, category) -> bool`、`build_citation(...) -> dict`、`cascade_rewrite(citations, new_source, cited_version) -> dict[str, Any]`、`suspend_followers(session, groups, now) -> int`、`promote_on_leader_failed(session, groups, now) -> int`；迁移 0002：`batch` 增 `gate` JSONB 列（default `'{}'`）。

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/unit/test_aggregate.py
from aitrace.scheduling.aggregate import (
    build_citation,
    cascade_rewrite,
    compute_batch_groups,
    passes_citation_threshold,
)


class S:
    def __init__(self, session_id, stable_fp, batch_id="B1", citation_group_id=None):
        self.session_id = session_id
        self.stable_fingerprint = stable_fp
        self.batch_id = batch_id
        self.citation_group_id = citation_group_id


def test_groups_by_stable_fingerprint_within_batch() -> None:
    sessions = [S("s1", "fp-a"), S("s2", "fp-a"), S("s3", "fp-b"), S("s4", "fp-a", batch_id="B2")]
    groups = compute_batch_groups(sessions)
    assert len(groups) == 3
    b1_a = next(g for g in groups if g.batch_id == "B1" and g.stable_fp == "fp-a")
    assert {s.session_id for s in b1_a.members} == {"s1", "s2"}
    assert b1_a.leader.session_id == "s1"


def test_threshold_requires_confidence_and_category() -> None:
    assert passes_citation_threshold(0.7, "FW") is True
    assert passes_citation_threshold(0.69, "FW") is False
    assert passes_citation_threshold(0.9, "OTHER") is False
    assert passes_citation_threshold(None, "FW") is False


def test_build_citation_shapes() -> None:
    cited = build_citation("s1", 1, "rp-1", 2, "CITING", None)
    assert cited == {
        "source_session": "s1", "source_round_no": 1, "source_report_id": "rp-1",
        "cited_version": 2, "citing_status": "CITING", "cascaded_from": None,
    }


def test_cascade_rewrite_stores_previous_source() -> None:
    citations = {
        "s2": {"source_session": "s1", "source_round_no": 1, "source_report_id": "rp-1", "cited_version": 1, "citing_status": "CITING", "cascaded_from": None},
        "s3": {"source_session": "s1", "source_round_no": 1, "source_report_id": "rp-1", "cited_version": 1, "citing_status": "CITING", "cascaded_from": None},
    }
    rewritten = cascade_rewrite(citations, new_source=("s9", 3, "rp-9"), cited_version=2)
    assert rewritten["s2"]["citing_status"] == "CITING"
    assert rewritten["s2"]["source_session"] == "s9"
    assert rewritten["s2"]["cascaded_from"]["source_session"] == "s1"
    assert rewritten["s2"]["cited_version"] == 2


def test_cascade_rewrite_keeps_stale_when_excluded() -> None:
    citations = {"s2": {"source_session": "s1", "source_round_no": 1, "source_report_id": "rp-1", "cited_version": 1, "citing_status": "CITING", "cascaded_from": None}}
    rewritten = cascade_rewrite(citations, new_source=("s9", 3, "rp-9"), cited_version=2, excluded={"s2"})
    assert rewritten["s2"]["citing_status"] == "CITING_STALE"
    assert rewritten["s2"]["source_session"] == "s1"
```

- [ ] **Step 2: Write the failing integration tests**

```python
# tests/integration/test_aggregate_db.py
from datetime import UTC, datetime

from aitrace.core.enums import QueueState
from aitrace.persistence.models import SessionModel, TaskQueue
from aitrace.scheduling.aggregate import compute_batch_groups, promote_on_leader_failed, suspend_followers
from sqlalchemy import select


def _session(db_session, sid, fp, batch="B1") -> SessionModel:
    row = SessionModel(
        session_id=sid, product_id="ssd_9560", batch_id=batch, case_id=sid,
        domain_id="enterprise", profile_version="v1", trigger_user_id="zhangsan",
        hardware_type="SSD", stable_fingerprint=fp, env_topology={}, git_context={},
        created_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    db_session.add(row)
    return row


def test_suspend_followers_sets_waiting_and_dependency(db_session) -> None:
    leader = _session(db_session, "s1", "fp-a")
    follower = _session(db_session, "s2", "fp-a")
    db_session.flush()
    groups = compute_batch_groups(db_session.execute(select(SessionModel)).scalars().all())
    suspended = suspend_followers(db_session, groups, datetime(2026, 11, 26, tzinfo=UTC))
    assert suspended == 1
    # 跟随者任务置 WAITING 且依赖首例任务
    follower_task = db_session.execute(select(TaskQueue).where(TaskQueue.session_id == "s2")).scalar_one_or_none()
    leader_task = db_session.execute(select(TaskQueue).where(TaskQueue.session_id == "s1")).scalar_one_or_none()
    assert follower_task is not None and leader_task is not None
    assert follower_task.queue_state == QueueState.WAITING.value
    assert follower_task.depends_on_task_id == leader_task.task_id


def test_promotion_picks_next_member_when_leader_failed(db_session) -> None:
    _session(db_session, "s1", "fp-a")
    follower = _session(db_session, "s2", "fp-a")
    db_session.flush()
    groups = compute_batch_groups(db_session.execute(select(SessionModel)).scalars().all())
    promoted = promote_on_leader_failed(db_session, groups, datetime(2026, 11, 26, tzinfo=UTC))
    assert promoted == 1
    follower_task = db_session.execute(select(TaskQueue).where(TaskQueue.session_id == "s2")).scalar_one_or_none()
    assert follower_task is not None
    assert follower_task.queue_state == QueueState.READY.value
    assert follower_task.depends_on_task_id is None
```

Note: 上两例依赖任务存在（`_session` 需同时插入对应 `TaskQueue` 行，或改用 ingest 构造——实现者补全：leader/follower 各插一条 `PENDING case_clean` 任务与 `Round`/`UserQueue` 行，字段按 M0 模型必填列）。

- [ ] **Step 3: Run and confirm failure**

Run: `pytest tests/unit/test_aggregate.py tests/integration/test_aggregate_db.py -v`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 4: Implement aggregation**

```python
# src/aitrace/scheduling/aggregate.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState
from aitrace.persistence.models import SessionModel, TaskQueue

_CITABLE_MIN_CONFIDENCE = 0.7


@dataclass(frozen=True)
class FingerprintGroup:
    batch_id: str
    stable_fp: str
    leader: SessionModel
    members: tuple[SessionModel, ...]


def compute_batch_groups(sessions: Iterable[SessionModel]) -> tuple[FingerprintGroup, ...]:
    by_key: dict[tuple[str, str], list[SessionModel]] = {}
    for s in sessions:
        if s.stable_fingerprint:
            by_key.setdefault((s.batch_id, s.stable_fingerprint), []).append(s)
    groups = []
    for (batch_id, fp), members in by_key.items():
        ordered = sorted(members, key=lambda m: m.created_at)
        groups.append(FingerprintGroup(batch_id, fp, ordered[0], tuple(ordered)))
    return tuple(groups)


def passes_citation_threshold(confidence: float | None, category: str | None) -> bool:
    return confidence is not None and confidence >= _CITABLE_MIN_CONFIDENCE and category != "OTHER"


def build_citation(
    source_session: str,
    source_round_no: int,
    source_report_id: str,
    cited_version: int,
    citing_status: str,
    cascaded_from: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "source_session": source_session,
        "source_round_no": source_round_no,
        "source_report_id": source_report_id,
        "cited_version": cited_version,
        "citing_status": citing_status,
        "cascaded_from": cascaded_from,
    }


def cascade_rewrite(
    citations: dict[str, dict[str, Any]],
    *,
    new_source: tuple[str, int, str],
    cited_version: int,
    excluded: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    excluded = excluded or set()
    rewritten: dict[str, dict[str, Any]] = {}
    for session_id, citation in citations.items():
        if session_id in excluded:
            rewritten[session_id] = {**citation, "citing_status": "CITING_STALE"}
            continue
        previous = {
            "source_session": citation["source_session"],
            "source_round_no": citation["source_round_no"],
            "source_report_id": citation["source_report_id"],
        }
        rewritten[session_id] = {
            "source_session": new_source[0],
            "source_round_no": new_source[1],
            "source_report_id": new_source[2],
            "cited_version": cited_version,
            "citing_status": "CITING",
            "cascaded_from": previous,
        }
    return rewritten


def suspend_followers(session: Session, groups: Iterable[FingerprintGroup], now: datetime) -> int:
    suspended = 0
    for group in groups:
        leader_task = _clean_task(session, group.leader.session_id)
        if leader_task is None:
            continue
        for member in group.members[1:]:
            task = _clean_task(session, member.session_id)
            if task is None or task.queue_state != QueueState.PENDING.value:
                continue
            task.queue_state = QueueState.WAITING.value
            task.depends_on_task_id = leader_task.task_id
            task.updated_at = now
            member.citation_group_id = group.leader.session_id
            suspended += 1
    session.flush()
    return suspended


def promote_on_leader_failed(session: Session, groups: Iterable[FingerprintGroup], now: datetime) -> int:
    promoted = 0
    for group in groups:
        leader_round = _latest_round(session, group.leader.session_id)
        if leader_round is None or leader_round.round_status != "FAILED":
            continue
        for member in group.members[1:]:
            task = _clean_task(session, member.session_id)
            if task is None or task.queue_state != QueueState.WAITING.value:
                continue
            task.queue_state = QueueState.READY.value
            task.depends_on_task_id = None
            task.updated_at = now
            member.citation_group_id = member.session_id
            promoted += 1
            break
    session.flush()
    return promoted


def _clean_task(session: Session, session_id: str) -> TaskQueue | None:
    return session.execute(
        select(TaskQueue)
        .where(TaskQueue.session_id == session_id, TaskQueue.task_type == "case_clean")
        .order_by(TaskQueue.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_round(session: Session, session_id: str):
    from aitrace.persistence.models import Round
    return session.execute(
        select(Round).where(Round.session_id == session_id).order_by(Round.round_no.desc()).limit(1)
    ).scalar_one_or_none()
```

- [ ] **Step 5: Migration 0002**

```python
# src/aitrace/persistence/migrations/versions/0002_batch_gate.py
"""batch.gate JSONB 列（聚合闸预算与分组快照）

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("batch", sa.Column("gate", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    op.drop_column("batch", "gate")
```

同时给 `src/aitrace/persistence/models.py` 的 `Batch` 加：

```python
    gate: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_aggregate.py tests/integration/test_aggregate_db.py -v && make test`
Expected: 9 passed；全套绿。注意：integration 测试库需先 `alembic upgrade head`（conftest 的 `db_engine` fixture 已自动执行）。

- [ ] **Step 7: Commit checkpoint（跳过——用户约束不提交）**

---

## M1 Acceptance Checklist

- [ ] 四源插件：ANSI/`\r` 剥离、串口乱码清洗与心跳折叠、dmesg 白名单与重复折叠、调度状态压缩，均有单测锁定（Task 1/2）。
- [ ] 首错定位产出 `T0/异常类型/错误码`，关键词类别闭集映射，相关源判定（Task 3）。
- [ ] 跨源对齐：测试/调度 UTC 毫秒、dmesg 开机相对秒换算、串口 tick 互锁锚定；对齐率入指标（Task 4）。
- [ ] 锚点级联前移至最早硬异常（串口 > dmesg > 测试首错），无首错以 `failed_at_utc` 兜底（Task 4）。
- [ ] 三段切片：≤1MB 全量；超预算按「前端基线 → 收尾 → 首错窗」收缩；必保行豁免（Task 5）。
- [ ] 双层指纹：精确指纹（含路径行号）批内去重计数；稳定指纹（不含路径行号）供案例库聚簇（Task 6）。
- [ ] L1 落盘 `evidence/{session_id}/round_{n}/`，切片 SHA-256 回填 `referenced_logs`，脱水指标含压缩比/事件数/耗时（Task 7）。
- [ ] 截流闸五条规则（锁板冲突/盘被占用/Agent 失联/任务取消/PCIe Link Down）命中即出 `TEST_ENV_ERROR` 报告回写、任务 DONE、不进 AI（Task 8/9）。
- [ ] 清洗 Worker：claim→pipeline→finish；成功 READY、规则命中 DONE、失败自动重试一次后 FAILED（Task 9）。
- [ ] 聚合闸：批内稳定指纹分组、跟随者 WAITING 挂起依赖首例、首例 FAILED 晋升、引用门槛纯函数、级联改写存 `cascaded_from`（Task 10）。
- [ ] 迁移 0002（batch.gate）可升级可回滚；`make lint`、`make test`、`make contract`、`make e2e` 全绿。

## Handoff to M2

M2 消费：`QueueRepository.claim_ai`（M0 已冻结）、`SessionModel.stable_fingerprint`、`Round.exact_fingerprint`、L1 `summary.json`、`passes_citation_threshold`/`build_citation`/`cascade_rewrite`/`promote_on_leader_failed`、`Batch.gate` 预算计数。M2 不得改 M1 的解析/切片/指纹算法与 L1 目录结构；AI 结论回填时用 `finish(READY 任务 → DONE)` 走 M0 槽位闸语义。

## 执行记录（2026-11-26，M1 实施完成）

实施采用 SDD 子代理逐任务执行 + 控制器内联评审（用户裁定：不派评审子代理、不自动提交）。全部 10 个任务完成；四道门禁全绿（`make lint` / `make test` 154 passed cov 95.93% / `make contract` 14 / `make e2e` 2）；迁移 0002 在测试库升降级往返实测通过。

**对计划文本的裁定与修正**（实施期发现，未改验收口径）：

1. Task 3：`ASSERT gpf` 归 power（gpf 针）与 `_EXC` 无法匹配 `AssertionError:` 两处计划缺陷——关键词集去掉 gpf 针；`_EXC` 增两分组（名称后冒号）且不得吞日志级别；首错时间取最早硬异常时间戳、详情取首个携带异常名/错误码的硬事件。
2. Task 4：串口无 tick 关键行缺时间证据——最后一个 tick 锚点前向carry；计划测试的键字典为冻结全文子串——测试改用精确全文；`AlignedContext` 增 `failed_at_utc_ms`，`lock_anchor` 兜底回退 source="test"。
3. Task 5：`SliceParams` 计划字段名与真实模型不符——测试与实现一律改用真实字段（baseline_lines/first_error_before_s/…/first_error_window_budget_bytes）；计划测试 3/4/5 的构造无法触达目标分支——按计划测试自身注释的意图修正构造（放大切片、命令关键行出窗、补填充行）。
4. Task 6/7/8：行为与计划一致，仅 ruff 格式化修整；Task 8 落地 5 条截流规则（锁板冲突/盘被占用/Agent 失联/任务取消/PCIe Link Down）为 `deploy/config/rules/cutoff.yaml`，`StorageSettings` 增 `cutoff_rules_file`。
5. Task 9：golden scheduler/dmesg 本身命中截流规则，ready 路径单测必须消毒这两路后才可断言 ready；M0 `retry` 的 error 为必填——`_fail` 显式传 error；`ResolvedProfile` 字段是 `config` 而非 `profile`；loader 真名是 `load_profiles`；worker 增会话缺失 ValueError 守卫。
6. Task 10：S 桩缺 `created_at`（分组排序需要）——补固定默认值；晋升测试原样永远无法晋升（跟随者 PENDING、首例 RUNNING）——流程改为先 suspend 再置首例 Round FAILED 再 promote；`_session` 需同时插入 SessionModel + Round + TaskQueue 行（计划注释授权）；`_latest_round` 补 mypy 注解。
7. 提交纪律：全程未做任何 git 提交（用户约束：不自动提交、分阶段验收）。

**M2 移交**：接口按 Handoff 节冻结（`claim_ai`/`stable_fingerprint`/`exact_fingerprint`/L1 `summary.json`/聚合闸纯函数/`Batch.gate`）。M2 不得改 M1 的解析/切片/指纹算法与 L1 目录结构；AI 结论回填用 `finish(READY 任务 → DONE)` 走 M0 槽位闸语义。
