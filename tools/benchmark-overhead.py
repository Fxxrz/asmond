#!/usr/bin/env python3
"""Measure a source checkout in a drained PTY; no third-party packages or runtime hooks."""
from __future__ import annotations

import argparse
import ctypes as c
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time


ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78])")


class RusageV0(c.Structure):
    # Complete RUSAGE_INFO_V0, not just the consumed fields (sys/resource.h).
    _fields_ = [("uuid", c.c_uint8 * 16)] + [(name, c.c_uint64) for name in (
        "user", "system", "idle_wakeups", "interrupt_wakeups", "pageins", "wired",
        "resident", "footprint", "started", "exited")]


class Timebase(c.Structure):
    _fields_ = [("numer", c.c_uint32), ("denom", c.c_uint32)]


@dataclass(frozen=True)
class Usage:
    at: float
    identity: tuple[int, bytes]
    cpu_s: float
    rss_bytes: int
    footprint_bytes: int


class UsageReader:
    def __init__(self):
        self.proc = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.proc.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
        self.proc.proc_pid_rusage.restype = c.c_int
        system = c.CDLL("/usr/lib/libSystem.B.dylib")
        system.mach_timebase_info.argtypes = [c.POINTER(Timebase)]
        system.mach_timebase_info.restype = c.c_int
        base = Timebase()
        if system.mach_timebase_info(c.byref(base)) != 0 or not base.numer or not base.denom:
            raise RuntimeError("Cannot read the Mach clock conversion.")
        self.seconds_per_tick = base.numer / base.denom / 1e9

    def read(self, pid: int) -> Usage:
        raw = RusageV0()
        if self.proc.proc_pid_rusage(pid, 0, c.byref(raw)) != 0:
            raise OSError(c.get_errno(), "Cannot read benchmark child resource counters")
        # XNU fill_task_rusage gets user/system time from task_power_info: Mach
        # ticks, unlike the explicitly named energy counter used by Asmond.
        return Usage(time.monotonic(), (raw.started, bytes(raw.uuid)),
                     (raw.user + raw.system) * self.seconds_per_tick,
                     raw.resident, raw.footprint)


def summarize(samples: list[Usage]) -> dict:
    if len(samples) < 2:
        raise ValueError("At least two resource observations are required.")
    first, last = samples[0], samples[-1]
    if (any(x.identity != first.identity for x in samples)
            or any(not math.isfinite(x.at) or not math.isfinite(x.cpu_s) for x in samples)
            or any(b.at <= a.at or b.cpu_s < a.cpu_s for a, b in zip(samples, samples[1:]))):
        raise ValueError("Resource counter identity, time or progression changed.")
    elapsed = last.at - first.at
    return {
        "observations": len(samples), "measured_s": elapsed,
        "cpu_s": last.cpu_s - first.cpu_s,
        "cpu_pct_one_logical_cpu": 100 * (last.cpu_s - first.cpu_s) / elapsed,
        "rss_median_bytes": statistics.median(x.rss_bytes for x in samples),
        "rss_sampled_max_bytes": max(x.rss_bytes for x in samples),
        "footprint_median_bytes": statistics.median(x.footprint_bytes for x in samples),
        "footprint_sampled_max_bytes": max(x.footprint_bytes for x in samples),
    }


class Terminal:
    def __init__(self, process: subprocess.Popen, fd: int):
        self.process, self.fd = process, fd
        self.tail = b""
        self.bytes = 0
        self.traceback = False
        self.sampler_problem = False
        os.set_blocking(fd, False)

    def pump(self, seconds: float, marker: bytes | None = None) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.fd], [], [], min(.05, max(0, deadline - time.monotonic())))[0]:
                try:
                    block = os.read(self.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        raise RuntimeError("Dashboard terminal closed.") from exc
                    raise
                if not block:
                    raise RuntimeError("Dashboard terminal closed.")
                self.bytes += len(block)
                self.tail = (self.tail + block)[-131072:]
                self.traceback |= b"Traceback (most recent call last)" in self.tail
                plain = ANSI.sub(b"", self.tail)
                self.sampler_problem |= any(text in plain for text in (
                    b"sampler stopped", b"sampler stale", b"sample stale", b"sudo:", b"Password:"))
                if marker and marker in plain:
                    return True
            if self.process.poll() is not None:
                break
        if self.process.poll() is not None:
            raise RuntimeError(f"Dashboard exited unexpectedly ({self.process.returncode}).")
        return False

    def key_response(self, key: bytes, marker: bytes) -> float:
        self.tail = b""
        began = time.monotonic()
        os.write(self.fd, key)
        if not self.pump(5, marker):
            raise RuntimeError("No expected menu output within five seconds.")
        return (time.monotonic() - began) * 1000

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    os.write(self.fd, b"q")
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                until = time.monotonic() + 5
                while self.process.poll() is None and time.monotonic() < until:
                    try:
                        self.pump(.05)
                    except RuntimeError:
                        break
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process.wait(timeout=5)
        finally:
            os.close(self.fd)


def interpreter_info(executable: str) -> tuple[str, dict]:
    path = shutil.which(executable)
    if not path:
        raise ValueError(f"Interpreter not found: {executable}")
    code = ("import ctypes,curses,json,platform,sys; "
            "print(json.dumps({'version':platform.python_version(),'machine':platform.machine(),"
            "'system':platform.system(),'supported':sys.version_info >= (3,10)}))")
    result = subprocess.run([path, "-I", "-c", code], capture_output=True, text=True, timeout=15, check=True)
    info = json.loads(result.stdout)
    if not info["supported"] or info["system"] != "Darwin" or info["machine"] != platform.machine():
        raise ValueError("Each interpreter must be native macOS Python 3.10+ with curses and ctypes.")
    return str(Path(path).absolute()), info


def power_source():
    result = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
    match = re.search(r"Now drawing from '(AC Power|Battery Power)'", result.stdout)
    return match.group(1) if result.returncode == 0 and match else "unavailable"


def run_case(args, reader, executable, info, layout, interval, repeat):
    power_start, load_start = power_source(), os.getloadavg()
    with tempfile.TemporaryDirectory(prefix="asmond-benchmark-") as directory:
        settings = Path(directory)
        (settings / "settings.json").write_text(json.dumps({
            "show_io": True, "process_panel": "right", "graph_history_minutes": 10,
            "rounded_borders": True, "graph_hover": False,
        }))
        master, slave = os.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.columns, 0, 0))
            env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
            env.update(TERM="xterm-256color", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8",
                       ASMOND_SETTINGS_DIR=directory, PYTHONDONTWRITEBYTECODE="1")
            command = [executable, "-B", "-E", "-s", str(ROOT / "asmond.py"),
                       "--layout", layout, "--theme", "solar", "--interval", str(interval), "--show-io"]
            if not args.live:
                command.append("--mock")
            began = time.monotonic()
            # Preserve the controlling terminal for cached sudo in live mode.
            # The benchmark never runs the UI as root or starts its own sampler.
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT)
        finally:
            os.close(slave)
            if process is None:
                os.close(master)
        terminal = Terminal(process, master)
        try:
            if not terminal.pump(15, b"POWER"):
                raise RuntimeError("Dashboard startup marker did not arrive.")
            startup_ms = (time.monotonic() - began) * 1000
            if args.live and b"last sample" not in ANSI.sub(b"", terminal.tail):
                if not terminal.pump(30, b"last sample"):
                    raise RuntimeError("No live powermetrics sample arrived.")
            terminal.pump(args.warmup)
            samples = [reader.read(process.pid)]
            end = samples[0].at + args.seconds
            while time.monotonic() < end:
                terminal.pump(min(.5, max(0, end - time.monotonic())))
                samples.append(reader.read(process.pid))
            metrics = summarize(samples)
            menu_ms = []
            for _ in range(3):
                menu_ms.append(terminal.key_response(b"m", b"MENU"))
                os.write(master, b"\x1b")
                terminal.pump(.3)
            if terminal.traceback:
                raise RuntimeError("Dashboard produced a traceback.")
            if args.live and terminal.sampler_problem:
                raise RuntimeError("Live sampler authorization/recovery warning; discard this comparison.")
        finally:
            terminal.close()
        if process.returncode != 0:
            raise RuntimeError(f"Dashboard did not stop cleanly ({process.returncode}).")
        power_end = power_source()
        if power_start != power_end:
            raise RuntimeError("Power source changed between case endpoints; discard this comparison.")
        return {"python": info["version"], "layout": layout, "interval_s": interval, "repeat": repeat,
                "mode": "live" if args.live else "mock", "startup_marker_ms": startup_ms,
                "menu_marker_ms": menu_ms, "terminal_bytes": terminal.bytes,
                "power_source_start": power_start, "power_source_end": power_end,
                "load_average_start": load_start, "load_average_end": os.getloadavg(),
                "exit_code": process.returncode, **metrics}


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive finite number.")
    return number


def runtime_fingerprints():
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted([ROOT / "asmond.py", *ROOT.glob("asmond_*.py")])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", action="append", dest="pythons", help="repeat to compare installed interpreters")
    parser.add_argument("--layouts", nargs="+", choices=("full", "compact", "focus"), default=["full", "compact"])
    parser.add_argument("--intervals", nargs="+", type=positive, default=[.7, 2.0])
    parser.add_argument("--seconds", type=positive, default=30, help="measured seconds per case, after warmup")
    parser.add_argument("--warmup", type=positive, default=5)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--columns", type=int, default=170)
    parser.add_argument("--rows", type=int, default=48)
    parser.add_argument("--live", action="store_true", help="real readers; cached or interactive sudo needed for powermetrics")
    parser.add_argument("--output", type=Path, required=True, help="new JSON file; never overwrite an existing result")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or os.geteuid() == 0:
        parser.error("Run as a normal user on native macOS.")
    translated = subprocess.run(["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"], capture_output=True, text=True)
    if translated.stdout.strip() == "1":
        parser.error("Open a native terminal, not a Rosetta terminal.")
    if not 80 <= args.columns <= 400 or not 24 <= args.rows <= 150:
        parser.error("Geometry must be 80..400 columns and 24..150 rows.")
    if args.seconds > 3600 or args.warmup > 120 or any(not .1 <= x <= 10 for x in args.intervals):
        parser.error("Use seconds <= 3600, warmup <= 120 and intervals .1..10.")
    if args.output.exists() or args.output.is_symlink():
        parser.error("Output exists; choose a new result file.")
    if not args.output.parent.is_dir() or not os.access(args.output.parent, os.W_OK):
        parser.error("Choose an existing writable output directory.")
    model = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.model"], capture_output=True, text=True, check=True)
    info = {"schema": 1, "system": {"macos": platform.mac_ver()[0], "machine": platform.machine(),
                                    "model": model.stdout.strip(), "logical_cpus": os.cpu_count()},
            "geometry": {"columns": args.columns, "rows": args.rows, "term": "xterm-256color"},
            "warmup_s": args.warmup, "source_sha256": runtime_fingerprints(),
            "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "scope": "Asmond PID only, including its threads; excludes child tools, terminal emulator and benchmark observer. "
                     "CPU uses Mach timebase conversion; 100% is one logical CPU. Memory maxima are sampled. "
                     "Latencies end at observed PTY markers, not screen presentation. Mock mode uses generated telemetry.",
            "cases": []}
    def stop(signum, frame):
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, stop)
    try:
        interpreters = [interpreter_info(p) for p in (args.pythons or [sys.executable])]
        reader = UsageReader()
        if args.live:
            cached = subprocess.run(["/usr/bin/sudo", "-n", "-v"], capture_output=True)
            if cached.returncode:
                if not sys.stdin.isatty():
                    raise RuntimeError("Live mode needs sudo authorization; run this command in your terminal.")
                subprocess.run(["/usr/bin/sudo", "-v"], check=True)
        # Alternate interpreters within each scenario; do not run cases concurrently.
        for repeat, layout, interval in itertools.product(range(1, args.repeats + 1), args.layouts, args.intervals):
            order = interpreters if repeat % 2 else list(reversed(interpreters))
            for executable, interpreter in order:
                print(f"{interpreter['version']} {layout} {interval:g}s repeat {repeat}", flush=True)
                info["cases"].append(run_case(args, reader, executable, interpreter, layout, interval, repeat))
        if (runtime_fingerprints() != info["source_sha256"]
                or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != info["benchmark_sha256"]):
            raise RuntimeError("Source changed during measurement; discard this comparison.")
        info["status"] = "complete"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        info["status"] = "incomplete"
        info["error"] = str(exc) or type(exc).__name__
    finally:
        signal.signal(signal.SIGTERM, old_term)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"{info['status']}: {args.output}", flush=True)
    return 0 if info["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
