from __future__ import annotations

import os
import ctypes as c
import math
import plistlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable

from asmond_models import ProcessGpuProbeState, ProcessIdentity, ProcessInfo
from asmond_system import POWERMETRICS_PATH, PS_PATH, SUDO_PATH, system_environment


PROCESS_GPU_SAMPLE_MS = 1000
RunCommand = Callable[..., subprocess.CompletedProcess]
RefreshSudo = Callable[[bool], tuple[bool, str]]
NeedsSudo = Callable[[], bool]


class _RusageInfoV6(c.Structure):
    # Apple's sys/resource.h, RUSAGE_INFO_V6. Keep the complete 464-byte
    # buffer: the kernel writes later fields even though we only read energy.
    _fields_ = [("ri_uuid", c.c_uint8 * 16)] + [(name, c.c_uint64) for name in """
ri_user_time ri_system_time ri_pkg_idle_wkups ri_interrupt_wkups ri_pageins
ri_wired_size ri_resident_size ri_phys_footprint ri_proc_start_abstime
ri_proc_exit_abstime ri_child_user_time ri_child_system_time ri_child_pkg_idle_wkups
ri_child_interrupt_wkups ri_child_pageins ri_child_elapsed_abstime ri_diskio_bytesread
ri_diskio_byteswritten ri_cpu_time_qos_default ri_cpu_time_qos_maintenance
ri_cpu_time_qos_background ri_cpu_time_qos_utility ri_cpu_time_qos_legacy
ri_cpu_time_qos_user_initiated ri_cpu_time_qos_user_interactive ri_billed_system_time
ri_serviced_system_time ri_logical_writes ri_lifetime_max_phys_footprint ri_instructions
ri_cycles ri_billed_energy ri_serviced_energy ri_interval_max_phys_footprint
ri_runnable_time ri_flags ri_user_ptime ri_system_ptime ri_pinstructions ri_pcycles
ri_energy_nj ri_penergy_nj ri_secure_time_in_system ri_secure_ptime_in_system
ri_neural_footprint ri_lifetime_max_neural_footprint ri_interval_max_neural_footprint
ri_conclave_footprint ri_page_wait_time_mach ri_page_cache_hits
""".split()] + [("ri_reserved", c.c_uint64 * 6)]


class _ProcessEnergyReader:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("process energy requires macOS")
        self.lib = c.CDLL("/usr/lib/libproc.dylib")
        self.lib.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
        self.lib.proc_pid_rusage.restype = c.c_int

    def __call__(self, pid: int) -> tuple[int, bytes, int] | None:
        if not 0 < pid <= 0x7FFFFFFF:
            return None
        usage = _RusageInfoV6()
        if self.lib.proc_pid_rusage(pid, 6, c.byref(usage)) != 0:
            return None
        return usage.ri_proc_start_abstime, bytes(usage.ri_uuid), usage.ri_energy_nj


class ProcessPowerSampler:
    """Apple's attributed CPU-energy estimate, not whole-process power.

    Keep baselines in the existing process worker. A progressing own-process
    counter proves the source works; Intel/unsupported zero counters never
    turn into fake 0-W process rows. No extra privilege or sampling command.
    """

    def __init__(self, read_counter: Callable[[int], tuple[int, bytes, int] | None] | None = None,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._reader = read_counter
        self._attempted = read_counter is not None
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._previous: dict[int, tuple[tuple, int, int]] = {}
        self._previous_self: tuple[int, bytes, int] | None = None

    def _read(self, pid: int) -> tuple[int, bytes, int] | None:
        if not self._attempted:
            self._attempted = True
            try:
                self._reader = _ProcessEnergyReader()
            except (OSError, AttributeError):
                pass
        return self._reader(pid) if self._reader is not None else None

    def update(self, processes: list[ProcessInfo]) -> list[ProcessInfo]:
        for process in processes:
            process.cpu_power_mw = None
        own_pid = os.getpid()
        own = self._read(own_pid)
        own_at = self._clock()
        old_own = self._previous_self
        self._previous_self = own
        if own is None or own[2] <= 0:
            self._previous.clear()
            return processes
        functional = old_own is not None and own[:2] == old_own[:2] and own[2] > old_own[2]
        current: dict[int, tuple[tuple, int, int]] = {}
        for process in processes:
            counter = own if process.pid == own_pid else self._read(process.pid)
            at = own_at if process.pid == own_pid else self._clock()
            if counter is None or counter[0] <= 0 or counter[2] <= 0:
                continue
            identity = (counter[0], counter[1], process.start_time, process.ppid, process.user, process.full_command)
            previous = self._previous.get(process.pid)
            current[process.pid] = (identity, at, counter[2])
            if functional and previous is not None:
                old_identity, old_at, old_energy = previous
                if identity == old_identity and at > old_at and counter[2] >= old_energy:
                    # nJ / ns = W; the public field follows Asmond's mW units.
                    power = (counter[2] - old_energy) / (at - old_at) * 1000.0
                    if math.isfinite(power):
                        process.cpu_power_mw = power
        self._previous = current
        return processes


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_key(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", path.lower()).strip()


def flatten(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten(value, next_prefix)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            next_prefix = f"{prefix}[{index}]"
            yield from flatten(value, next_prefix)
    else:
        yield prefix, obj


def parse_number_text(text: str) -> float | None:
    value = text.strip()
    if not value:
        return None
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def parse_value(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int | float):
        number = float(value)
        return (number, None) if math.isfinite(number) else (None, None)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "ignore")
        except Exception:
            return None, None
    if isinstance(value, str):
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z%/]+)?", value)
        if match:
            number = float(match.group(1))
            if math.isfinite(number):
                return number, (match.group(2) or "").lower() or None
    return None, None


def invalid_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return math.isfinite(float(value)) and float(value) != 0.0
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "invalid"}


def parse_cpu_time(text: str) -> float | None:
    """Accumulated ps time, not its decaying %CPU. Preserve sub-second units."""
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):([0-5]\d)(?::([0-5]\d))?(\.\d{1,9})?", text)
    if not match:
        return None
    days, first, second, third, fraction = match.groups()
    if days is not None and third is None:
        return None
    value = int(first) * (3600 if third is not None else 60) + int(second) * (60 if third is not None else 1)
    value += int(third or 0) + int(days or 0) * 86400 + float(fraction or 0)
    return float(value)


def parse_process_line(line: str, *, include_cpu_time: bool = False) -> ProcessInfo | None:
    cpu_time = None
    if include_cpu_time:
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or (cpu_time := parse_cpu_time(parts[0])) is None:
            return None
        line = parts[1]
    parts_with_start = line.strip().split(None, 12)
    has_start_columns = (
        len(parts_with_start) >= 13
        and parts_with_start[2].isdigit()
        and parts_with_start[3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    )
    start_time = ""
    if has_start_columns:
        (
            pid_text,
            user,
            ppid_text,
            weekday,
            month,
            day,
            clock,
            year,
            etime,
            cpu_text,
            mem_text,
            rss_text,
            command_text,
        ) = parts_with_start
        start_time = f"{weekday} {month} {day} {clock} {year}"
    else:
        parts_with_user = line.strip().split(None, 7)
        has_user_columns = (
            len(parts_with_user) >= 8
            and parts_with_user[2].isdigit()
            and re.match(r"^(?:\d+-)?\d{1,2}:\d{2}(?::\d{2})?$", parts_with_user[3])
        )
        user = ""
        if has_user_columns:
            pid_text, user, ppid_text, etime, cpu_text, mem_text, rss_text, command_text = parts_with_user
        else:
            parts = line.strip().split(None, 6)
            has_extended_columns = len(parts) >= 7 and parts[1].isdigit() and re.match(r"^(?:\d+-)?\d{1,2}:\d{2}(?::\d{2})?$", parts[2])
            if has_extended_columns:
                pid_text, ppid_text, etime, cpu_text, mem_text, rss_text, command_text = parts
            elif len(parts) >= 5:
                pid_text, cpu_text, mem_text, rss_text, command_text = line.strip().split(None, 4)
                ppid_text = ""
                etime = ""
            else:
                return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    try:
        ppid = int(ppid_text) if ppid_text else None
    except ValueError:
        ppid = None
    cpu_pct = parse_number_text(cpu_text)
    mem_pct = parse_number_text(mem_text)
    rss_kib = parse_number_text(rss_text.replace(",", ""))
    if cpu_pct is None or mem_pct is None or rss_kib is None or cpu_pct < 0 or mem_pct < 0 or rss_kib < 0:
        return None
    command = os.path.basename(command_text) or command_text
    return ProcessInfo(pid, cpu_pct, mem_pct, int(rss_kib), command, ppid, etime, command_text, user, start_time=start_time, cpu_time_s=cpu_time)


def process_gpu_command(sample_ms: int, output_format: str = "plist", needs_sudo: NeedsSudo | None = None) -> list[str]:
    command = [
        POWERMETRICS_PATH,
        "--samplers",
        "tasks",
        "--show-process-gpu",
        "--show-process-energy",
        "--sample-rate",
        str(sample_ms),
        "--sample-count",
        "1",
        "--format",
        output_format,
        "--buffer-size",
        "1",
        "--handle-invalid-values",
    ]
    if needs_sudo is not None and needs_sudo():
        return [SUDO_PATH, "-n", *command]
    return command


def iter_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        if invalid_marker(obj.get("invalid")):
            return
        yield obj
        for value in obj.values():
            yield from iter_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_dicts(value)


def pid_from_mapping(mapping: dict[str, Any]) -> int | None:
    for key, value in mapping.items():
        norm = normalize_key(str(key))
        if norm not in {"pid", "process id", "processid", "task pid"}:
            continue
        number, _ = parse_value(value)
        if number is None:
            continue
        pid = int(number)
        if pid > 0:
            return pid
    return None


def gpu_duration_pct(path: str, value: Any, interval_s: float) -> float | None:
    number, unit = parse_value(value)
    if number is None or number < 0:
        return None
    key = normalize_key(path)
    if unit == "%" or "percent" in key or key.endswith("pct"):
        return clamp(number, 0.0, 100.0)
    if "ratio" in key or "fraction" in key:
        return clamp(number * 100.0 if number <= 1.0 else number, 0.0, 100.0)
    if "time" not in key and "duration" not in key:
        return None
    seconds: float | None
    if unit in {"ms", "ms/s", "msec", "millisecond", "milliseconds"} or " ms" in key or key.endswith("ms") or "msec" in key or "millisecond" in key:
        seconds = number / 1000.0
    elif unit in {"us", "us/s", "usec", "microsecond", "microseconds"} or " us" in key or key.endswith("us") or "usec" in key or "microsecond" in key:
        seconds = number / 1_000_000.0
    elif unit in {"ns", "ns/s", "nsec", "nanosecond", "nanoseconds"} or " ns" in key or key.endswith("ns") or "nsec" in key or "nanosecond" in key:
        seconds = number / 1_000_000_000.0
    elif unit in {"s", "s/s", "sec", "second", "seconds"} or " sec" in key or "second" in key:
        seconds = number
    elif "nano" in key:
        seconds = number / 1_000_000_000.0
    elif "micro" in key:
        seconds = number / 1_000_000.0
    elif "milli" in key:
        seconds = number / 1000.0
    elif number <= interval_s:
        seconds = number
    elif number <= interval_s * 1000.0:
        seconds = number / 1000.0
    elif number <= interval_s * 1_000_000.0:
        seconds = number / 1_000_000.0
    elif number <= interval_s * 1_000_000_000.0:
        seconds = number / 1_000_000_000.0
    else:
        return None
    return clamp(seconds / max(interval_s, 0.001) * 100.0, 0.0, 100.0)


def gpu_pct_from_process_mapping(mapping: dict[str, Any], interval_s: float) -> float | None:
    values: list[float] = []
    for path, value in flatten(mapping):
        key = normalize_key(path)
        if "gpu" not in key:
            continue
        if any(word in key for word in ("power", "energy", "freq", "frequency", "vendor", "device", "registry")):
            continue
        pct = gpu_duration_pct(path, value, interval_s)
        if pct is not None:
            values.append(pct)
    return max(values) if values else None


def process_gpu_pcts_from_plist(obj: Any, interval_s: float) -> dict[int, float]:
    if isinstance(obj, dict) and invalid_marker(obj.get("invalid")):
        return {}
    values: dict[int, float] = {}
    for mapping in iter_dicts(obj):
        pid = pid_from_mapping(mapping)
        if pid is None:
            continue
        gpu_pct = gpu_pct_from_process_mapping(mapping, interval_s)
        if gpu_pct is None:
            continue
        values[pid] = max(values.get(pid, 0.0), gpu_pct)
    return values


def parse_process_gpu_text_line(line: str) -> tuple[int, float] | None:
    if not line.strip() or line.lstrip().startswith("***"):
        return None
    match = re.match(
        r"^(.+?)\s+(-?\d+)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$",
        line,
    )
    if not match:
        return None
    pid = int(match.group(2))
    if pid < 0:
        return None
    gpu_ms_per_s = float(match.group(9))
    if not math.isfinite(gpu_ms_per_s) or gpu_ms_per_s < 0:
        return None
    return pid, clamp(gpu_ms_per_s / 10.0, 0.0, 100.0)


def process_gpu_pcts_from_text(text: str) -> dict[int, float]:
    values: dict[int, float] = {}
    for line in text.splitlines():
        parsed = parse_process_gpu_text_line(line)
        if parsed is None:
            continue
        pid, gpu_pct = parsed
        values[pid] = max(values.get(pid, 0.0), gpu_pct)
    if values and max(values.values()) <= 0.0:
        return {}
    return values


def read_process_gpu_pcts(
    sample_ms: int = PROCESS_GPU_SAMPLE_MS,
    probe_state: ProcessGpuProbeState | None = None,
    run: RunCommand = subprocess.run,
    refresh_sudo: RefreshSudo | None = None,
    needs_sudo: NeedsSudo | None = None,
) -> dict[int, float]:
    if probe_state is None:
        probe_state = ProcessGpuProbeState()
    if not probe_state.can_probe():
        return {}
    if refresh_sudo is not None:
        ok, _ = refresh_sudo(False)
        if not ok:
            probe_state.mark_unavailable()
            return {}
    try:
        text_proc = run(
            process_gpu_command(sample_ms, "text", needs_sudo=needs_sudo),
            check=False,
            capture_output=True,
            timeout=max(4.0, sample_ms / 1000.0 + 3.0),
            env=system_environment(),
        )
    except Exception:
        text_proc = None
    if text_proc is not None and text_proc.returncode == 0 and text_proc.stdout:
        text_values = process_gpu_pcts_from_text(text_proc.stdout.decode("utf-8", "ignore"))
        if text_values:
            probe_state.mark_available()
            return text_values
    try:
        proc = run(
            process_gpu_command(sample_ms, needs_sudo=needs_sudo),
            check=False,
            capture_output=True,
            timeout=max(4.0, sample_ms / 1000.0 + 3.0),
            env=system_environment(),
        )
    except Exception:
        probe_state.mark_unavailable()
        return {}
    if proc.returncode != 0 or not proc.stdout:
        probe_state.mark_unavailable()
        return {}
    merged: dict[int, float] = {}
    for raw in proc.stdout.split(b"\0"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = plistlib.loads(raw)
        except Exception:
            continue
        for pid, gpu_pct in process_gpu_pcts_from_plist(obj, sample_ms / 1000.0).items():
            merged[pid] = max(merged.get(pid, 0.0), gpu_pct)
    if merged:
        probe_state.mark_available()
    else:
        probe_state.mark_unavailable()
    return merged


def gpu_process_identity(process: ProcessInfo) -> ProcessIdentity:
    return process.pid, process.start_time, process.ppid, process.user, process.full_command


def merge_process_gpu_pcts(processes: list[ProcessInfo], gpu_pcts: dict[int, float]) -> list[ProcessInfo]:
    for process in processes:
        process.gpu_pct = gpu_pcts.get(process.pid)
    return processes


def read_processes(
    include_gpu: bool = False,
    run: RunCommand = subprocess.run,
    read_gpu: Callable[[], dict[int, float]] | None = None,
    full_command: bool = False,
    include_cpu_time: bool = False,
    strict: bool = False,
) -> list[ProcessInfo]:
    columns = "pid=,user=,ppid=,lstart=,etime=,pcpu=,pmem=,rss="
    command_field = "command=" if full_command else "comm="
    fallback_field = "comm=" if full_command else "command="
    commands = (
        [PS_PATH, "-axo", f"{columns},{command_field}"],
        [PS_PATH, "-axo", f"{columns},{fallback_field}"],
    )
    if include_cpu_time:
        # Recording never falls back to argv-bearing command= output.
        commands = ([PS_PATH, "-axo", f"time=,{columns},comm="],)
    processes: list[ProcessInfo] = []
    for command in commands:
        try:
            proc = run(command, check=False, capture_output=True, timeout=2.0, env=system_environment())
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        processes = [
            process
            for line in proc.stdout.decode("utf-8", "ignore").splitlines()
            if (process := parse_process_line(line, include_cpu_time=include_cpu_time)) is not None
        ]
        if processes:
            break
    if strict and not processes:
        raise OSError("process snapshot unavailable or empty")
    if include_gpu and processes and read_gpu is not None:
        processes = merge_process_gpu_pcts(processes, read_gpu())
    return processes


def sorted_processes(processes: list[ProcessInfo], sort_key: str) -> list[ProcessInfo]:
    if sort_key == "power":
        return sorted(processes, key=lambda item: (-1.0 if item.cpu_power_mw is None else item.cpu_power_mw, item.cpu_pct, item.mem_pct), reverse=True)
    if sort_key == "rss":
        return sorted(processes, key=lambda item: (item.rss_kib, item.mem_pct, item.cpu_pct), reverse=True)
    if sort_key == "ram":
        return sorted(processes, key=lambda item: (item.mem_pct, item.rss_kib, item.cpu_pct), reverse=True)
    if sort_key == "gpu":
        return sorted(processes, key=lambda item: (-1.0 if item.gpu_pct is None else item.gpu_pct, item.cpu_pct, item.mem_pct), reverse=True)
    if sort_key == "pid":
        return sorted(processes, key=lambda item: item.pid)
    if sort_key == "name":
        return sorted(processes, key=lambda item: item.command.lower())
    return sorted(processes, key=lambda item: (item.cpu_pct, item.mem_pct, item.rss_kib), reverse=True)
