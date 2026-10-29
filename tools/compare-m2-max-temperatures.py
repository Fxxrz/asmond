#!/usr/bin/env python3
"""Time-aligned, read-only M2 Max temperature comparison. Python stdlib only.

Run from the prepared test folder, not with sudo. No workload, fan control,
screenshots, screen recording or settings changes are started by this script.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import json
import math
import os
import platform
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path


PHASES = (
    ("idle", "Keep the Mac idle"),
    ("cpu", "Start your CPU-heavy task; leave GPU-heavy tasks stopped"),
    ("cooldown_cpu", "Stop the CPU-heavy task and let the Mac cool"),
    ("gpu", "Start your GPU-heavy task"),
    ("cooldown_gpu", "Stop the GPU-heavy task and let the Mac cool"),
)
SENSOR_KEY = re.compile(r"T[\x20-\x7e]{3}|FNum|F[0-9A-F](?:Ac|Mn|Mx|Tg)")
HID_NAME = re.compile(r"(?:PMU [A-Za-z0-9 ._+-]+|NAND CH\d+ temp|gas gauge battery|(?:pACC|eACC|GPU|SOC|ANE) MTR Temp Sensor[A-Za-z0-9 ._+-]*)")


def stamp():
    return {"unix_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns()}


def sysctl(name):
    proc = subprocess.run(
        ["/usr/sbin/sysctl", "-n", name], capture_output=True, timeout=3,
        check=False, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
    )
    if proc.returncode:
        raise RuntimeError("required hardware identity unavailable")
    return proc.stdout.decode("ascii", "strict").strip()


def checked_identity():
    if sys.version_info < (3, 10) or platform.system() != "Darwin" or platform.machine() != "arm64" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("use native arm64 Python 3.10+ on the M2 Max Mac")
    if os.geteuid() == 0:
        raise RuntimeError("run as the normal user, not with sudo")
    model, brand = sysctl("hw.model"), sysctl("machdep.cpu.brand_string")
    p, e = int(sysctl("hw.perflevel0.logicalcpu")), int(sysctl("hw.perflevel1.logicalcpu"))
    if (model, brand, p, e) != ("Mac14,5", "Apple M2 Max", 8, 4):
        raise RuntimeError("this comparison is for Mac14,5 / M2 Max / 8P+4E only")
    return {"model": model, "brand": brand, "performance_cores": p, "efficiency_cores": e,
            "macos": platform.mac_ver()[0], "build": sysctl("kern.osversion"),
            "python": platform.python_version()}


def decode(kind, raw):
    if kind == "flt " and len(raw) == 4:
        value = float(struct.unpack("<f", raw)[0])
    elif kind == "sp78" and len(raw) == 2:
        value = int.from_bytes(raw, "big", signed=True) / 256.0
    elif kind == "fpe2" and len(raw) == 2:
        value = int.from_bytes(raw, "big") / 4.0
    elif (kind, len(raw)) in {("ui8 ", 1), ("ui16", 2), ("ui32", 4)}:
        value = float(int.from_bytes(raw, "big"))
    else:
        return None
    return value if math.isfinite(value) else None


class RawSMC:
    """Only SMC operations 5/8/9; never substitute an old reading."""

    def __init__(self, module, reader):
        self.module, self.reader = module, reader
        self.info = {}

    def read(self, key):
        before = stamp()
        result = {"start": before}
        try:
            if key != "#KEY" and SENSOR_KEY.fullmatch(key) is None:
                raise ValueError("not a sensor key")
            info = self.info.get(key)
            if info is None:
                request = self.module._SMCKeyData()
                request.key = int.from_bytes(key.encode("ascii"), "big")
                request.data8 = 9
                code, reply = self.reader._call(request)
                if code != 0 or reply.result != 0 or not 0 < reply.key_info.data_size <= 32:
                    raise OSError("key info unavailable")
                info = (int(reply.key_info.data_size), int(reply.key_info.data_type))
            size, kind_code = info
            request = self.module._SMCKeyData()
            request.key = int.from_bytes(key.encode("ascii"), "big")
            request.key_info.data_size = size
            request.data8 = 5
            code, reply = self.reader._call(request)
            if code != 0 or reply.result != 0:
                raise OSError("sensor read failed")
            self.info[key] = info
            kind = kind_code.to_bytes(4, "big").decode("ascii", "replace")
            raw = bytes(reply.bytes[:size])
            value = decode(kind, raw)
            result.update(type=kind, size=size, value=value,
                          status="ok" if value is not None else "undecoded_or_nonfinite")
            # Raw numeric sensor bytes only. ioft is retained without asserting
            # its interpretation or hardware assignment; no arbitrary strings.
            if (kind, size) in {("flt ", 4), ("sp78", 2), ("fpe2", 2), ("ui8 ", 1), ("ui16", 2), ("ui32", 4), ("ioft", 8)}:
                result["raw_hex"] = raw.hex()
        except Exception as exc:
            self.info.pop(key, None)
            result.update(status="failed", error=type(exc).__name__)
        result["end"] = stamp()
        return result

    def discover(self):
        count = self.read("#KEY")
        if count.get("type") != "ui32" or count.get("value") is None or not 0 < count["value"] <= 8192:
            raise RuntimeError("missing or excessive SMC key count")
        keys, failures = set(), 0
        for index in range(int(count["value"])):
            request = self.module._SMCKeyData()
            request.data8, request.data32 = 8, index
            code, reply = self.reader._call(request)
            if code != 0 or reply.result != 0:
                failures += 1
                continue
            key = int(reply.key).to_bytes(4, "big").decode("ascii", "replace")
            if SENSOR_KEY.fullmatch(key):
                keys.add(key)
        if not any(key.startswith("T") for key in keys):
            raise RuntimeError("no SMC temperature keys discovered")
        return sorted(keys), {"reported_key_count": int(count["value"]), "index_read_failures": failures}


def hid_readings(app):
    hid, services = app.HID_TEMPS, None
    result = {"start": stamp(), "values": []}
    try:
        if not hid.available:
            raise RuntimeError("IOHID unavailable")
        services = hid.iokit.IOHIDEventSystemClientCopyServices(hid.client)
        if not services:
            raise RuntimeError("IOHID services unavailable")
        for index in range(min(int(hid.cf.CFArrayGetCount(services)), 2048)):
            service = hid.cf.CFArrayGetValueAtIndex(services, index)
            if not service:
                continue
            name = hid._service_name(service, hid.product_key)
            safe_name = name if isinstance(name, str) and len(name) <= 80 and HID_NAME.fullmatch(name) else "unclassified sensor"
            before = stamp()
            event = hid.iokit.IOHIDServiceClientCopyEvent(service, app.IOHID_TEMP_TYPE, 0, 0)
            value = None
            if event:
                try:
                    raw = float(hid.iokit.IOHIDEventGetFloatValue(event, app.IOHID_TEMP_FIELD))
                    value = raw if math.isfinite(raw) else None
                finally:
                    hid.cf.CFRelease(event)
            result["values"].append({"index": index, "sensor": safe_name, "value": value,
                                     "status": "ok" if value is not None else "unavailable",
                                     "start": before, "end": stamp()})
    except Exception as exc:
        result["error"] = type(exc).__name__
    finally:
        if services:
            hid.cf.CFRelease(services)
    result["end"] = stamp()
    return result


def record(handle, raw, keys, app, cpu, phase_seconds, interval, events):
    previous = None
    count = 0
    for phase, instruction in PHASES:
        begin = stamp()
        events.append({"phase": phase, "start": begin, "instruction": instruction})
        print(f"\n{phase.upper()}: {instruction}", flush=True)
        deadline = time.monotonic() + phase_seconds
        while time.monotonic() < deadline:
            frame = {"phase": phase, "start": stamp(), "smc": {}}
            for key in keys:
                frame["smc"][key] = raw.read(key)
            frame["hid"] = hid_readings(app)
            try:
                current = cpu.read_cpu_load_snapshot()
                frame["host_cpu_usage_pct"] = cpu.cpu_usage_from_snapshots(previous, current)
                previous = current
            except Exception:
                frame["host_cpu_usage_pct"] = None
                previous = None
            frame["end"] = stamp()
            handle.write(json.dumps(frame, allow_nan=False, separators=(",", ":")) + "\n")
            handle.flush()
            count += 1
            remaining = max(0, math.ceil(deadline - time.monotonic()))
            clock = datetime.now().astimezone().isoformat(timespec="milliseconds")
            print(f"\r{clock} | {phase} | {remaining:3d}s left | samples {count}", end="", flush=True)
            elapsed = (frame["end"]["monotonic_ns"] - frame["start"]["monotonic_ns"]) / 1e9
            time.sleep(max(0, min(interval - elapsed, deadline - time.monotonic())))
        events[-1]["end"] = stamp()
    print()
    return count


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def package(directory, output):
    files = sorted(directory.iterdir())
    manifest = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files)
    (directory / "manifest.sha256").write_text(manifest, encoding="ascii")
    target = output / (directory.name + ".tgz")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w:gz") as archive:
        for path in sorted(directory.iterdir()):
            info = tarfile.TarInfo(f"{directory.name}/{path.name}")
            info.size, info.mode = path.stat().st_size, 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as source:
                archive.addfile(info, source)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    sidecar = target.with_suffix(".tgz.sha256")
    fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"{digest}  {target.name}\n")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "Desktop")
    parser.add_argument("--phase-seconds", type=int, default=90, help="seconds per phase, 30..300 (default 90)")
    parser.add_argument("--interval", type=float, default=1.0, help="minimum frame interval, 0.5..5 seconds")
    args = parser.parse_args()
    if not 30 <= args.phase_seconds <= 300 or not math.isfinite(args.interval) or not 0.5 <= args.interval <= 5:
        parser.error("phase-seconds must be 30..300; interval must be 0.5..5")
    source, output = args.source_dir.expanduser().resolve(), args.output_dir.expanduser().resolve()
    if not all((source / name).is_file() for name in ("asmond.py", "asmond_smc.py", "asmond_cpu.py")):
        parser.error("source-dir must contain the extracted Asmond test modules")
    if not output.is_dir():
        parser.error("output-dir must already exist")
    try:
        identity = checked_identity()
    except Exception as exc:
        parser.error(str(exc))
    print("M2 Max comparison: 5 phases, no automatic workload, no sudo.")
    print("Keep Macs Fan Control visible. Record MFC + this terminal with macOS screen recording.")
    print("Phases: idle -> CPU task -> stop/cool -> GPU task -> stop/cool.")
    print(f"Each phase lasts {args.phase_seconds}s. Stop the test with Ctrl+C if needed.")
    try:
        input("Press Enter when your recording is running and the Mac is idle: ")
    except (EOFError, KeyboardInterrupt):
        return 130
    sys.path.insert(0, str(source))
    smc, app, cpu = (importlib.import_module(name) for name in ("asmond_smc", "asmond", "asmond_cpu"))
    reader = smc.AppleSMCTemperatureReader()
    if not reader.available:
        reader.close()
        parser.error("read-only SMC access unavailable; no measurement made")
    metadata = {"collector_version": 1, "identity": identity, "asmond_version": app.VERSION,
                "start": stamp(), "phase_seconds": args.phase_seconds, "interval_seconds": args.interval,
                "note": "Raw keys; no CPU/GPU/core assignment. Phases describe requested activity, not verified isolation. Each read has its own timestamps; reads are not simultaneous. No screenshots or process data collected."}
    events = []
    status, exit_code = "complete", 0
    prefix = "asmond-m2-temperature-" + datetime.now().strftime("%Y%m%d-%H%M%S-")
    with tempfile.TemporaryDirectory(prefix=prefix, dir=output) as temporary:
        directory = Path(temporary)
        try:
            raw = RawSMC(smc, reader)
            keys, discovery = raw.discover()
            metadata.update(discovery=discovery, keys=keys)
            with (directory / "samples.jsonl").open("w", encoding="utf-8") as handle:
                record(handle, raw, keys, app, cpu, args.phase_seconds, args.interval, events)
        except KeyboardInterrupt:
            status, exit_code = "interrupted", 130
        except Exception as exc:
            status, exit_code = "failed", 1
            metadata["error"] = type(exc).__name__
        finally:
            reader.close()
        metadata.update(status=status, end=stamp())
        write_json(directory / "metadata.json", metadata)
        write_json(directory / "events.json", events)
        target = package(directory, output)
    print(f"\nCapture {status}: {target}\nChecksum: {target}.sha256")
    print("Send these two files plus the corresponding MFC/terminal screen recording privately.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
