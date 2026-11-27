"""生成 mock aitest 的多 case 真实日志（确定性、可复现）。

运行：.venv/bin/python mocks/aitest/tools/generate_logs.py

覆盖场景：
- 5 个失败 case + 2 个通过 case；四路日志（test/uart/dmesg/scheduler）时间戳
  与 env_topology.host_boot_time_utc（BOOT）严格对齐：uart @TICK 为开机毫秒数、
  dmesg [sec.frac] 为开机相对秒、test/scheduler 为绝对 ISO UTC。
- 大小不一：CASE-1021 ~128KB、CASE-1048 ~200KB、CASE-1055 ~26KB（合计 ≤1MiB
  → 不切片走 full）；CASE-1035 ~1.2MiB（>1MiB → 三段切片，key line 远小于
  5MiB 不收缩）；CASE-1042 ~6.4MB（key line >5MiB → 切片且触发收缩）。
- 截流闸（deploy/config/rules/cutoff.yaml）只命中预期 case：
  CASE-1035 dmesg「PCIe Link Down」、CASE-1048 test「device busy」、
  CASE-1055 scheduler「锁板冲突」；其余日志内容刻意避开全部规则模式。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS_ROOT = ROOT / "logs" / "var" / "log" / "aitest"

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
BATCH = "BATCH-20261120-0089"
SN = "SN20261120001"
FW = "2.3.1"


def at(seconds: float) -> datetime:
    return BOOT + timedelta(seconds=seconds)


def iso(seconds: float) -> str:
    """绝对时间戳（秒精度）。"""
    return at(seconds).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(seconds: float) -> str:
    """绝对时间戳（毫秒精度）。"""
    stamp = at(seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{stamp.microsecond // 1000:03d}Z"


def dmesg_rel(seconds: float) -> str:
    """dmesg 开机相对时间戳：[ 8398.912400]。"""
    return f"[{seconds:10.6f}]"


def tick(seconds: float) -> str:
    return f"@TICK {int(seconds * 1000)}"


def scheduler_base(
    case_id: str, script: str, start: float, fail: float, exit_code: int
) -> list[str]:
    lines = [
        f"{iso(start)} scheduler v3.2.1 pid=4120 start batch={BATCH}",
        f"{iso(start + 1)} STATE ENV_INIT device=nvme-ssd-001 host=node-45 fw={FW}",
        f"{iso(start + 2)} STATE LOCK_DEVICE nvme-ssd-001 slot=slot-0 holder=zhangsan",
        f"{iso(start + 3)} STATE RUN case={case_id} script={script}",
    ]
    t = start + 60
    while t < fail:
        pct = int((t - start) / (fail - start) * 100)
        lines.append(
            f"{iso(t)} STATE CHECKPOINT case={case_id} progress={pct}% "
            f"iops={284_000 + (int(t) % 7) * 311}"
        )
        t += 60
    return lines


def scheduler_tail(case_id: str, fail: float, error_line: str, exit_code: int) -> list[str]:
    return [
        f"{iso(fail)} {error_line}",
        f"{iso(fail + 60)} STATE TERMINATED case={case_id} exit={exit_code}",
        f"{iso(fail + 62)} STATE EXIT rc={exit_code} duration_report=written",
    ]


def test_base(case_id: str, script: str, start: float, cmd_ticks: list[float]) -> list[str]:
    lines = [
        f"{iso_ms(start)} INFO aitest-runner v2.8.0 case={case_id} start script={script}",
        f"{iso_ms(start + 0.5)} INFO device probe: /dev/nvme0n1 model=SSD-9560 sn={SN} fw={FW}",
        f"{iso_ms(start + 1.2)} INFO precondition: smart health ok, temperature 41C",
    ]
    for t in cmd_ticks:
        lines.append(f"{iso_ms(t)} INFO runner: {CMD_TEXT[t]} @CMD uart_tick={int(t * 1000)}")
    return lines


def test_progress(start: float, fail: float, step: int, prefix: str) -> list[str]:
    lines = []
    t = start + 30
    while t < fail:
        lines.append(f"{iso_ms(t)} INFO {prefix} loop progress {(t - start):.0f}s ok")
        t += step
    return lines


CMD_TEXT: dict[float, str] = {}


def uart_header() -> list[str]:
    return [
        "[console] FW console baud=115200 loglevel=6",
        f"[console] device {SN} fw {FW}",
        "Build ID: 2.3.1-20261101.abc123",
        f"{tick(0)} nvme0: firmware {FW} boot seq=1 zone_map=ok",
    ]


def uart_heartbeats(start: float, end: float, step: float) -> list[str]:
    lines = []
    t = start
    while t < end:
        lines.append(f"{tick(t)} heartbeat zone-0 idle_util={12 + int(t) % 5}%")
        t += step
    return lines


def uart_periodic(start: float, end: float, step: float, text: str) -> list[str]:
    lines = []
    t = start
    while t < end:
        lines.append(f"{tick(t)} {text} iops={284_000 + (int(t) % 7) * 311} qd=32")
        t += step
    return lines


def dmesg_boot() -> list[str]:
    return [
        f"{dmesg_rel(0.000000)} Linux version 5.14.0-427.el9.x86_64 (builder@node-45)",
        f"{dmesg_rel(0.356001)} pci 0000:01:00.0: [0x1c58:0x0102] type 00 class 0x010802",
        f"{dmesg_rel(0.512340)} nvme nvme0: pci function 0000:01:00.0",
        f"{dmesg_rel(1.204512)} nvme nvme0: missing or invalid SUBNQN field",
        f"{dmesg_rel(1.318770)} nvme nvme0: 8/0/0 default/read/poll queues",
    ]


def dmesg_periodic(start: float, end: float, step: float, text: str) -> list[str]:
    lines = []
    t = start
    while t < end:
        lines.append(f"{dmesg_rel(t)} {text} temperature={38 + int(t) % 6}C wear=3%")
        t += step
    return lines


def write_case(case_id: str, files: dict[str, list[str]]) -> dict[str, int]:
    case_dir = LOGS_ROOT / case_id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    sizes: dict[str, int] = {}
    for name, lines in files.items():
        data = "\n".join(lines) + "\n"
        (case_dir / name).write_text(data, encoding="utf-8")
        sizes[name] = len(data.encode("utf-8"))
    return sizes


def case_1021() -> dict[str, int]:
    """随机写耐久压缩模拟 → 固件断言（进 AI）。~128KB，不切片。"""
    start, fail = 600.0, 8400.0
    CMD_TEXT.update({60.0: "nvme smart-log /dev/nvme0n1", 600.0: "nvme id-ctrl /dev/nvme0n1"})
    test = test_base("CASE-1021", "cases/endurance/random_write_72h.py", start, [60.0, 600.0])
    test += test_progress(start, fail, 120, "random_write")
    test += [
        f"{iso_ms(fail)} ERROR TimeoutException: device /dev/nvme0n1 no response "
        f"in 3000ms err_code=0x1A",
        f"{iso_ms(fail)} Traceback (most recent call last):",
        f'{iso_ms(fail)}   File "cases/endurance/random_write_72h.py", line 412, in run_loop',
        f'{iso_ms(fail)}   File "cases/lib/io_worker.py", line 96, in submit_and_wait',
        f"{iso_ms(fail)} TimeoutException: uart no response err_code=0x1A",
        f"{iso_ms(fail + 1)} INFO teardown begins",
        f"{iso_ms(fail + 2)} exit code 1",
    ]
    uart = uart_header()
    uart += uart_periodic(5, fail - 4, 5, "nvme0: io completion")
    uart += uart_heartbeats(2.5, fail - 4, 10)
    uart += [f"{tick(100.0 + i)} heartbeat zone-0 idle_util={12 + i}% flood" for i in range(6)]
    uart += [
        f"{tick(fail - 1.8)} ASSERT fw_sched.c:887 io tag=41 qd=32 timeout 3000ms exceeded",
        f"{tick(fail - 1.75)} RESET: controller panic path entered, flushing 8 queues",
        f"{tick(fail - 1.6)} nvme0: csts=0x8 cfs=0x1 fatal firmware fault",
        f"{tick(fail + 2.0)} post-mortem: core dump seg-1 written to slot-0 spare",
        f"{tick(fail + 4.5)} console idle, awaiting host command",
    ]
    dmesg = dmesg_boot()
    dmesg += dmesg_periodic(10, fail - 3, 10, "nvme nvme0: periodic health")
    dmesg += [
        f"{dmesg_rel(fail - 1.1)} nvme nvme0: I/O 41 QID 3 timeout, aborting",
        f"{dmesg_rel(fail - 0.6)} nvme nvme0: controller reset done, fatal status detected",
        f"{dmesg_rel(fail - 0.2)} nvme nvme0: 8/0/0 default/read/poll queues (re-probe)",
        f"{dmesg_rel(fail + 1.5)} nvme nvme0: device not ready, waiting controller reset",
    ]
    sched = scheduler_base("CASE-1021", "cases/endurance/random_write_72h.py", start, fail, 1)
    sched += scheduler_tail(
        "CASE-1021", fail, "ERROR case CASE-1021 io timeout, device nvme-ssd-001 unresponsive", 1
    )
    return write_case(
        "CASE-1021",
        {"test.log": test, "uart.log": uart, "dmesg.log": dmesg, "scheduler.log": sched},
    )


def case_1035() -> dict[str, int]:
    """老化后重扫描 → PCIe Link Down（截流闸 TEST_ENV_ERROR）。~1.2MiB，切片不收缩。"""
    start, fail = 10800.0, 16200.0
    CMD_TEXT.update({10860.0: "nvme reset /dev/nvme0n1", 13000.0: "nvme smart-log /dev/nvme0n1"})
    test = test_base("CASE-1035", "cases/aging/rescan_power_cycle.py", start, [10860.0, 13000.0])
    test += test_progress(start, fail, 30, "aging_rescan")
    test += [
        f"{iso_ms(fail)} ERROR DeviceGoneError: device /dev/nvme0n1 removed from bus err_code=0x2F",
        f"{iso_ms(fail)} Traceback (most recent call last):",
        f'{iso_ms(fail)}   File "cases/aging/rescan_power_cycle.py", line 188, in cycle',
        f"{iso_ms(fail)} DeviceGoneError: nvme0n1 not found after rescan",
        f"{iso_ms(fail + 1)} exit code 1",
    ]
    uart = uart_header()
    uart += uart_periodic(1, fail - 4, 1, "nvme0: io completion")
    uart += [
        f"{tick(fail - 3.0)} nvme0: slot power cycle requested by host",
        f"{tick(fail - 2.0)} RESET nvme0: hot reset initiated",
        f"{tick(fail - 1.5)} WATCHDOG: slot-0 power good lost 320ms",
    ]
    dmesg = dmesg_boot()
    dmesg += dmesg_periodic(2, fail - 5, 2, "nvme nvme0: periodic health")
    dmesg += [
        f"{dmesg_rel(fail - 20.0)} pcieport 0000:00:01.0: AER: "
        f"Multiple Uncorrected Non-Fatal error received",
        f"{dmesg_rel(fail - 18.5)} pcieport 0000:00:01.0: AER: device recovery failed",
        f"{dmesg_rel(fail - 2.0)} nvme nvme0: PCIe Link Down detected, link status 0x2f1",
        f"{dmesg_rel(fail - 1.0)} nvme nvme0: controller reset in progress",
        f"{dmesg_rel(fail - 0.5)} pcieport 0000:00:01.0: link training failed 3 times",
    ]
    sched = scheduler_base("CASE-1035", "cases/aging/rescan_power_cycle.py", start, fail, 1)
    sched += scheduler_tail(
        "CASE-1035",
        fail,
        "ERROR case CASE-1035 device nvme-ssd-001 lost (wait rescan 30s TIMEOUT)",
        1,
    )
    return write_case(
        "CASE-1035",
        {"test.log": test, "uart.log": uart, "dmesg.log": dmesg, "scheduler.log": sched},
    )


def case_1042() -> dict[str, int]:
    """长稳 I/O 超时风暴 + watchdog → AI 分析。~6.4MB，切片且收缩（key line >5MiB）。"""
    start, fail, storm_start = 3600.0, 32400.0, 31460.0
    CMD_TEXT.update({3660.0: "nvme smart-log /dev/nvme0n1", 18000.0: "nvme error-log /dev/nvme0n1"})
    test = test_base("CASE-1042", "cases/stability/longrun_io_72h.py", start, [3660.0, 18000.0])
    test += test_progress(start, storm_start, 60, "longrun_io")
    t = storm_start
    while t < fail:
        test.append(
            f"{iso_ms(t)} ERROR io_worker: submit latency {900 + (int(t) % 13) * 7}ms "
            f"exceeds sla err_code=0x41"
        )
        t += 10
    test += [
        f"{iso_ms(fail)} ERROR TimeoutException: queue depth stuck at 32 for 900s err_code=0x41",
        f"{iso_ms(fail)} Traceback (most recent call last):",
        f'{iso_ms(fail)}   File "cases/stability/longrun_io_72h.py", line 301, in watchdog_loop',
        f"{iso_ms(fail)} TimeoutException: no completion in 900s window",
        f"{iso_ms(fail + 1)} exit code 1",
    ]
    uart = uart_header()
    uart += uart_periodic(5, storm_start, 5, "nvme0: io completion")
    uart += uart_heartbeats(2.5, storm_start, 15)
    base_ms = int(storm_start * 1000)
    for i in range(int((fail - storm_start) * 100)):
        uart.append(
            f"@TICK {base_ms + i * 10} TIMEOUT io tag={41 + i % 7} "
            f"qid=3 abort queued retry={i % 3 + 1}"
        )
    uart += [
        f"{tick(fail - 0.5)} WATCHDOG: fw watchdog fired, dumping core seq=2",
        f"{tick(fail - 0.4)} PANIC fw_core.c:211 assert dump_pending==0 failed",
    ]
    dmesg = dmesg_boot()
    dmesg += dmesg_periodic(2, storm_start, 2, "nvme nvme0: periodic health")
    t = storm_start
    while t < fail:
        dmesg.append(
            f"{dmesg_rel(t)} pcieport 0000:00:01.0: AER: "
            f"Corrected error received, 0x{int(t) % 99:04d}"
        )
        t += 0.5
    dmesg += [
        f"{dmesg_rel(fail - 1.0)} nvme nvme0: I/O 41 QID 3 timeout, aborting",
        f"{dmesg_rel(fail - 0.6)} BUG: kernel NULL pointer dereference, nvme complete_cmd",
        f"{dmesg_rel(fail - 0.5)} Call Trace:",
        f"{dmesg_rel(fail - 0.5)}  nvme_complete_rq+0x1a2/0x210",
        f"{dmesg_rel(fail - 0.4)} nvme nvme0: controller reset done after fatal handshake",
        f"{dmesg_rel(fail + 1.1)} nvme nvme0: device offline, removing namespace head",
    ]
    sched = scheduler_base("CASE-1042", "cases/stability/longrun_io_72h.py", start, fail, 1)
    sched += scheduler_tail(
        "CASE-1042", fail, "ERROR case CASE-1042 io storm, device nvme-ssd-001 queue stuck", 1
    )
    return write_case(
        "CASE-1042",
        {"test.log": test, "uart.log": uart, "dmesg.log": dmesg, "scheduler.log": sched},
    )


def case_1048() -> dict[str, int]:
    """测试环境盘被占用 → 截流闸 TEST_ENV_ERROR（device_busy，test 源）。~430KB，不切片。

    主机整批只开机一次（env_topology.host_boot_time_utc），故 uart/dmesg 覆盖
    开机 → 本 case 失败的全时段；test/scheduler 仅覆盖本 case 的执行窗口。
    """
    start, fail = 12600.0, 13020.0
    CMD_TEXT.update({12606.0: "nvme list /dev/nvme0n1", 12810.0: "nvme smart-log /dev/nvme0n1"})
    test = test_base("CASE-1048", "cases/basic/trim_verify.py", start, [12606.0, 12810.0])
    test += test_progress(start, fail - 30, 60, "trim_verify")
    test += [
        f"{iso_ms(fail)} ERROR nvme ioctl(NVME_IOCTL_ADMIN_CMD) failed: device busy err_code=0x07",
        f"{iso_ms(fail)} Traceback (most recent call last):",
        f'{iso_ms(fail)}   File "cases/basic/trim_verify.py", line 77, in open_device',
        f"{iso_ms(fail)} OSError: errno 16 device busy, holder=ci-runner-07",
        f"{iso_ms(fail + 1)} exit code 1",
    ]
    uart = uart_header()
    uart += uart_periodic(2, fail - 4, 2, "nvme0: io completion")
    dmesg = dmesg_boot()
    dmesg += dmesg_periodic(10, fail - 3, 10, "nvme nvme0: periodic health")
    sched = scheduler_base("CASE-1048", "cases/basic/trim_verify.py", start, fail, 1)
    sched += scheduler_tail(
        "CASE-1048", fail, "ERROR case CASE-1048 device nvme-ssd-001 locked by other job", 1
    )
    return write_case(
        "CASE-1048",
        {"test.log": test, "uart.log": uart, "dmesg.log": dmesg, "scheduler.log": sched},
    )


def case_1055() -> dict[str, int]:
    """调度器锁板冲突 → 截流闸 TEST_ENV_ERROR（lock_conflict，scheduler 源）。
    环境级故障未触达盘卡：仅 scheduler + test 两路日志（可用性缺口场景）。~40KB。"""
    start, fail = 40200.0, 40800.0
    test = [
        f"{iso_ms(start)} INFO aitest-runner v2.8.0 "
        f"case=CASE-1055 start script=cases/raid/pair_write.py",
        f"{iso_ms(start + 0.5)} INFO allocation request: slot-0 + slot-1 (raid pair mode)",
        f"{iso_ms(start + 1)} INFO slot-0 granted (nvme-ssd-001), waiting slot-1 lease",
    ]
    t = start + 5
    attempt = 1
    while t < fail - 30:
        test.append(
            f"{iso_ms(t)} WARN slot-1 busy, retry allocation (attempt {attempt}/60, "
            f"holder=ci-runner-07 lease_remaining={int(fail - t + 2400)}s)"
        )
        attempt += 1
        t += 60
    test += [
        f"{iso_ms(fail)} ERROR AllocationRefusedError: "
        f"scheduler refused pair allocation err_code=0x55",
        f"{iso_ms(fail)} Traceback (most recent call last):",
        f'{iso_ms(fail)}   File "cases/raid/pair_write.py", line 44, in acquire_devices',
        f"{iso_ms(fail)} AllocationRefusedError: slot-1 held by ci-runner-07",
        f"{iso_ms(fail + 1)} exit code 1",
    ]
    sched = scheduler_base("CASE-1055", "cases/raid/pair_write.py", start, fail, 1)
    sched += [
        f"{iso(fail - 300)} WARN slot-1 lock holder ci-runner-07 alive, lease expires in 2700s",
        f"{iso(fail - 60)} WARN slot-1 lock holder ci-runner-07 "
        f"still alive, lease expires in 2400s",
    ]
    sched += scheduler_tail(
        "CASE-1055",
        fail,
        "ERROR 锁板冲突：slot-1 被 CI 长稳任务锁定（holder=ci-runner-07），无法调度 CASE-1055",
        1,
    )
    return write_case("CASE-1055", {"test.log": test, "scheduler.log": sched})


def main() -> None:
    sizes: dict[str, dict[str, int]] = {}
    sizes["CASE-1021"] = case_1021()
    sizes["CASE-1035"] = case_1035()
    sizes["CASE-1042"] = case_1042()
    sizes["CASE-1048"] = case_1048()
    sizes["CASE-1055"] = case_1055()
    total = 0
    for case_id, files in sizes.items():
        subtotal = sum(files.values())
        total += subtotal
        detail = ", ".join(f"{name}={size / 1024:.0f}KB" for name, size in files.items())
        print(f"{case_id}: {subtotal / 1024:.0f}KB ({detail})")
    print(f"total: {total / 1024 / 1024:.2f}MB")


if __name__ == "__main__":
    main()
