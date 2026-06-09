from __future__ import annotations

import os
import plistlib
import re
import subprocess
from typing import Any, Callable, Iterable

from asmond_models import ProcessGpuProbeState, ProcessInfo


PROCESS_GPU_SAMPLE_MS = 1000
RunCommand = Callable[..., subprocess.CompletedProcess]
RefreshSudo = Callable[[bool], tuple[bool, str]]
NeedsSudo = Callable[[], bool]


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
        return float(value)
    except ValueError:
        return None


def parse_value(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int | float):
        return float(value), None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "ignore")
        except Exception:
            return None, None
    if isinstance(value, str):
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z%/]+)?", value)
        if match:
            return float(match.group(1)), (match.group(2) or "").lower() or None
    return None, None


def parse_process_line(line: str) -> ProcessInfo | None:
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
    if cpu_pct is None or mem_pct is None or rss_kib is None:
        return None
    command = os.path.basename(command_text) or command_text
    return ProcessInfo(pid, cpu_pct, mem_pct, int(rss_kib), command, ppid, etime, command_text, user)


def process_gpu_command(sample_ms: int, output_format: str = "plist", needs_sudo: NeedsSudo | None = None) -> list[str]:
    command = [
        "powermetrics",
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
        return ["sudo", "-n", *command]
    return command


def iter_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
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
    if number is None:
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
            return {}
    try:
        text_proc = run(
            process_gpu_command(sample_ms, "text", needs_sudo=needs_sudo),
            check=False,
            capture_output=True,
            timeout=max(4.0, sample_ms / 1000.0 + 3.0),
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
        )
    except Exception:
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


def merge_process_gpu_pcts(processes: list[ProcessInfo], gpu_pcts: dict[int, float]) -> list[ProcessInfo]:
    if not gpu_pcts:
        return processes
    for process in processes:
        if process.pid in gpu_pcts:
            process.gpu_pct = gpu_pcts[process.pid]
    return processes


def read_processes(
    include_gpu: bool = False,
    run: RunCommand = subprocess.run,
    read_gpu: Callable[[], dict[int, float]] | None = None,
) -> list[ProcessInfo]:
    commands = (
        ["/bin/ps", "-axo", "pid=,user=,ppid=,etime=,pcpu=,pmem=,rss=,comm="],
        ["/bin/ps", "-axo", "pid=,user=,ppid=,etime=,pcpu=,pmem=,rss=,command="],
        ["ps", "-axo", "pid=,user=,ppid=,etime=,pcpu=,pmem=,rss=,comm="],
    )
    processes: list[ProcessInfo] = []
    for command in commands:
        try:
            proc = run(command, check=False, capture_output=True, timeout=2.0)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        processes = [
            process
            for line in proc.stdout.decode("utf-8", "ignore").splitlines()
            if (process := parse_process_line(line)) is not None
        ]
        if processes:
            break
    if include_gpu and processes and read_gpu is not None:
        processes = merge_process_gpu_pcts(processes, read_gpu())
    return processes


def sorted_processes(processes: list[ProcessInfo], sort_key: str) -> list[ProcessInfo]:
    if sort_key == "ram":
        return sorted(processes, key=lambda item: (item.mem_pct, item.rss_kib, item.cpu_pct), reverse=True)
    if sort_key == "gpu":
        return sorted(processes, key=lambda item: (-1.0 if item.gpu_pct is None else item.gpu_pct, item.cpu_pct, item.mem_pct), reverse=True)
    if sort_key == "pid":
        return sorted(processes, key=lambda item: item.pid)
    if sort_key == "name":
        return sorted(processes, key=lambda item: item.command.lower())
    return sorted(processes, key=lambda item: (item.cpu_pct, item.mem_pct, item.rss_kib), reverse=True)
