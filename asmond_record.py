"""Explicit local recording and CSV export; no UI, daemon or third-party code."""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time
import uuid

from asmond_models import SamplerRetryState
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
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self.handle = os.fdopen(fd, "wb")
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
        contiguous = elapsed is not None and 0 < elapsed <= max(5.0, self.interval * 2.5) and size == before[1]
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
        contiguous = elapsed is not None and 0 < elapsed <= max(5.0, self.interval * 2.5)
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
        self.stream, self.latest = None, None
        self.started = time.monotonic()
        self.status = "starting"
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if not self.mock:
            self.thread.start()

    def _run(self):
        retry = SamplerRetryState()
        while not self.stop_event.is_set():
            stream = None
            try:
                options = {} if self.include_temperatures else {"include_temperatures": False}
                stream = self.backend.PowerMetricsStream(int(self.interval * 1000), **options)
                with self.lock:
                    self.stream, self.started = stream, time.monotonic()
                if self.stop_event.is_set():
                    break
                for sample in stream.samples():
                    with self.lock:
                        if self.backend.powermetrics_sample_is_usable(sample):
                            self.latest, self.status = (sample, time.monotonic()), "partial" if sample.warning else "ok"
                            retry.reset()
                        else:
                            self.latest, self.status = None, "unavailable"
                    if self.stop_event.is_set():
                        break
            except Exception:
                with self.lock:
                    self.latest, self.status = None, "unavailable"
            finally:
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
            with self.lock:
                self.latest, self.stream = None, None
                self.status = "unavailable"
            self.stop_event.wait(retry.schedule())

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
            stream.stop()
        return (copy.deepcopy(last[0]) if last is not None and not stale else None,
                {"status": "stale" if stale else status, "received_age_s": age,
                 "received_at_monotonic_s": last[1] if last else None})

    def stop(self):
        self.stop_event.set()
        with self.lock:
            stream = self.stream
        if stream is not None:
            stream.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def invalidate(self):
        with self.lock:
            stream = self.stream
            self.latest, self.status, self.started = None, "clock_gap", time.monotonic()
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
        self.rates, self.ranks = CounterRates(args.interval), ProcessRanks(args.interval)

    def read(self):
        b, args = self.backend, self.args
        result = entry("sample", groups={}, sources={})
        groups, sources = result["groups"], result["sources"]
        clock = (time.time(), time.monotonic())
        result["observation_gap"] = None
        if self.last_clock is not None:
            wall_delta, monotonic_delta = (current - previous for current, previous in zip(clock, self.last_clock))
            if monotonic_delta <= 0 or monotonic_delta > max(5, args.interval * 2.5) or abs(wall_delta - monotonic_delta) > max(2, args.interval * 0.5):
                result["observation_gap"] = {"wall_elapsed_s": wall_delta, "monotonic_elapsed_s": monotonic_delta,
                                             "source": "observed clock discontinuity or long sampling pause; cause not inferred"}
                self.cpu_before, self.io_before = None, None
                self.rates.previous = None
                self.ranks.reset()
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
            groups["power"] = numeric_fields(sample, ("soc_power_mw", "cpu_power_mw", "gpu_power_mw", "ane_power_mw", "media_power_mw")) if sample else None
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
                if self.cpu_at is not None and now - self.cpu_at > max(5, args.interval * 2.5):
                    self.cpu_before = None
                value = b.cpu_usage_from_snapshots(self.cpu_before, current) if not args.mock else 25.0
                self.cpu_before, self.cpu_at = current, now
                return {"host_usage_pct": value, "source": "generated mock" if args.mock else "Mach host CPU-time deltas; 0..100% host average",
                        "gpu_usage_pct": finite(sample.gpu_usage_pct) if sample else None,
                        "p_usage_pct": finite(sample.p_usage_pct) if sample else None,
                        "e_usage_pct": finite(sample.e_usage_pct) if sample else None,
                        "p_freq_mhz": finite(sample.p_freq_mhz) if sample else None,
                        "e_freq_mhz": finite(sample.e_freq_mhz) if sample else None,
                        "gpu_freq_mhz": finite(sample.gpu_freq_mhz) if sample else None,
                        "gpu_source": pm_source["status"],
                        "cores": {core.label: {"usage_pct": finite(core.usage_pct), "freq_mhz": finite(core.freq_mhz)} for core in sample.cores} if sample else {}}
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
                values = numeric_fields(current, ("cpu_temp_avg_c", "cpu_temp_max_c", "gpu_temp_avg_c", "gpu_temp_max_c", "pmu_temp_avg_c", "pmu_temp_max_c", "airport_temp_c", "power_supply_temp_c", "trackpad_temp_c", "trackpad_actuator_temp_c", "temp_max_c", "fan_rpm"))
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
                processes = b.mock_processes(time.monotonic()) if args.mock else read_processes(include_cpu_time=True, strict=True)
                if args.mock:
                    for process in processes:
                        process.start_time, process.cpu_time_s = "generated-mock-start", time.monotonic() * process.cpu_pct / 100
                return self.ranks.read(processes, time.monotonic())
            acquire("processes", processes)
            if groups["processes"] is None:
                self.ranks.reset()
        if "io" in args.groups:
            def io():
                if args.mock:
                    stats = b.mock_io_stats(time.monotonic())
                else:
                    current = b.read_io_snapshot()
                    previous = self.io_before
                    elapsed = current.timestamp - previous.timestamp if previous is not None else None
                    contiguous = elapsed is not None and 0 < elapsed <= max(5, args.interval * 2.5)
                    stats = b.io_stats_from_snapshots(previous if contiguous else None, current)
                    if contiguous:
                        for field, counter in (("disk_read_bps", "disk_read_bytes"), ("disk_write_bps", "disk_write_bytes"), ("net_in_bps", "net_in_bytes"), ("net_out_bps", "net_out_bytes")):
                            if getattr(current, counter) < getattr(previous, counter):
                                setattr(stats, field, None)
                    self.io_before = current
                return numeric_fields(stats, ("disk_read_bps", "disk_write_bps", "net_in_bps", "net_out_bps"))
            acquire("io", io)
            if groups["io"] is None:
                self.io_before = None
            elif all(value is None for value in groups["io"].values()):
                sources["io"]["status"] = "baseline_or_reset"
        if "battery" in args.groups:
            def battery():
                stats, usb = (b.mock_battery_stats(time.monotonic()), b.mock_usb_c_stats(time.monotonic())) if args.mock else b.read_charge_stats()
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
        def threshold(key, value, limit, enabled):
            if enabled and limit > 0:
                states[key] = "unavailable" if value is None else "active" if value >= limit else "clear"
        threshold("temperature_high", temp.get("temp_max_c"), self.thresholds[0], "temperatures" in groups)
        threshold("swap_used_high", memory.get("swap_used_bytes"), self.thresholds[1], "memory" in groups)
        drain = battery.get("power_mw")
        threshold("battery_drain_high", -drain if drain is not None else None, self.thresholds[2], "battery" in groups)
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
    stop = threading.Event()
    old_term = None
    reason, code, count = "completed", 0, 0
    try:
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
        backend.print_console(f"Recording to {writer.directory} (Ctrl+C stops; no automatic restart)")
        if "power" in args.groups:
            if not args.mock:
                backend.ensure_powermetrics_access(args)
                keeper = backend.SudoKeeper()
                keeper.start()
            feed = StreamFeed(backend, args.interval, args.mock, include_temperatures="temperatures" in args.groups)
            feed.start()
        capture = Capture(backend, args, feed)
        events = Events(thresholds)
        if threading.current_thread() is threading.main_thread():
            old_term = signal.signal(signal.SIGTERM, lambda *_: stop.set())
        start = time.monotonic()
        while not stop.is_set():
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
            stop.wait(max(0, remaining))
        if stop.is_set():
            reason = "terminated"
    except KeyboardInterrupt:
        reason, code = "interrupted", 130
    except SizeLimit:
        reason, code = "size_limit", 0
    except SystemExit:
        reason, code = "authorization_failed", 1
    except (OSError, ValueError) as exc:
        reason, code = "write_or_configuration_error", 1
        backend.print_console(f"Recording stopped: {type(exc).__name__}", file=backend.sys.stderr)
    except Exception as exc:
        reason, code = "recording_error", 1
        backend.print_console(f"Recording stopped: {type(exc).__name__}", file=backend.sys.stderr)
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        for resource in (feed, keeper):
            if resource is not None:
                try:
                    resource.stop()
                except Exception:
                    reason, code = "cleanup_error", 1
        if writer is not None:
            try:
                writer.write(entry("session_end", reason=reason, samples=count, jsonl_bytes_before_end=writer.total), final=True)
            except (OSError, ValueError, SizeLimit):
                code = 1
            finally:
                try:
                    writer.close()
                except OSError:
                    code = 1
            backend.print_console(f"Recording stopped: {reason}; {count} samples; {writer.directory}")
    return code


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def flatten(value, prefix=""):
    result = {}
    if not isinstance(value, dict):
        return {prefix: value}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(flatten(item, name))
        elif not isinstance(item, (list, tuple)):
            result[name] = item
    return result


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
    strings = {"source", "pressure_source", "used_source", "cpu_source", "gpu_source", "input_source", "memory_source", "total_label", "telemetry_source", "hotspot_source", "cpu_max_label", "thermal_pressure", "status", "error", "start_time", "name", "cpu_status", "state"}
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
                record = json.loads(raw, parse_constant=invalid_constant)
                validate_record(record)
                if fingerprint is not None:
                    fingerprint.update(raw)
                yield record
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("recording changed while reading")


def sample_csv_row(record):
    row = flatten({key: value for key, value in record["groups"].items() if key != "processes"})
    row.update(flatten({"sources": record.get("sources", {})}))
    row.update(flatten({"observation_gap": record.get("observation_gap")}))
    row.update(timestamp=record["timestamp"], monotonic_s=record["monotonic_s"], collection_seconds=record.get("collection_seconds"))
    return row


def private_csv(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "w", newline="", encoding="utf-8")


def run_export(args, backend):
    session, output = args.session.expanduser().absolute(), args.output_dir.expanduser().absolute()
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
        output.mkdir(mode=0o700)
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
            raise ValueError("recording changed between export passes")
        backend.print_console(f"CSV export: {output}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        backend.print_console(f"CSV export failed: {type(exc).__name__}; no existing files overwritten", file=backend.sys.stderr)
        return 1
