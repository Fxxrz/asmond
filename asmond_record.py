"""Explicit local recording, summaries and CSV export."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
import ctypes
from datetime import datetime, timedelta, timezone
import errno
from functools import cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time
import uuid

from asmond_models import SamplerRetryState, sampling_clock_gap
from asmond_models import sampling_gap_limit_s as recording_gap_limit_s
from asmond_process import read_processes


GROUPS = ("cpu", "power", "temperatures", "memory", "processes", "io", "battery")
COUNTERS = ("swapins", "swapouts", "compressions", "decompressions")
SCHEMA = 1
LOG_NAME = re.compile(r"records-\d{4}-\d{2}-\d{2}-(\d{6})\.jsonl")
MAX_LINE = 2 * 1024 * 1024


def groups_arg(text):
    selected = text.split(",")
    if not selected or any(item not in GROUPS for item in selected) or len(set(selected)) != len(selected):
        raise argparse.ArgumentTypeError("choose distinct comma-separated groups: " + ",".join(GROUPS))
    return tuple(selected)


def interval_arg(text):
    value = float(text)
    if not math.isfinite(value) or not 1 <= value <= 3600:
        raise argparse.ArgumentTypeError("record interval must be 1..3600 seconds")
    return value


def duration_arg(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("duration must be positive finite seconds")
    return value


def add_parsers(subparsers):
    record = subparsers.add_parser("record", help="explicit local headless monitoring; no automatic restart")
    record.add_argument("--mock", action="store_true", default=argparse.SUPPRESS)
    record.add_argument("-i", "--interval", type=interval_arg, default=10.0, help="record interval in seconds (1..3600; default 10)")
    record.add_argument("--groups", type=groups_arg, default=GROUPS, help="comma-separated measurement groups")
    record.add_argument("--output-dir", type=Path, default=Path.home() / "Library/Logs/Asmond", help="parent of a new private session folder")
    record.add_argument("--duration", type=duration_arg, help="optional recording duration in seconds; otherwise stop with Ctrl+C")
    record.add_argument("--max-size-mib", type=int, default=256, help="total JSONL limit in MiB; stop, never delete (default 256)")
    summary = subparsers.add_parser("summary", help="summarize an existing recording session")
    summary.add_argument("session", type=Path)
    export = subparsers.add_parser("export", help="export a recording session into a new CSV folder")
    export.add_argument("session", type=Path)
    export.add_argument("--output-dir", type=Path, required=True, help="new directory; existing output is never overwritten")


def finite(value):
    try:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    except OverflowError:
        return None


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entry(kind, **values):
    return dict(schema=SCHEMA, type=kind, timestamp=timestamp(), monotonic_s=time.monotonic(), **values)


class SizeLimit(Exception):
    pass


class OutputPrivacyError(ValueError):
    pass


# Darwin sys/acl.h; check the opened item so the check and write refer to the same file.
ACL_TYPE_EXTENDED = 0x100
ACL_FIRST_ENTRY, ACL_NEXT_ENTRY = 0, -1
ACL_EXTENDED_DENY = 2


@cache
def output_acl_api():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    pointer = ctypes.c_void_p
    for name, result, arguments in (
        ("acl_get_fd_np", pointer, (ctypes.c_int, ctypes.c_int)),
        ("acl_get_entry", ctypes.c_int, (pointer, ctypes.c_int, ctypes.POINTER(pointer))),
        ("acl_get_tag_type", ctypes.c_int, (pointer, ctypes.POINTER(ctypes.c_int))),
        ("acl_free", ctypes.c_int, (pointer,)),
    ):
        function = getattr(lib, name)
        function.restype = result
        function.argtypes = arguments
    return lib


def require_private_output(fd):
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OutputPrivacyError("Output must be owned by you with no group or other permissions.")
    if sys.platform != "darwin":
        return
    error = "Could not verify macOS output permissions. Choose a private output directory."
    try:
        lib = output_acl_api()
    except (AttributeError, OSError):
        raise OutputPrivacyError(error) from None
    acl = lib.acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:  # Darwin returns ENOENT for an item with no ACL.
            return
        raise OutputPrivacyError(error)
    try:
        entry, tag = ctypes.c_void_p(), ctypes.c_int()
        index = ACL_FIRST_ENTRY
        while lib.acl_get_entry(acl, index, ctypes.byref(entry)) == 0:
            index = ACL_NEXT_ENTRY
            if lib.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                raise OutputPrivacyError(error)
            if tag.value != ACL_EXTENDED_DENY:
                raise OutputPrivacyError("A macOS ACL grants access to the output. Choose a private output directory.")
        if ctypes.get_errno() != errno.EINVAL:  # End of a Darwin ACL.
            raise OutputPrivacyError(error)
    finally:
        lib.acl_free(acl)


def open_private_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        require_private_output(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_private_file(path, mode, **options):
    parent_fd = open_private_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        require_private_output(fd)
        return os.fdopen(fd, mode, **options)
    except BaseException:
        os.close(fd)
        raise


class DailyWriter:
    """Private exclusive files, bounded total bytes, flushed complete JSONL rows."""
    def __init__(self, parent, limit):
        parent = Path(parent).expanduser().absolute()
        if parent.is_symlink():
            raise ValueError("output directory must not be a symlink")
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("output directory must be owned by you and not group/world writable")
        self.directory = parent / ("asmond-record-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12])
        self.directory.mkdir(mode=0o700)
        os.close(open_private_directory(self.directory))
        self.limit, self.total = limit, 0
        self.handle, self.day, self.sequence = None, None, 0

    def write(self, record, *, final=False):
        raw = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_LINE or self.total + len(raw) > self.limit - (0 if final else 8192):
            raise SizeLimit("recording size limit reached")
        day = record["timestamp"][:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("invalid record timestamp")
        if self.handle is None or day != self.day:
            self.close()
            self.sequence += 1
            path = self.directory / f"records-{day}-{self.sequence:06d}.jsonl"
            self.handle = open_private_file(path, "wb")
            self.day = day
        self.handle.write(raw)
        self.handle.flush()
        self.total += len(raw)

    def close(self):
        if self.handle is not None:
            try:
                self.handle.close()
            finally:
                self.handle = None


class CounterRates:
    def __init__(self, interval):
        self.previous, self.interval = None, interval

    def read(self, memory):
        now, size = memory.vm_sampled_at, memory.vm_page_size_bytes
        counters = {key: memory.vm_pages.get(key) for key in COUNTERS}
        result = {"page_size_bytes": size, "sampled_at_monotonic_s": now}
        usable = memory.read_status.get("vm_stat") == "ok" and finite(now) is not None and isinstance(size, int) and size > 0
        before = self.previous
        elapsed = now - before[0] if usable and before is not None else None
        contiguous = elapsed is not None and 0 < elapsed <= recording_gap_limit_s(self.interval) and size == before[1]
        result["interval_s"] = elapsed if contiguous else None
        for key, value in counters.items():
            old = before[2].get(key) if before is not None else None
            valid = usable and isinstance(value, int) and not isinstance(value, bool) and value >= 0
            status = "unavailable" if not valid else "baseline" if before is None else "gap" if not contiguous else "baseline" if old is None else "reset" if value < old else "ok"
            delta = value - old if status == "ok" else None
            result[key] = {"pages_total": value if valid else None, "status": status,
                           "pages_delta": delta,
                           "pages_per_s": delta / elapsed if delta is not None else None,
                           "mib_equivalent_per_s": delta * size / elapsed / 1024**2 if delta is not None else None}
        self.previous = (now, size, {k: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None for k, v in counters.items()}) if usable else None
        return result


class ProcessRanks:
    def __init__(self, interval):
        self.previous, self.interval = None, interval

    def reset(self):
        self.previous = None

    def read(self, processes, now):
        before = self.previous
        elapsed = now - before[0] if before is not None else None
        contiguous = elapsed is not None and 0 < elapsed <= recording_gap_limit_s(self.interval)
        rows, current = [], {}
        for process in processes:
            cpu_time = finite(process.cpu_time_s)
            key = (process.pid, process.start_time)
            if process.pid <= 0 or not process.start_time or cpu_time is None or cpu_time < 0:
                continue
            current[key] = cpu_time
            old = before[1].get(key) if contiguous else None
            delta = cpu_time - old if old is not None and cpu_time >= old else None
            name = os.path.basename(process.command)
            name = "".join(char if char.isprintable() else "?" for char in name)[:256]
            rows.append({"pid": process.pid, "start_time": process.start_time, "name": name,
                         "cpu_pct": 100.0 * delta / elapsed if delta is not None else None,
                         "cpu_time_s": cpu_time, "cpu_status": "ok" if delta is not None else "baseline_or_reset",
                         "ram_pct": finite(process.mem_pct), "rss_bytes": process.rss_kib * 1024 if process.rss_kib >= 0 else None})
        self.previous = now, current
        return {"interval_s": elapsed if contiguous else None, "process_count": len(rows),
                "cpu_source": "delta of accumulated process CPU time / actual elapsed time; 100% per logical CPU",
                "memory_source": "ps RSS; shared pages can appear in multiple processes",
                "top_cpu": sorted((row for row in rows if row["cpu_pct"] is not None), key=lambda row: (-row["cpu_pct"], row["pid"]))[:10],
                "top_memory": sorted(rows, key=lambda row: (-(row["rss_bytes"] or 0), row["pid"]))[:10]}


class StreamFeed:
    """Reuse the existing parser/native stream with a bounded latest snapshot."""
    def __init__(self, backend, interval, mock, *, include_temperatures=True):
        self.backend, self.interval, self.mock = backend, interval, mock
        self.include_temperatures = include_temperatures
        self.stop_event, self.lock = threading.Event(), threading.Lock()
        self.retry_wake = threading.Event()
        self.stream, self.latest = None, None
        self.generation = 0
        self.started = time.monotonic()
        self.status = "starting"
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if not self.mock:
            self.thread.start()

    def _run(self):
        retry = SamplerRetryState()
        while not self.stop_event.is_set():
            with self.lock:
                generation = self.generation
            stream = None
            try:
                options = {} if self.include_temperatures else {"include_temperatures": False}
                stream = self.backend.PowerMetricsStream(int(self.interval * 1000), **options)
                with self.lock:
                    if generation != self.generation:
                        retry.reset()
                        continue
                    self.stream, self.started = stream, time.monotonic()
                if self.stop_event.is_set():
                    break
                for sample in stream.samples():
                    with self.lock:
                        if generation != self.generation or self.stop_event.is_set():
                            break
                        if self.backend.powermetrics_sample_is_usable(sample):
                            self.latest, self.status = (sample, time.monotonic()), "partial" if sample.warning else "ok"
                            retry.reset()
                        else:
                            self.latest, self.status = None, "unavailable"
                    if self.stop_event.is_set():
                        break
            except Exception:
                with self.lock:
                    if generation == self.generation:
                        self.latest, self.status = None, "unavailable"
            finally:
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                with self.lock:
                    if self.stream is stream:
                        self.stream = None
                    if generation == self.generation:
                        self.latest, self.status = None, "unavailable"
            with self.lock:
                invalidated = generation != self.generation
                if self.stop_event.is_set():
                    break
                if not invalidated:
                    self.retry_wake.clear()
            if invalidated:
                retry.reset()
                continue
            if self.retry_wake.wait(retry.schedule()):
                retry.reset()

    def snapshot(self):
        if self.mock:
            # Take one generated sample without sleeping in a mock stream's
            # generator. No real readers or lingering mock worker on shutdown.
            stream = self.backend.MockStream(int(self.interval * 1000))
            try:
                return next(stream.samples()), {"status": "ok", "received_age_s": 0.0, "received_at_monotonic_s": time.monotonic()}
            finally:
                stream.stop()
        now = time.monotonic()
        with self.lock:
            last, status, stream, started = self.latest, self.status, self.stream, self.started
        age = now - last[1] if last is not None else None
        stale = (age if age is not None else now - started) > max(5.0, 3 * self.interval)
        if stale and stream is not None:
            self.invalidate(status="stale", expected_stream=stream)
        return (copy.deepcopy(last[0]) if last is not None and not stale else None,
                {"status": "stale" if stale else status, "received_age_s": age,
                 "received_at_monotonic_s": last[1] if last else None})

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.retry_wake.set()
            stream = self.stream
        if stream is not None:
            stream.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def invalidate(self, *, status="clock_gap", expected_stream=None):
        with self.lock:
            if expected_stream is not None and self.stream is not expected_stream:
                return
            stream = self.stream
            self.generation += 1
            self.retry_wake.set()
            self.latest, self.status, self.started = None, status, time.monotonic()
        if stream is not None:
            stream.stop()


def numeric_fields(obj, names):
    return {name: finite(getattr(obj, name)) for name in names}


def memory_values(memory, rates):
    page_size = memory.vm_page_size_bytes
    def page_bytes(key):
        value = memory.vm_pages.get(key)
        return value * page_size if page_size is not None and value is not None else None
    total = memory.total_bytes if memory.read_status.get("total") == "ok" else None
    active, wired = page_bytes("pages active"), page_bytes("pages wired down")
    used = active + wired if active is not None and wired is not None else None
    pressure = 100.0 - memory.system_free_pct if memory.read_status.get("pressure") == "ok" and memory.system_free_pct is not None else None
    return {"total_bytes": total, "used_bytes": used, "used_pct": 100 * used / total if used is not None and total else None,
            "active_bytes": active, "wired_bytes": wired, "compressed_bytes": page_bytes("pages occupied by compressor"),
            "uncompressed_bytes_in_compressor": page_bytes("pages stored in compressor"),
            "swap_used_bytes": memory.swap_used_bytes if memory.read_status.get("swap") == "ok" else None,
            "swap_total_bytes": memory.swap_total_bytes if memory.read_status.get("swap") == "ok" else None,
            "pressure_pct": pressure, "pressure_source": "100 - memory_pressure reported system free %; not native pressure level",
            "used_source": "vm_stat active + wired; not Activity Monitor app memory",
            "read_status": dict(memory.read_status), "activity": rates.read(memory)}


class Capture:
    def __init__(self, backend, args, feed):
        self.backend, self.args, self.feed = backend, args, feed
        self.cpu_before, self.io_before = None, None
        self.cpu_at = None
        self.last_clock = None
        self.mock_process_at, self.mock_process_cpu_times = None, {}
        self.rates, self.ranks = CounterRates(args.interval), ProcessRanks(args.interval)

    def read(self):
        b, args = self.backend, self.args
        result = entry("sample", groups={}, sources={})
        groups, sources = result["groups"], result["sources"]
        clock = (time.time(), time.monotonic())
        result["observation_gap"] = None
        gap = sampling_clock_gap(self.last_clock, clock, args.interval) if self.last_clock is not None else None
        if gap is not None:
            wall_delta, monotonic_delta = gap
            result["observation_gap"] = {"wall_elapsed_s": wall_delta, "monotonic_elapsed_s": monotonic_delta,
                                         "source": "observed clock discontinuity or long sampling pause; cause not inferred"}
            self.cpu_before, self.io_before = None, None
            self.rates.previous = None
            self.ranks.reset()
            b.SSD_TEMPS.invalidate()
            if self.feed is not None:
                self.feed.invalidate()
        self.last_clock = clock
        sample, pm_source = self.feed.snapshot() if self.feed else (None, {"status": "not_requested"})
        def acquire(name, action):
            start = time.monotonic()
            try:
                groups[name] = action()
                sources[name] = {"status": "ok"}
            except Exception as exc:
                groups[name] = None
                sources[name] = {"status": "unavailable", "error": type(exc).__name__}
            sources[name].update(start_monotonic_s=start, end_monotonic_s=time.monotonic())

        if "power" in args.groups:
            groups["power"] = numeric_fields(sample, ("soc_power_mw", "cpu_power_mw", "gpu_power_mw", "ane_power_mw", "ram_power_mw", "media_power_mw")) if sample else None
            if groups["power"] is not None:
                groups["power"]["total_label"] = b.total_power_label(sample)
                groups["power"]["telemetry_source"] = sample.telemetry_source
            sources["power"] = dict(pm_source)
            if sample is not None and all(value is None for key, value in groups["power"].items() if key.endswith("_mw")):
                sources["power"]["status"] = "no_data"
        if "cpu" in args.groups:
            def cpu():
                current = b.read_cpu_load_snapshot() if not args.mock else None
                now = time.monotonic()
                cpu_sample = (sample if sample is not None else b.mock_metric_sample(now)) if args.mock else sample
                if self.cpu_at is not None and now - self.cpu_at > recording_gap_limit_s(args.interval):
                    self.cpu_before = None
                value = b.cpu_usage_from_snapshots(self.cpu_before, current) if not args.mock else (
                    b.current_cpu_usage(cpu_sample))
                self.cpu_before, self.cpu_at = current, now
                return {"host_usage_pct": value, "source": "generated mock" if args.mock else "Mach host CPU-time deltas; 0..100% host average",
                        "gpu_usage_pct": finite(cpu_sample.gpu_usage_pct) if cpu_sample else None,
                        "p_usage_pct": finite(cpu_sample.p_usage_pct) if cpu_sample else None,
                        "e_usage_pct": finite(cpu_sample.e_usage_pct) if cpu_sample else None,
                        "p_freq_mhz": finite(cpu_sample.p_freq_mhz) if cpu_sample else None,
                        "e_freq_mhz": finite(cpu_sample.e_freq_mhz) if cpu_sample else None,
                        "gpu_freq_mhz": finite(cpu_sample.gpu_freq_mhz) if cpu_sample else None,
                        "gpu_source": "generated mock" if args.mock else pm_source["status"],
                        "cores": {core.label: {"usage_pct": finite(core.usage_pct), "freq_mhz": finite(core.freq_mhz)} for core in cpu_sample.cores} if cpu_sample else {}}
            acquire("cpu", cpu)
            if groups["cpu"] is None:
                self.cpu_before = None
        if "temperatures" in args.groups:
            def temperatures():
                current = sample
                if current is None and args.mock:
                    current = b.MetricSample()
                elif current is None:
                    current = b.MetricSample()
                    b.apply_hid_temperatures(current)
                values = numeric_fields(current, ("cpu_temp_avg_c", "cpu_temp_max_c", "gpu_temp_avg_c", "gpu_temp_max_c", "gpu_die_temp_c", "cpu_peci_temp_c", "cpu_proximity_temp_c", "memory_proximity_temp_c", "pch_die_temp_c", "ssd_temp_c", "pmu_temp_avg_c", "pmu_temp_max_c", "airport_temp_c", "power_supply_temp_c", "trackpad_temp_c", "trackpad_actuator_temp_c", "temp_max_c", "fan_rpm"))
                if current.ssd_temp_c is not None:
                    values["ssd_source"] = "NVMe SMART; one explicitly internal device; cache up to 30s"
                values.update(source=current.temperature_source, hotspot_source=current.temp_max_source,
                              cpu_max_label=current.cpu_temp_max_label, thermal_pressure=current.thermal_pressure,
                              throttled=current.throttled, fan_rpms={key: finite(value) for key, value in current.fan_rpms.items()})
                return values
            acquire("temperatures", temperatures)
            sources["temperatures"]["powermetrics"] = pm_source
            if groups["temperatures"] is not None and not any(value is not None for value in groups["temperatures"].values() if not isinstance(value, dict)) and not groups["temperatures"]["fan_rpms"]:
                sources["temperatures"]["status"] = "unavailable"
        if "memory" in args.groups:
            def memory():
                stats = b.mock_memory_stats(time.monotonic()) if args.mock else b.read_memory_stats()
                if args.mock:
                    stats.vm_page_size_bytes, stats.vm_sampled_at = 4096, time.monotonic()
                    stats.vm_pages = {"pages active": stats.active_bytes // 4096, "pages wired down": stats.wired_bytes // 4096,
                                      "pages free": stats.free_bytes // 4096, "pages speculative": 0,
                                      "pages occupied by compressor": stats.compressed_bytes // 4096,
                                      "pages stored in compressor": stats.compressed_bytes // 2048, **dict.fromkeys(COUNTERS, 0)}
                    stats.read_status = dict.fromkeys(("total", "vm_stat", "pressure", "swap"), "ok")
                return memory_values(stats, self.rates)
            acquire("memory", memory)
            if groups["memory"] is None:
                self.rates.previous = None
            else:
                statuses = list(groups["memory"]["read_status"].values())
                sources["memory"]["status"] = "ok" if statuses and all(value == "ok" for value in statuses) else "partial" if "ok" in statuses else "unavailable"
                for name, status in groups["memory"]["read_status"].items():
                    sources[f"memory.{name}"] = {"status": status}
        if "processes" in args.groups:
            def processes():
                if args.mock:
                    now = time.monotonic()
                    processes = b.mock_processes(now)
                    elapsed = max(0.0, now - self.mock_process_at) if self.mock_process_at is not None else 0.0
                    accumulated = {}
                    for process in processes:
                        process.start_time = "generated-mock-start"
                        key = process.pid, process.start_time
                        # Integrate the demo rate; uptime * current rate is not
                        # an accumulated counter when the workload changes.
                        process.cpu_time_s = self.mock_process_cpu_times.get(key, 0.0) + elapsed * process.cpu_pct / 100
                        accumulated[key] = process.cpu_time_s
                    self.mock_process_at, self.mock_process_cpu_times = now, accumulated
                else:
                    processes = read_processes(include_cpu_time=True, strict=True)
                    now = time.monotonic()
                return self.ranks.read(processes, now)
            acquire("processes", processes)
            if groups["processes"] is None:
                self.ranks.reset()
        if "io" in args.groups:
            def io():
                if args.mock:
                    stats = b.mock_io_stats(time.monotonic())
                else:
                    current = b.read_io_snapshot()
                    stats = b.io_stats_from_snapshots(self.io_before, current, max_gap_s=recording_gap_limit_s(args.interval))
                    self.io_before = current
                return numeric_fields(stats, ("disk_read_bps", "disk_write_bps", "net_in_bps", "net_out_bps"))
            acquire("io", io)
            if groups["io"] is None:
                self.io_before = None
            elif not args.mock and self.io_before.failed_readers:
                failed = self.io_before.failed_readers
                sources["io"]["status"] = "unavailable" if len(failed) == 2 else "partial"
                sources["io"]["error"] = ", ".join(failed)
                if len(failed) == 2:
                    groups["io"] = None
            elif all(value is None for value in groups["io"].values()):
                sources["io"]["status"] = "baseline_or_reset"
        if "battery" in args.groups:
            def battery():
                stats, usb = (b.mock_battery_stats(time.monotonic()), b.mock_usb_c_stats(time.monotonic())) if args.mock else b.read_charge_stats(include_usb_details=False)
                value = numeric_fields(stats, ("power_mw", "temperature_c", "charge_pct", "health_pct", "cycle_count"))
                value.update(charging=stats.charging, external_connected=stats.external_connected,
                             measured_input_w=finite(usb.system_power_w) if usb.external_connected is not False else None,
                             input_source="AppleSmartBattery SystemPowerIn; no adapter-rating fallback")
                return value
            acquire("battery", battery)
            if groups["battery"] is not None and all(value is None for key, value in groups["battery"].items() if key != "input_source"):
                sources["battery"]["status"] = "no_data"
        result["completed_at_monotonic_s"] = time.monotonic()
        result["collection_seconds"] = result["completed_at_monotonic_s"] - result["monotonic_s"]
        return result


class Events:
    def __init__(self, thresholds):
        self.previous, self.thresholds = {}, thresholds

    def read(self, record):
        states = {"source." + key: source["status"] for key, source in record["sources"].items()}
        groups = record["groups"]
        temp, memory, battery = (groups.get(name) or {} for name in ("temperatures", "memory", "battery"))
        def threshold(key, value, limit, enabled, *, inclusive=True):
            if enabled:
                if value is None:
                    states[key] = "unavailable"
                else:
                    active = value >= limit if inclusive else value > limit
                    states[key] = "active" if active else "clear"
        threshold("temperature_high", temp.get("temp_max_c"), self.thresholds[0], "temperatures" in groups and self.thresholds[0] > 0)
        threshold("swap_used_high", memory.get("swap_used_bytes"), self.thresholds[1], "memory" in groups and self.thresholds[1] > 0)
        drain = battery.get("power_mw")
        threshold("battery_drain_high", -drain if drain is not None else None, self.thresholds[2], "battery" in groups, inclusive=False)
        if "temperatures" in groups:
            throttled = temp.get("throttled")
            states["throttled"] = "unavailable" if throttled is None else "active" if throttled else "clear"
        events = []
        for key, state in states.items():
            old = self.previous.get(key)
            if old != state:
                events.append(entry("event", event=key, state=state, previous_state=old,
                                    observed_sample_timestamp=record["timestamp"]))
        self.previous = states
        if record.get("observation_gap") is not None:
            events.append(entry("event", event="observation_gap", state="observed", details=record["observation_gap"], observed_sample_timestamp=record["timestamp"]))
        return events


def run_record(args, backend):
    if not 1 <= args.max_size_mib <= 1024 * 1024:
        backend.print_console("max-size-mib must be 1..1048576", file=backend.sys.stderr)
        return 2
    if not args.mock and (backend.platform.system() != "Darwin" or os.geteuid() == 0):
        backend.print_console("Run recording as a normal macOS user; only powermetrics may use sudo.", file=backend.sys.stderr)
        return 2
    writer, feed, keeper = None, None, None
    header_written = False
    handlers = {}
    stopped_by = None
    interruptible = False
    reason, code, count = "completed", 0, 0

    def request_stop(signum, _frame):
        nonlocal stopped_by
        if stopped_by is None:
            stopped_by = signum
        if interruptible:
            raise KeyboardInterrupt

    def interruptible_call(function, *parameters):
        nonlocal interruptible
        try:
            interruptible = True
            if stopped_by is not None:
                raise KeyboardInterrupt
            return function(*parameters)
        finally:
            interruptible = False

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, request_stop)
        writer = DailyWriter(args.output_dir, args.max_size_mib * 1024**2)
        thresholds = backend.alert_thresholds(args)
        writer.write(entry("session_start", version=backend.VERSION, mock=args.mock, groups=args.groups,
                           system={"source": "generated mock"} if args.mock else {"macos": backend.platform.mac_ver()[0], "machine": backend.HOST_MACHINE, "python": backend.platform.python_version()},
                           interval_s=args.interval, max_jsonl_bytes=writer.limit,
                           thresholds={"temperature_high_c": thresholds[0], "swap_used_high_bytes": thresholds[1], "battery_drain_high_mw": thresholds[2]},
                           notes={"units": "bytes, seconds, mW, degrees C, percent; field suffixes are authoritative",
                                  "swap_activity": "reported compressed-swap page equivalents, includes maintenance; not physical SSD throughput",
                                  "compression_activity": "uncompressed-page equivalents, not compressed byte volume",
                                  "processes": "two independent top-10 rankings; names/PID/start time, no argv/user/full paths",
                                  "timestamps": "UTC wall clock and monotonic; sequential acquisition; receive age; events mark observations, not hardware occurrence times",
                                  "privacy": "local sensitive recording, not an anonymized public support report"}))
        header_written = True
        backend.print_console(f"Recording to {writer.directory} (Ctrl+C stops; no automatic restart)")
        if "power" in args.groups and stopped_by is None:
            if not args.mock:
                interruptible_call(backend.ensure_powermetrics_access, args)
                if stopped_by is None:
                    keeper = backend.SudoKeeper()
                    keeper.start()
            if stopped_by is None:
                feed = StreamFeed(backend, args.interval, args.mock, include_temperatures="temperatures" in args.groups)
                feed.start()
        capture = Capture(backend, args, feed)
        events = Events(thresholds)
        start = time.monotonic()
        while stopped_by is None:
            begin = time.monotonic()
            if args.duration is not None and begin - start >= args.duration:
                break
            record = capture.read()
            writer.write(record)
            count += 1
            for event in events.read(record):
                writer.write(event)
            remaining = args.interval - (time.monotonic() - begin)
            if args.duration is not None:
                remaining = min(remaining, args.duration - (time.monotonic() - start))
            interruptible_call(time.sleep, max(0, remaining))
    except KeyboardInterrupt:
        if stopped_by is None:
            stopped_by = signal.SIGINT
    except SizeLimit:
        reason, code = "size_limit", 0
    except SystemExit:
        reason, code = "authorization_failed", 1
    except (OSError, ValueError) as exc:
        reason, code = "write_or_configuration_error", 1
        detail = str(exc) if isinstance(exc, OutputPrivacyError) else type(exc).__name__
        backend.print_console(f"Recording stopped: {detail}", file=backend.sys.stderr)
    except Exception as exc:
        reason, code = "recording_error", 1
        backend.print_console(f"Recording stopped: {type(exc).__name__}", file=backend.sys.stderr)
    finally:
        try:
            for resource in (feed, keeper):
                if resource is not None:
                    try:
                        resource.stop()
                    except Exception:
                        reason, code = "cleanup_error", 1
            if stopped_by is not None and reason in {"completed", "authorization_failed"}:
                reason, code = ("interrupted", 130) if stopped_by == signal.SIGINT else ("terminated", 0)
            if writer is not None:
                try:
                    if header_written:
                        writer.write(entry("session_end", reason=reason, samples=count, jsonl_bytes_before_end=writer.total), final=True)
                except (OSError, ValueError, SizeLimit):
                    code = 1
                finally:
                    try:
                        writer.close()
                    except OSError:
                        code = 1
                backend.print_console(f"Recording stopped: {reason}; {count} samples; {writer.directory}")
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
    return code


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def flatten(value, prefix=""):
    if not isinstance(value, dict):
        if not isinstance(value, (list, tuple)):
            yield prefix, value
        return
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        yield from flatten(item, name)


def validate_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
        raise ValueError("invalid recording timestamp")
    datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_record(record):
    def tree(value, depth=0):
        if depth > 16:
            raise ValueError("recording nesting limit exceeded")
        if isinstance(value, dict):
            if len(value) > 2048:
                raise ValueError("too many recording fields")
            for key, item in value.items():
                if len(key) > 256:
                    raise ValueError("recording key too long")
                tree(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 256:
                raise ValueError("recording list too long")
            for item in value:
                tree(item, depth + 1)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and finite(value) is None:
            raise ValueError("nonfinite recording number")

    tree(record)
    if not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != SCHEMA or record.get("type") not in {"session_start", "sample", "event", "session_end"}:
        raise ValueError("unsupported recording schema")
    validate_timestamp(record.get("timestamp"))
    if finite(record.get("monotonic_s")) is None:
        raise ValueError("invalid recording clock")
    if record["type"] != "sample":
        if record["type"] == "session_start":
            if not isinstance(record.get("version"), str) or not isinstance(record.get("groups"), list) or any(group not in GROUPS for group in record["groups"]):
                raise ValueError("invalid recording header")
            if finite(record.get("interval_s")) is None or type(record.get("max_jsonl_bytes")) is not int:
                raise ValueError("invalid recording limits")
        if record["type"] == "session_end" and not isinstance(record.get("reason"), str):
            raise ValueError("invalid recording end")
        if record["type"] == "event":
            if not isinstance(record.get("event"), str) or not isinstance(record.get("state"), str):
                raise ValueError("invalid recording event")
            if record.get("observed_sample_timestamp") is not None:
                validate_timestamp(record["observed_sample_timestamp"])
        return
    groups = record.get("groups")
    if not isinstance(groups, dict) or any(key not in GROUPS or value is not None and not isinstance(value, dict) for key, value in groups.items()):
        raise ValueError("malformed recording groups")
    if not isinstance(record.get("sources", {}), dict):
        raise ValueError("invalid recording sources")
    strings = {"source", "ssd_source", "pressure_source", "used_source", "cpu_source", "gpu_source", "input_source", "memory_source", "total_label", "telemetry_source", "hotspot_source", "cpu_max_label", "thermal_pressure", "status", "error", "start_time", "name", "cpu_status", "state"}
    maps = {("cpu", "cores"), ("temperatures", "fan_rpms"), ("memory", "activity"), ("memory", "read_status")}

    def metrics(value, path):
        for key, item in value.items():
            location = (*path, key)
            expected_map = location in maps or path == ("cpu", "cores") or path == ("memory", "activity") and key in COUNTERS
            if expected_map:
                if not isinstance(item, dict):
                    raise ValueError("recording object required")
                metrics(item, location)
            elif isinstance(item, dict):
                raise ValueError("unexpected recording object")
            elif key in strings or path == ("memory", "read_status"):
                if item is not None and not isinstance(item, str):
                    raise ValueError("invalid recording text")
            elif key in {"throttled", "charging", "external_connected"}:
                if item is not None and type(item) is not bool:
                    raise ValueError("invalid recording state")
            elif item is not None and finite(item) is None:
                raise ValueError("invalid recording metric")

    for group, values in groups.items():
        if values is None:
            continue
        if group != "processes":
            metrics(values, (group,))
            continue
        metrics({key: value for key, value in values.items() if key not in {"top_cpu", "top_memory"}}, (group,))
        for ranking in ("top_cpu", "top_memory"):
            processes = values.get(ranking, [])
            if not isinstance(processes, list) or len(processes) > 10:
                raise ValueError("invalid process ranking")
            for process in processes:
                if not isinstance(process, dict) or type(process.get("pid")) is not int or process["pid"] <= 0 or not isinstance(process.get("name"), str):
                    raise ValueError("invalid ranked process")
                if set(process) - {"pid", "start_time", "name", "cpu_pct", "cpu_time_s", "cpu_status", "ram_pct", "rss_bytes"}:
                    raise ValueError("unexpected process field")
                metrics(process, (group, ranking))
    for key in ("collection_seconds",):
        if record.get(key) is not None and finite(record[key]) is None:
            raise ValueError("invalid collection duration")
    def source_values(value):
        if not isinstance(value, dict) or not isinstance(value.get("status"), str):
            raise ValueError("source status object required")
        for key, item in value.items():
            if key == "powermetrics":
                source_values(item)
            elif key in {"status", "error"}:
                if item is not None and not isinstance(item, str):
                    raise ValueError("invalid source text")
            elif item is not None and finite(item) is None:
                raise ValueError("invalid source metadata")
    for value in record.get("sources", {}).values():
        source_values(value)
    gap = record.get("observation_gap")
    if gap is not None:
        if not isinstance(gap, dict):
            raise ValueError("invalid observation gap")
        metrics(gap, ("observation_gap",))


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key in recording")
        result[key] = value
    return result


class RecordingChanged(ValueError):
    """The input was modified while it was being read."""


def read_records(session, *, fingerprint=None):
    if session.is_symlink() or not session.is_dir():
        raise ValueError("session must be a recording directory, not a symlink")
    paths = sorted((path for path in session.iterdir() if LOG_NAME.fullmatch(path.name)),
                   key=lambda path: int(LOG_NAME.fullmatch(path.name).group(1)))
    if not paths:
        raise ValueError("no recording files found")
    for path in paths:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("recording input must be a regular file")
            if fingerprint is not None:
                fingerprint.update(path.name.encode() + b"\0")
            while raw := handle.readline(MAX_LINE + 1):
                if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                    raise ValueError("oversized or incomplete JSONL row; input left unchanged")
                def invalid_constant(_value):
                    raise ValueError("nonfinite JSON number")
                record = json.loads(raw, parse_constant=invalid_constant, object_pairs_hook=unique_json_object)
                validate_record(record)
                if fingerprint is not None:
                    fingerprint.update(raw)
                yield record
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RecordingChanged("recording changed while reading")


def sample_csv_row(record):
    fields = {key: value for key, value in record["groups"].items() if key != "processes"}
    fields.update(sources=record.get("sources", {}), observation_gap=record.get("observation_gap"),
                  timestamp=record["timestamp"], monotonic_s=record["monotonic_s"],
                  collection_seconds=record.get("collection_seconds"))
    row = {}
    for key, value in flatten(fields):
        if key in row:
            raise ValueError("colliding CSV column paths in recording")
        row[key] = value
    return row


def summary_lines(records):
    header = end = last = previous_sample = longest_spacing = None
    samples = gaps = temperature_samples = 0
    non_ok_sources = Counter()
    peaks = {}
    for record in records:
        kind = record["type"]
        last = record
        if header is None:
            if kind != "session_start" or not 1 <= record["interval_s"] <= 3600:
                raise ValueError("expected a recording header with a valid sampling interval")
            if len(set(record["groups"])) != len(record["groups"]):
                raise ValueError("duplicate measurement group in recording header")
            header = record
            continue
        if kind == "session_start" or end is not None:
            raise ValueError("expected a single recording session")
        if kind == "session_end":
            end = record
        if kind != "sample":
            continue

        samples += 1
        now = record["monotonic_s"]
        gap = record.get("observation_gap") is not None
        if previous_sample is not None:
            spacing = finite(now - previous_sample)
            if spacing is None or spacing <= 0:
                gap = True
            else:
                if longest_spacing is None or spacing > longest_spacing[0]:
                    longest_spacing = spacing, record["timestamp"]
                gap |= spacing > recording_gap_limit_s(header["interval_s"])
        previous_sample = now
        gaps += gap

        groups, sources = record["groups"], record.get("sources", {})
        for name in header["groups"]:
            if sources.get(name, {}).get("status") != "ok":
                non_ok_sources[name] += 1
        temperatures = groups.get("temperatures") or {}
        temperature_samples += any(key.endswith("_c") and finite(value) is not None
                                   for key, value in temperatures.items())
        memory = groups.get("memory") or {}
        activity = memory.get("activity") or {}
        swap_in, swap_out = (activity.get(name, {}) for name in ("swapins", "swapouts"))
        for label, value, scale, unit in (
            ("Peak host CPU", (groups.get("cpu") or {}).get("host_usage_pct"), 1, "%"),
            ("Peak memory pressure", memory.get("pressure_pct"), 1, "%"),
            ("Peak swap occupied", memory.get("swap_used_bytes"), 1024**3, "GiB"),
            ("Peak swap-in", swap_in.get("mib_equivalent_per_s") if swap_in.get("status") == "ok" else None, 1, "MiB-equivalent/s"),
            ("Peak swap-out", swap_out.get("mib_equivalent_per_s") if swap_out.get("status") == "ok" else None, 1, "MiB-equivalent/s"),
        ):
            if finite(value) is not None and (label not in peaks or value > peaks[label][0]):
                peaks[label] = value, scale, unit, record

    if header is None:
        raise ValueError("session contains no records")
    if end is not None and "samples" in end and (type(end["samples"]) is not int or end["samples"] != samples):
        raise ValueError("end marker sample count does not match the session")
    elapsed = finite(last["monotonic_s"] - header["monotonic_s"])
    duration = str(timedelta(seconds=round(elapsed))) if elapsed is not None and elapsed >= 0 else "n/a"
    lines = [
        "Asmond recording summary" + (" (generated mock)" if header.get("mock") is True else ""),
        f"Started (UTC): {header['timestamp']}",
        f"{'Ended' if end is not None else 'Last record'} (UTC): {last['timestamp']}",
        f"Elapsed (monotonic): {duration}",
        f"Stop reason: {end['reason'] if end is not None else 'unknown (no end marker)'}",
        f"Samples: {samples:,} (target interval {header['interval_s']:g} s)",
        f"Sampling gaps / clock changes: {gaps:,}",
    ]
    if longest_spacing is not None:
        seconds, stamp = longest_spacing
        lines.append(f"Longest sample spacing: {seconds:.3f} s at {stamp}")
    if samples and "temperatures" in header["groups"]:
        lines.append(f"Samples with numeric temperatures: {temperature_samples:,}/{samples:,}")
    if non_ok_sources:
        statuses = ", ".join(f"{name} {count:,}/{samples:,}" for name, count in sorted(non_ok_sources.items()))
        lines.append(f"Source samples with status other than ok: {statuses}")
    for label, (value, scale, unit, record) in peaks.items():
        lines.append(f"{label}: {value / scale:.2f} {unit} at {record['timestamp']}")
        if label not in {"Peak swap-in", "Peak swap-out"} or value <= 0:
            continue
        processes = record["groups"].get("processes") or {}
        for rank, field, name, divisor, suffix in (
            ("top_cpu", "cpu_pct", "CPU leader", 1, "%"),
            ("top_memory", "rss_bytes", "RSS leader", 1024**2, "MiB"),
        ):
            leader = next((process for process in processes.get(rank, []) if finite(process.get(field)) is not None), None)
            if leader is not None:
                lines.append(f"  Same sample {name}: {leader['name'][:80]} (PID {leader['pid']}), {leader[field] / divisor:.2f} {suffix}")
    return lines


def run_summary(args, backend):
    try:
        lines = summary_lines(read_records(args.session.expanduser().absolute()))
    except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        backend.print_console(f"Recording summary failed: {exc}", file=backend.sys.stderr)
        return 1
    for line in lines:
        backend.print_console(line)
    return 0


def private_csv(path):
    return open_private_file(path, "w", newline="", encoding="utf-8")


def csv_export_error_reason(exc):
    if isinstance(exc, OutputPrivacyError):
        return str(exc)
    if isinstance(exc, RecordingChanged):
        return "the recording changed during export. Stop the recorder before retrying."
    if isinstance(exc, json.JSONDecodeError):
        return "the recording contains invalid JSON."
    if isinstance(exc, OSError):
        return os.strerror(exc.errno) if exc.errno is not None else "could not read the recording or write the CSV files."
    return "the recording is incomplete, malformed or uses an unsupported schema."


def run_export(args, backend):
    session, output = args.session.expanduser().absolute(), args.output_dir.expanduser().absolute()
    output_created = False
    try:
        columns, found = {"timestamp", "monotonic_s"}, False
        first_digest, second_digest = hashlib.sha256(), hashlib.sha256()
        for record in read_records(session, fingerprint=first_digest):
            if record["type"] == "sample":
                found = True
                columns.update(sample_csv_row(record))
                if len(columns) > 2048:
                    raise ValueError("too many CSV columns")
        if not found:
            raise ValueError("session contains no samples")
        try:
            output.mkdir(mode=0o700)
        except FileExistsError:
            backend.print_console(f"CSV export failed: output path already exists: {output}. Choose a new --output-dir.",
                                  file=backend.sys.stderr)
            return 1
        output_created = True
        with private_csv(output / "metrics.csv") as metrics, private_csv(output / "processes.csv") as processes, private_csv(output / "events.csv") as events:
            fields = ["timestamp", "monotonic_s", *sorted(columns - {"timestamp", "monotonic_s"})]
            metric_writer = csv.DictWriter(metrics, fieldnames=fields)
            process_fields = ["timestamp", "ranking", "rank", "pid", "start_time", "name", "cpu_pct", "cpu_time_s", "cpu_status", "ram_pct", "rss_bytes"]
            process_writer = csv.DictWriter(processes, fieldnames=process_fields)
            event_writer = csv.DictWriter(events, fieldnames=["timestamp", "event", "state", "previous_state", "observed_sample_timestamp"])
            for writer in (metric_writer, process_writer, event_writer):
                writer.writerow({field: csv_value(field) for field in writer.fieldnames})
            for record in read_records(session, fingerprint=second_digest):
                if record["type"] == "sample":
                    row = sample_csv_row(record)
                    metric_writer.writerow({key: csv_value(value) for key, value in row.items()})
                    for ranking in ("top_cpu", "top_memory"):
                        for rank, process in enumerate((record["groups"].get("processes") or {}).get(ranking, [])[:10], 1):
                            row = {key: process.get(key) for key in process_fields if key not in {"timestamp", "ranking", "rank"}}
                            row.update(timestamp=record["timestamp"], ranking=ranking, rank=rank)
                            process_writer.writerow({key: csv_value(value) for key, value in row.items()})
                elif record["type"] == "event":
                    event_writer.writerow({key: csv_value(record.get(key)) for key in event_writer.fieldnames})
                elif record["type"] in {"session_start", "session_end"}:
                    event_writer.writerow({"timestamp": csv_value(record.get("timestamp")), "event": record["type"], "state": csv_value(record.get("reason", "started"))})
        if first_digest.digest() != second_digest.digest():
            raise RecordingChanged("recording changed between export passes")
        backend.print_console(f"CSV export: {output}")
        return 0
    except KeyboardInterrupt:
        message, status = "CSV export interrupted.", 130
    except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        message, status = f"CSV export failed: {csv_export_error_reason(exc)}", 1
    backend.print_console(message, file=backend.sys.stderr)
    if output_created:
        backend.print_console(f"Incomplete CSV files may remain in {output}. Choose a new --output-dir to retry.",
                              file=backend.sys.stderr)
    return status
