from __future__ import annotations

import plistlib
import re
import subprocess
from typing import Any, Callable

from asmond_models import IoSnapshot, IoStats, MemoryStats


RunCommand = Callable[..., subprocess.CompletedProcess]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def bytes_from_pages(pages: int | float, page_size: int) -> int:
    return max(0, int(pages) * page_size)


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


def parse_sysctl_size(text: str) -> int:
    match = re.search(r"(-?[\d.,]+)\s*([KMGT]?)(?:i?B|B)?", text.strip(), re.IGNORECASE)
    if not match:
        return 0
    amount = parse_number_text(match.group(1))
    if amount is None:
        return 0
    unit = match.group(2).upper()
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
    return max(0, int(amount * factor))


def parse_swapusage(text: str) -> tuple[int, int, int]:
    values = {"total": 0, "used": 0, "free": 0}
    for name, raw_value in re.findall(r"\b(total|used|free)\s*=\s*([^\s)]+)", text, flags=re.IGNORECASE):
        values[name.lower()] = parse_sysctl_size(raw_value)
    return values["total"], values["used"], values["free"]


def read_swap_stats(stats: MemoryStats, run: RunCommand = subprocess.run) -> None:
    try:
        proc = run(["sysctl", "-n", "vm.swapusage"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        return
    if proc.returncode != 0:
        return
    text = proc.stdout.decode("utf-8", "ignore")
    stats.swap_total_bytes, stats.swap_used_bytes, stats.swap_free_bytes = parse_swapusage(text)


def read_memory_stats(run: RunCommand = subprocess.run) -> MemoryStats:
    stats = MemoryStats()
    try:
        total_proc = run(["sysctl", "-n", "hw.memsize"], check=False, capture_output=True, timeout=1.0)
        if total_proc.returncode == 0:
            stats.total_bytes = int(total_proc.stdout.decode("utf-8", "ignore").strip())
    except Exception:
        pass
    try:
        proc = run(["vm_stat"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        read_swap_stats(stats, run=run)
        return stats
    if proc.returncode != 0:
        read_swap_stats(stats, run=run)
        return stats
    text = proc.stdout.decode("utf-8", "ignore")
    page_size = 4096
    page_match = re.search(r"page size of (\d+) bytes", text)
    if page_match:
        page_size = int(page_match.group(1))
    pages: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+([\d,.]+)", line.strip())
        if not match:
            continue
        key = match.group(1).strip().lower()
        pages[key] = int(match.group(2).replace(",", "").rstrip("."))
    free_pages = pages.get("pages free", 0) + pages.get("pages speculative", 0)
    active_pages = pages.get("pages active", 0)
    purgeable_pages = pages.get("pages purgeable", 0)
    wired_pages = pages.get("pages wired down", 0)
    compressed_pages = pages.get("pages occupied by compressor", 0)
    file_backed_pages = pages.get("file-backed pages", 0)
    cached_pages = file_backed_pages + purgeable_pages

    stats.free_bytes = bytes_from_pages(free_pages, page_size)
    stats.cached_bytes = bytes_from_pages(cached_pages, page_size)
    stats.available_bytes = stats.free_bytes + stats.cached_bytes
    stats.active_bytes = bytes_from_pages(active_pages, page_size)
    stats.wired_bytes = bytes_from_pages(wired_pages, page_size)
    stats.compressed_bytes = bytes_from_pages(compressed_pages, page_size)

    if stats.total_bytes > 0:
        stats.available_bytes = min(stats.total_bytes, stats.available_bytes)
    try:
        pressure_proc = run(["memory_pressure"], check=False, capture_output=True, timeout=1.0)
        if pressure_proc.returncode == 0:
            pressure_text = pressure_proc.stdout.decode("utf-8", "ignore")
            match = re.search(r"System-wide memory free percentage:\s+(\d+(?:\.\d+)?)%", pressure_text)
            if match:
                stats.system_free_pct = clamp(float(match.group(1)), 0.0, 100.0)
    except Exception:
        pass
    read_swap_stats(stats, run=run)
    return stats


def read_disk_total_bytes_from_iostat(run: RunCommand = subprocess.run) -> int:
    try:
        proc = run(["iostat", "-Id", "-c", "1"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        return 0
    if proc.returncode != 0:
        return 0
    total_mb = 0.0
    for line in proc.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            total_mb += float(parts[2])
        except ValueError:
            continue
    return int(total_mb * 1024 * 1024)


def dict_number(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def read_disk_counters(run: RunCommand = subprocess.run) -> tuple[int, int]:
    try:
        proc = run(
            ["ioreg", "-r", "-c", "IOBlockStorageDriver", "-k", "Statistics", "-a"],
            check=False,
            capture_output=True,
            timeout=1.0,
        )
    except Exception:
        total = read_disk_total_bytes_from_iostat(run=run)
        return total, 0
    if proc.returncode != 0 or not proc.stdout:
        total = read_disk_total_bytes_from_iostat(run=run)
        return total, 0
    try:
        items = plistlib.loads(proc.stdout)
    except Exception:
        total = read_disk_total_bytes_from_iostat(run=run)
        return total, 0

    read_bytes = 0
    write_bytes = 0

    def walk(obj: Any) -> None:
        nonlocal read_bytes, write_bytes
        if isinstance(obj, dict):
            if obj.get("IOObjectClass") == "IOBlockStorageDriver":
                stats = obj.get("Statistics")
                if isinstance(stats, dict):
                    read_bytes += int(dict_number(stats, "Bytes (Read)") or 0)
                    write_bytes += int(dict_number(stats, "Bytes (Write)") or 0)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(items)
    if read_bytes == 0 and write_bytes == 0:
        total = read_disk_total_bytes_from_iostat(run=run)
        return total, 0
    return read_bytes, write_bytes


def read_network_bytes(run: RunCommand = subprocess.run) -> tuple[int, int]:
    try:
        proc = run(["netstat", "-ibn"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        return 0, 0
    if proc.returncode != 0:
        return 0, 0
    seen: set[str] = set()
    in_bytes = 0
    out_bytes = 0
    ignored_prefixes = ("lo", "gif", "stf", "awdl", "llw", "utun")
    for line in proc.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 11 or not parts[2].startswith("<Link#"):
            continue
        name = parts[0].rstrip("*")
        if name in seen or name.startswith(ignored_prefixes):
            continue
        seen.add(name)
        try:
            in_bytes += int(parts[6])
            out_bytes += int(parts[9])
        except ValueError:
            continue
    return in_bytes, out_bytes


def read_io_snapshot(run: RunCommand = subprocess.run) -> IoSnapshot:
    net_in, net_out = read_network_bytes(run=run)
    disk_read, disk_write = read_disk_counters(run=run)
    return IoSnapshot(disk_read_bytes=disk_read, disk_write_bytes=disk_write, net_in_bytes=net_in, net_out_bytes=net_out)


def io_stats_from_snapshots(previous: IoSnapshot | None, current: IoSnapshot) -> IoStats:
    if previous is None:
        return IoStats()
    elapsed = max(0.001, current.timestamp - previous.timestamp)
    return IoStats(
        disk_read_bps=max(0, current.disk_read_bytes - previous.disk_read_bytes) / elapsed,
        disk_write_bps=max(0, current.disk_write_bytes - previous.disk_write_bytes) / elapsed,
        net_in_bps=max(0, current.net_in_bytes - previous.net_in_bytes) / elapsed,
        net_out_bps=max(0, current.net_out_bytes - previous.net_out_bytes) / elapsed,
    )
