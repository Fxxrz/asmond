from __future__ import annotations

import ctypes as c
import math
import plistlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable

from asmond_models import IoSnapshot, IoStats, MemoryBandwidthStats, MemoryStats, sampling_clock_gap
from asmond_system import IOREG_PATH, MEMORY_PRESSURE_PATH, NETSTAT_PATH, SYSCTL_PATH, VM_STAT_PATH, system_environment


RunCommand = Callable[..., subprocess.CompletedProcess]

IOREPORT_BYTE_UNIT = (9 << 56) | ((3 + 127) << 40)  # Apple's kIOReportUnitBytes.
MEMORY_BANDWIDTH_SOURCE = "IOReport AMC Stats / Perf Counters: DCS RD, DCS WR (bytes / elapsed seconds)"


class _IOReportBandwidthReader:
    """Own a narrow read-only subscription to one unambiguous controller pair."""

    def __init__(self) -> None:
        self.refs: list[int] = []
        try:
            self.cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.io = c.CDLL("/usr/lib/libIOReport.dylib")
            self._bind_api()
            self._open()
        except BaseException:
            self.close()
            raise

    def _bind_api(self) -> None:
        pointer, index = c.c_void_p, c.c_long
        signatures = (
            (self.cf, "CFRelease", None, [pointer]),
            (self.cf, "CFStringCreateWithCString", pointer, [pointer, c.c_char_p, c.c_uint32]),
            (self.cf, "CFStringGetCString", c.c_bool, [pointer, pointer, index, c.c_uint32]),
            (self.cf, "CFDictionaryGetValue", pointer, [pointer, pointer]),
            (self.cf, "CFArrayGetCount", index, [pointer]),
            (self.cf, "CFArrayGetValueAtIndex", pointer, [pointer, index]),
            (self.cf, "CFArrayCreateMutable", pointer, [pointer, index, pointer]),
            (self.cf, "CFArrayAppendValue", None, [pointer, pointer]),
            (self.cf, "CFDictionaryCreateMutableCopy", pointer, [pointer, index, pointer]),
            (self.cf, "CFDictionarySetValue", None, [pointer, pointer, pointer]),
            (self.io, "IOReportCopyChannelsInGroup", pointer, [pointer, pointer, c.c_uint64, c.c_uint64, c.c_uint64]),
            (self.io, "IOReportCreateSubscription", pointer, [pointer, pointer, c.POINTER(pointer), c.c_uint64, pointer]),
            (self.io, "IOReportCreateSamples", pointer, [pointer, pointer, pointer]),
            (self.io, "IOReportSimpleGetIntegerValue", c.c_int64, [pointer, c.c_int]),
        )
        for library, name, result, arguments in signatures:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
        for name in ("Group", "SubGroup", "ChannelName"):
            function = getattr(self.io, "IOReportChannelGet" + name)
            function.restype, function.argtypes = pointer, [pointer]
        for name in ("DriverID", "ChannelID", "Unit"):
            function = getattr(self.io, "IOReportChannelGet" + name)
            function.restype, function.argtypes = c.c_uint64, [pointer]
        self.io.IOReportChannelGetFormat.restype = c.c_int
        self.io.IOReportChannelGetFormat.argtypes = [pointer]

    def _own(self, pointer: int) -> int:
        if not pointer:
            raise OSError("IOReport object unavailable")
        self.refs.append(pointer)
        return pointer

    def _string(self, text: bytes) -> int:
        return self._own(self.cf.CFStringCreateWithCString(None, text, 0x08000100))

    def _text(self, pointer: int) -> str:
        buffer = c.create_string_buffer(128)
        if not pointer or not self.cf.CFStringGetCString(pointer, buffer, len(buffer), 0x08000100):
            return ""
        return buffer.value.decode("utf-8", "replace")

    def _channels(self, dictionary: int) -> Iterable[int]:
        array = self.cf.CFDictionaryGetValue(dictionary, self.key)
        if not array:
            raise OSError("IOReport channels missing")
        count = self.cf.CFArrayGetCount(array)
        if not 0 < count <= 4096:
            raise OSError("IOReport channel count invalid")
        for index in range(count):
            item = self.cf.CFArrayGetValueAtIndex(array, index)
            if not item:
                raise OSError("IOReport channel missing")
            yield item

    def _pair(self, dictionary: int) -> dict[str, tuple[int, int, int]]:
        pair = {}
        for channel in self._channels(dictionary):
            name = self._text(self.io.IOReportChannelGetChannelName(channel))
            if name not in ("DCS RD", "DCS WR"):
                continue
            if (name in pair or self._text(self.io.IOReportChannelGetGroup(channel)) != "AMC Stats"
                    or self._text(self.io.IOReportChannelGetSubGroup(channel)) != "Perf Counters"
                    or self.io.IOReportChannelGetFormat(channel) != 1
                    or self.io.IOReportChannelGetUnit(channel) != IOREPORT_BYTE_UNIT):
                raise OSError("IOReport bandwidth identity or schema ambiguous")
            driver = self.io.IOReportChannelGetDriverID(channel)
            identity = self.io.IOReportChannelGetChannelID(channel)
            if not driver or not identity:
                raise OSError("IOReport bandwidth identity missing")
            pair[name] = (driver, identity, channel)
        if (set(pair) != {"DCS RD", "DCS WR"} or pair["DCS RD"][0] != pair["DCS WR"][0]
                or pair["DCS RD"][1] == pair["DCS WR"][1]):
            raise OSError("IOReport controller pair unavailable")
        return pair

    def _open(self) -> None:
        self.key = self._string(b"IOReportChannels")
        group, subgroup = self._string(b"AMC Stats"), self._string(b"Perf Counters")
        channels = self._own(self.io.IOReportCopyChannelsInGroup(group, subgroup, 0, 0, 0))
        pair = self._pair(channels)
        callbacks = c.addressof(c.c_byte.in_dll(self.cf, "kCFTypeArrayCallBacks"))
        chosen = self._own(self.cf.CFArrayCreateMutable(None, 2, callbacks))
        for _, _, channel in pair.values():
            self.cf.CFArrayAppendValue(chosen, channel)
        request = self._own(self.cf.CFDictionaryCreateMutableCopy(None, 0, channels))
        self.cf.CFDictionarySetValue(request, self.key, chosen)
        actual = c.c_void_p()
        subscription = self.io.IOReportCreateSubscription(None, request, c.byref(actual), 0, None)
        # Both returned objects are owned even on a partial subscription failure.
        if actual.value:
            self._own(actual.value)
        self.subscription = self._own(subscription)
        if not actual.value:
            raise OSError("IOReport subscription channels unavailable")
        self.channels = actual.value
        self._pair(self.channels)

    def read(self) -> tuple[tuple[int, ...], tuple[int, int]]:
        sample = self.io.IOReportCreateSamples(self.subscription, self.channels, None)
        if not sample:
            raise OSError("IOReport bandwidth sample unavailable")
        try:
            pair = self._pair(sample)
            identities, values = [], []
            for name in ("DCS RD", "DCS WR"):
                driver, identity, channel = pair[name]
                value = self.io.IOReportSimpleGetIntegerValue(channel, 0)
                if value < 0:  # Includes Apple's INT64_MIN unavailable sentinel.
                    raise OSError("IOReport bandwidth counter invalid")
                identities.extend((driver, identity))
                values.append(value)
            return tuple(identities), (values[0], values[1])
        finally:
            self.cf.CFRelease(sample)

    def close(self) -> None:
        while self.refs:
            self.cf.CFRelease(self.refs.pop())


class MemoryBandwidthSampler:
    """Worker-owned byte deltas; no waits, estimates or native work on import."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.platform == "darwin"
        self.reader: _IOReportBandwidthReader | None = None
        self.retry_at = 0.0
        self.reset()

    def reset(self) -> None:
        self.previous: tuple | None = None
        self.verified = (False, False)

    def close(self) -> None:
        self.reset()
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.close()

    def read(self, interval: float) -> MemoryBandwidthStats:
        result = MemoryBandwidthStats(sampled_at=time.monotonic())
        if not self.enabled or result.sampled_at < self.retry_at:
            return result
        try:
            if self.reader is None:
                self.reader = _IOReportBandwidthReader()
            identities, values = self.reader.read()
            clock = (time.time(), time.monotonic())
            result.sampled_at = clock[1]
            previous = self.previous
            self.previous = (identities, values, clock)
            if previous is None:
                return result
            old_identities, old_values, old_clock = previous
            if (old_identities != identities or sampling_clock_gap(old_clock, clock, interval)
                    or any(value < old for value, old in zip(values, old_values))):
                self.verified = (False, False)
                return result
            elapsed = clock[1] - old_clock[1]
            rates = tuple((value - old) / elapsed for value, old in zip(values, old_values))
            self.verified = tuple(valid or rate > 0 for valid, rate in zip(self.verified, rates))
            if all(self.verified) and all(math.isfinite(rate) for rate in rates):
                result.read_bps, result.write_bps = rates
                result.source = MEMORY_BANDWIDTH_SOURCE
            return result
        except (OSError, AttributeError, ValueError, OverflowError):
            self.close()
            self.retry_at = time.monotonic() + 60.0
            return result


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
    stats.read_status["swap"] = "unavailable"
    try:
        proc = run(
            [SYSCTL_PATH, "-n", "vm.swapusage"],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
    except Exception:
        return
    if proc.returncode != 0:
        return
    text = proc.stdout.decode("utf-8", "ignore")
    stats.swap_total_bytes, stats.swap_used_bytes, stats.swap_free_bytes = parse_swapusage(text)
    tokens = {key.lower(): value for key, value in re.findall(r"\b(total|used|free)\s*=\s*([^\s)]+)", text, flags=re.IGNORECASE)}
    if all(re.fullmatch(r"\d+(?:[.,]\d+)?[KMGT]?", tokens.get(key, ""), re.I) for key in ("total", "used", "free")):
        stats.read_status["swap"] = "ok"


def apply_vm_stat(stats: MemoryStats, text: str) -> None:
    stats.vm_sampled_at = time.monotonic()
    stats.read_status["vm_stat"] = "unavailable"
    stats.vm_page_size_bytes = None
    stats.vm_pages = {}
    stats.file_backed_bytes = stats.speculative_bytes = None
    page_match = re.search(r"page size of (\d+) bytes", text)
    if page_match is None:
        return
    page_size = int(page_match.group(1))
    if page_size <= 0:
        return
    stats.vm_page_size_bytes = page_size
    pages: dict[str, int] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([^:]+):\s*([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\.?", line.strip())
        if not match:
            continue
        key = match.group(1).strip().lower()
        try:
            pages[key] = int(match.group(2).replace(",", ""))
        except ValueError:
            continue
    stats.vm_pages = pages
    if pages and stats.vm_page_size_bytes is not None:
        stats.read_status["vm_stat"] = "ok"
    free_pages = pages.get("pages free", 0)
    active_pages = pages.get("pages active", 0)
    purgeable_pages = pages.get("pages purgeable", 0)
    wired_pages = pages.get("pages wired down", 0)
    compressed_pages = pages.get("pages occupied by compressor", 0)
    file_backed_pages = pages.get("file-backed pages", 0)
    cached_pages = file_backed_pages + purgeable_pages

    stats.free_bytes = bytes_from_pages(free_pages, page_size)
    stats.file_backed_bytes = bytes_from_pages(pages["file-backed pages"], page_size) if "file-backed pages" in pages else None
    stats.speculative_bytes = bytes_from_pages(pages["pages speculative"], page_size) if "pages speculative" in pages else None
    stats.cached_bytes = bytes_from_pages(cached_pages, page_size)
    # vm_stat's free excludes speculative pages; file-backed includes them.
    stats.available_bytes = bytes_from_pages(free_pages + cached_pages, page_size)
    stats.active_bytes = bytes_from_pages(active_pages, page_size)
    stats.wired_bytes = bytes_from_pages(wired_pages, page_size)
    stats.compressed_bytes = bytes_from_pages(compressed_pages, page_size)

    if stats.total_bytes > 0:
        stats.available_bytes = min(stats.total_bytes, stats.available_bytes)


def read_memory_pressure_level(run: RunCommand) -> int | None:
    try:
        proc = run(
            [SYSCTL_PATH, "-n", "kern.memorystatus_vm_pressure_level"],
            check=False, capture_output=True, timeout=1.0, env=system_environment(),
        )
        if proc.returncode != 0 or not isinstance(proc.stdout, bytes):
            return None
        level = int(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return level if level in (1, 2, 4) else None


def read_memory_stats(run: RunCommand = subprocess.run) -> MemoryStats:
    stats = MemoryStats()
    stats.read_status = dict.fromkeys(("total", "vm_stat", "pressure", "swap"), "unavailable")
    try:
        total_proc = run(
            [SYSCTL_PATH, "-n", "hw.memsize"],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
        if total_proc.returncode == 0:
            stats.total_bytes = int(total_proc.stdout.decode("utf-8", "ignore").strip())
            if stats.total_bytes > 0:
                stats.read_status["total"] = "ok"
    except Exception:
        pass
    try:
        proc = run([VM_STAT_PATH], check=False, capture_output=True, timeout=1.0, env=system_environment())
        if proc.returncode == 0:
            apply_vm_stat(stats, proc.stdout.decode("utf-8", "ignore"))
    except Exception:
        pass
    try:
        pressure_proc = run(
            [MEMORY_PRESSURE_PATH],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
        if pressure_proc.returncode == 0:
            pressure_text = pressure_proc.stdout.decode("utf-8", "ignore")
            match = re.search(r"System-wide memory free percentage:\s+(\d+(?:\.\d+)?)%", pressure_text)
            if match:
                reported = float(match.group(1))
                if 0 <= reported <= 100:
                    stats.system_free_pct = reported
                    stats.read_status["pressure"] = "ok"
    except Exception:
        pass
    stats.pressure_level = read_memory_pressure_level(run)
    read_swap_stats(stats, run=run)
    return stats


def dict_number(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def read_disk_entries(run: RunCommand) -> list[tuple[int | None, tuple[int, int]]]:
    try:
        proc = run(
            [IOREG_PATH, "-r", "-c", "IOBlockStorageDriver", "-k", "Statistics", "-a"],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
    except Exception as exc:
        raise RuntimeError("disk counters unavailable") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ioreg disk counters exited {proc.returncode}")
    try:
        items = plistlib.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError("invalid ioreg disk plist") from exc

    entries: list[tuple[int | None, tuple[int, int]]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("IOObjectClass") == "IOBlockStorageDriver":
                stats = obj.get("Statistics")
                if not isinstance(stats, dict):
                    raise RuntimeError("incomplete disk byte counters")
                values = [stats.get(key) for key in ("Bytes (Read)", "Bytes (Write)")]
                if any(type(value) is not int or not 0 <= value <= 2**64 - 1 for value in values):
                    raise RuntimeError("invalid disk byte counters")
                entry_id = obj.get("IORegistryEntryID")
                if type(entry_id) is not int or entry_id <= 0:
                    entry_id = None
                entries.append((entry_id, (values[0], values[1])))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(items)
    if not entries:
        raise RuntimeError("ioreg returned no disk byte counters")
    return entries


def counter_totals(counters: Iterable[tuple[int, int]]) -> tuple[int, int]:
    first = second = 0
    for incoming, outgoing in counters:
        first += incoming
        second += outgoing
    return first, second


def read_disk_counters(run: RunCommand = subprocess.run) -> tuple[int, int]:
    return counter_totals(counters for _, counters in read_disk_entries(run))


def read_network_entries(run: RunCommand) -> dict[tuple[str, str, str], tuple[int, int]]:
    try:
        proc = run(
            [NETSTAT_PATH, "-ibn"],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
    except Exception as exc:
        raise RuntimeError("network counters unavailable") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"netstat exited {proc.returncode}")
    seen: set[str] = set()
    counters: dict[tuple[str, str, str], tuple[int, int]] = {}
    ignored_prefixes = ("lo", "gif", "stf", "awdl", "llw", "utun")
    for line in proc.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3 or not parts[2].startswith("<Link#"):
            continue
        name = parts[0].rstrip("*")
        if name in seen or name.startswith(ignored_prefixes):
            continue
        if len(parts) < 10:
            raise RuntimeError("incomplete network byte counters")
        # Some link-layer interfaces have no Address. netstat leaves that
        # column blank, so whitespace splitting puts Ipkts at index 3 instead.
        address_columns = 0 if parts[3].isdigit() else 1
        if len(parts) < 10 + address_columns:
            raise RuntimeError("incomplete network byte counters")
        try:
            incoming = int(parts[5 + address_columns])
            outgoing = int(parts[8 + address_columns])
        except ValueError as exc:
            raise RuntimeError("invalid network byte counters") from exc
        if not (0 <= incoming <= 2**64 - 1 and 0 <= outgoing <= 2**64 - 1):
            raise RuntimeError("invalid network byte counters")
        seen.add(name)
        # Include link index and link address to distinguish replaced interfaces.
        address = parts[3] if address_columns else ""
        counters[name, parts[2], address] = (incoming, outgoing)
    if not counters:
        raise RuntimeError("netstat returned no physical interfaces")
    return counters


def read_network_bytes(run: RunCommand = subprocess.run) -> tuple[int, int]:
    return counter_totals(read_network_entries(run).values())


def read_io_snapshot(run: RunCommand = subprocess.run) -> IoSnapshot:
    failed = []
    try:
        network = read_network_entries(run)
        net_at = time.monotonic()
    except (OSError, RuntimeError):
        network, net_at = {}, None
        failed.append("network")
    try:
        entries = read_disk_entries(run)
        disk_at = time.monotonic()
    except (OSError, RuntimeError):
        entries, disk_at = [], None
        failed.append("disk")
    disks = {entry_id: pair for entry_id, pair in entries if entry_id is not None}
    if len(disks) != len(entries):
        disks = {}
    disk_read, disk_write = counter_totals(pair for _, pair in entries)
    net_in, net_out = counter_totals(network.values())
    return IoSnapshot(
        disk_read_bytes=disk_read, disk_write_bytes=disk_write, net_in_bytes=net_in, net_out_bytes=net_out,
        disk_counters=disks, net_counters=network, disk_sampled_at=disk_at, net_sampled_at=net_at,
        failed_readers=tuple(failed),
    )


def io_stats_from_snapshots(
    previous: IoSnapshot | None, current: IoSnapshot, *, max_gap_s: float = math.inf,
) -> IoStats:
    if previous is None:
        return IoStats()

    def rates(before, after, previous_totals, current_totals, previous_at, current_at):
        elapsed = (current_at if current_at is not None else current.timestamp) - (
            previous_at if previous_at is not None else previous.timestamp
        )
        if not math.isfinite(elapsed) or not 0 < elapsed <= max_gap_s:
            return None, None
        if before is None and after is None:
            before, after = {None: previous_totals}, {None: current_totals}
        if not before or not after or before.keys() != after.keys():
            return None, None
        first, second = zip(*(
            (after[key][0] - old[0], after[key][1] - old[1]) for key, old in before.items()
        ))
        return (
            sum(first) / elapsed if min(first) >= 0 else None,
            sum(second) / elapsed if min(second) >= 0 else None,
        )

    disk = rates(
        previous.disk_counters, current.disk_counters,
        (previous.disk_read_bytes, previous.disk_write_bytes), (current.disk_read_bytes, current.disk_write_bytes),
        previous.disk_sampled_at, current.disk_sampled_at,
    )
    network = rates(
        previous.net_counters, current.net_counters,
        (previous.net_in_bytes, previous.net_out_bytes), (current.net_in_bytes, current.net_out_bytes),
        previous.net_sampled_at, current.net_sampled_at,
    )
    return IoStats(*disk, *network)
