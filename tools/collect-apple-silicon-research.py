#!/usr/bin/env python3
"""Read-only, stdlib-only research capture for Homebrew or source Asmond.

Run with Homebrew's Python as a normal user. Only powermetrics is elevated.
Sensor discovery is diagnostic only: key names are NOT CPU/core assignments.
The historical filename is retained; use --intel for a native Intel Mac.
Use --source-dir /path/to/asmond for an existing Git checkout.
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
import plistlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ENV = {**os.environ, "LC_ALL": "C", "LANG": "C", "HOMEBREW_NO_AUTO_UPDATE": "1"}
PRIVATE_KEY = re.compile(
    r"serial|uuid|guid|udid|address|hostname|host_name|computer.?name|"
    r"user.?name|owner|boot.?args|boot.?volume|volume.?name|sales.?order", re.I,
)
PRIVATE_NAMES = {
    "name", "io_name", "ioname", "ioregistryentryname", "usb_product_name",
    "product_name", "product", "device_name", "iouserclientcreator", "comm",
}


def clean_text(text: str) -> str:
    text = re.sub(r"(?im)^.*Boot arguments:.*$", "Boot arguments: <redacted>", text)
    text = text.replace(str(Path.home()), "~")
    for value in {os.environ.get("USER", ""), socket.gethostname(), platform.node()}:
        if value:
            text = re.sub(r"(?<![\w.-])" + re.escape(value) + r"(?![\w.-])", "<redacted>", text)
    text = re.sub(r"/Users/[^/\s\"']+", "/Users/<user>", text)
    text = re.sub(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", "<mac>", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", "<uuid>", text, flags=re.I)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "<email>", text)
    return text


def clean(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[ -]", "_", str(key)).lower()
            if normalized == "ioregistryentryname" and isinstance(item, str) and re.fullmatch(r"(?:Port-)?USB-C@[0-9]{1,3}|MagSafe(?:@[0-9]{1,3})?", item):
                result[str(key)] = item
            elif PRIVATE_KEY.search(normalized) or normalized in PRIVATE_NAMES:
                result[str(key)] = "<redacted>"
            elif normalized == "vdos" and isinstance(item, list):
                # Identity VDOs are public 32-bit PD words, not arbitrary blobs.
                result[str(key)] = [int.from_bytes(word, "little") if isinstance(word, bytes) and len(word) == 4 else clean(word) for word in item]
            elif normalized in {"pid", "process_id", "command", "command_line", "executable", "path"}:
                result[str(key)] = "<redacted>"
            else:
                result[clean_text(str(key))] = clean(item)
        return result
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, bytes):
        # Do not export binary serials, descriptors or firmware buffers.
        return {"redacted_binary_bytes": len(value)}
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clean_text(str(value))


def run(command, timeout=25):
    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout, env=ENV, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, b"", b"command timed out; incomplete output discarded"
    except OSError as exc:
        return 127, b"", type(exc).__name__.encode()


def parse_plists(raw):
    return [plistlib.loads(chunk.strip()) for chunk in raw.split(b"\0") if chunk.strip()]


def network_rows(raw):
    """Keep original header/counter positions, but no IP/MAC/interface addresses."""
    lines = raw.decode("utf-8", "replace").splitlines()
    result = [lines[0]] if lines else []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 5 and parts[2].startswith("<Link#"):
            # Some down interfaces have no Address column; preserve that shape.
            if not parts[3].isdigit():
                parts[3] = "<address>"
            if all(re.fullmatch(r"\d+|[-*]", item) for item in parts[4:]):
                result.append(" ".join(parts))
    return "\n".join(result) + "\n"


def settings_visibility():
    directory = os.environ.get("ASMOND_SETTINGS_DIR")
    path = (Path(directory).expanduser() if directory else Path.home() / "Library/Application Support/Asmond") / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            return {"status": "not an object"}
    except (OSError, ValueError) as exc:
        return {"status": type(exc).__name__}
    # No profile names, free-form text, process identities or entire settings dump.
    result = {"status": "read", "overridden_directory": bool(directory)}
    for key, choices in {
        "layout": {"full", "compact", "focus", "custom"},
        "custom_grid": {"classic", "balanced", "columns"},
        "io_mode": {"disk_read", "disk_write", "net_in", "net_out"},
        "charge_panel": {"battery", "usb", "cable", "diagnostics"},
    }.items():
        value = settings.get(key)
        if isinstance(value, str) and value in choices:
            result[key] = value
    for key in ("show_io", "custom_show_graph"):
        if isinstance(settings.get(key), bool):
            result[key] = settings[key]
    layout = settings.get("custom_layout", {})
    if isinstance(layout, dict):
        result["custom_panel_counts"] = {
            panel: sum(isinstance(item, dict) and item.get("panel") == panel for item in layout.values())
            for panel in ("io", "power", "thermals", "battery", "usb", "cable")
        }
    return result


def installed_libexec():
    executable = shutil.which("asmond")
    candidates = [Path(executable).resolve().parent.parent / "libexec"] if executable else []
    candidates.extend(Path(prefix) / "opt/asmond/libexec" for prefix in ("/opt/homebrew", "/usr/local"))
    return next((path for path in candidates if (path / "asmond_smc.py").is_file() and (path / "asmond.py").is_file()), None)


def sensor_worker(libexec):
    """Reuse the selected Asmond ABI bindings in an isolated, timed subprocess.

    Only SMC commands 5 (read), 8 (indexed key lookup) and 9 (key info) are
    used. No fan-control or other write operation is exposed. Unsupported
    model maps remain unsupported; values here retain their raw key labels.
    """
    sys.path.insert(0, str(libexec))
    smc = importlib.import_module("asmond_smc")
    app = importlib.import_module("asmond")
    result = {"mapping_note": "Raw keys only; no CPU/GPU/core/fan assignment inferred", "smc": {}, "hid": []}
    reader = smc.AppleSMCTemperatureReader()
    try:
        if not reader.available:
            if ctypes.sizeof(smc._SMCKeyData) != 80:
                raise RuntimeError("unexpected SMC ABI size")
            reader.iokit = ctypes.CDLL(smc.IOKIT_PATH)
            reader.libsystem = ctypes.CDLL(smc.LIBSYSTEM_PATH)
            reader._bind()
            reader._open()
        if not reader.connection.value:
            raise RuntimeError("SMC user-client unavailable without privilege")

        def read_key(key):
            data = smc._SMCKeyData()
            data.key = int.from_bytes(key.encode("ascii"), "big")
            data.data8 = 9
            code, info = reader._call(data)
            if code != 0 or info.result != 0 or not 0 < info.key_info.data_size <= 32:
                return None
            data.key_info.data_size = info.key_info.data_size
            data.data8 = 5
            code, output = reader._call(data)
            if code != 0 or output.result != 0:
                return None
            raw = bytes(output.bytes[:info.key_info.data_size])
            kind = int(info.key_info.data_type).to_bytes(4, "big").decode("ascii", "replace")
            return kind, raw

        count_data = read_key("#KEY")
        if count_data is None or count_data[0] != "ui32" or len(count_data[1]) != 4:
            raise RuntimeError("SMC key count unavailable or unexpected type")
        total = int.from_bytes(count_data[1], "big")
        result["smc"]["reported_key_count"] = total
        result["smc"]["enumeration_capped"] = total > 8192
        keys = []
        failed = 0
        for index in range(min(total, 8192)):
            data = smc._SMCKeyData()
            data.data8 = 8
            data.data32 = index
            code, output = reader._call(data)
            if code != 0 or output.result != 0:
                failed += 1
                continue
            key = int(output.key).to_bytes(4, "big").decode("ascii", "replace")
            if re.fullmatch(r"T[\x20-\x7e]{3}|FNum|F[0-9A-Fa-f](?:Ac|Mn|Mx|Tg)", key):
                keys.append(key)
        result["smc"]["index_read_failures"] = failed
        result["smc"]["keys"] = sorted(set(keys))
        samples = []
        for _ in range(3):
            values = {}
            for key in sorted(set(keys)):
                entry = read_key(key)
                if entry is None:
                    values[key] = {"status": "read failed"}
                    continue
                kind, raw = entry
                value = None
                if kind == "flt " and len(raw) == 4:
                    value = struct.unpack("<f", raw)[0]
                elif kind == "sp78" and len(raw) == 2:
                    value = int.from_bytes(raw, "big", signed=True) / 256
                elif kind == "fpe2" and len(raw) == 2:
                    value = int.from_bytes(raw, "big") / 4
                elif kind in {"ui8 ", "ui16", "ui32"}:
                    value = int.from_bytes(raw, "big")
                # Raw bytes only for numeric sensor data, never arbitrary strings.
                values[key] = {"type": kind, "size": len(raw), "value": value}
                if value is not None:
                    values[key]["raw_hex"] = raw.hex()
            samples.append({"monotonic_s": time.monotonic(), "values": values})
            time.sleep(0.5)
        result["smc"]["samples"] = samples
    except Exception as exc:
        result["smc"]["error"] = str(exc)
    finally:
        reader.close()

    hid = app.HID_TEMPS
    services = None
    try:
        if not hid.available:
            raise RuntimeError("IOHID temperature client unavailable")
        services = hid.iokit.IOHIDEventSystemClientCopyServices(hid.client)
        if services:
            for index in range(min(int(hid.cf.CFArrayGetCount(services)), 2048)):
                service = hid.cf.CFArrayGetValueAtIndex(services, index)
                if not service:
                    continue
                name = hid._service_name(service, hid.product_key)
                event = hid.iokit.IOHIDServiceClientCopyEvent(service, app.IOHID_TEMP_TYPE, 0, 0)
                if event:
                    try:
                        value = float(hid.iokit.IOHIDEventGetFloatValue(event, app.IOHID_TEMP_FIELD))
                    finally:
                        hid.cf.CFRelease(event)
                    if math.isfinite(value):
                        result["hid"].append({"sensor": name, "reported_temperature_c": value})
    except Exception as exc:
        result["hid_error"] = str(exc)
    finally:
        if services:
            hid.cf.CFRelease(services)
    print(json.dumps(clean(result), indent=2, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="neutral state label, e.g. magsafe-idle or usbc-left")
    parser.add_argument("output", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--skip-sudo", action="store_true", help="partial collection without powermetrics")
    parser.add_argument("--intel", action="store_true", help="collect on a native Intel Mac, including individual SMC fan keys")
    parser.add_argument("--source-dir", type=Path, help="use this Asmond source directory instead of an installed command")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", args.label):
        parser.error("label must be 1..40 neutral ASCII letters/digits/dots/dashes/underscores")
    architecture = "x86_64" if args.intel else "arm64"
    if platform.system() != "Darwin" or platform.machine() != architecture:
        parser.error(f"run with a native {architecture} Python 3.10 or newer" + ("; omit --intel on Apple Silicon" if args.intel else "; use --intel for an Intel Mac"))
    if args.intel:
        code, translated, _ = run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"])
        if code == 0 and translated.strip() == b"1":
            parser.error("Rosetta is not native Intel hardware; use a native arm64 Python without --intel")
    if os.geteuid() == 0:
        parser.error("run as your normal user, NOT with sudo")
    if not args.output.is_dir():
        parser.error("output directory must already exist")
    if args.source_dir is not None:
        libexec = args.source_dir.expanduser().resolve()
        if not all((libexec / name).is_file() for name in ("asmond.py", "asmond_smc.py")):
            parser.error("--source-dir must contain asmond.py and asmond_smc.py")
        asmond_command = [sys.executable, str(libexec / "asmond.py")]
    else:
        libexec = installed_libexec()
        executable = shutil.which("asmond")
        if not libexec or not executable:
            parser.error("installed Asmond not found; use --source-dir /path/to/asmond for a Git checkout")
        asmond_command = [executable]
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    capture_platform = "intel-mac" if args.intel else "apple-silicon"
    name = f"asmond-{capture_platform}-{args.label}-{stamp}"
    archive = args.output.resolve() / f"{name}.tgz"
    with tempfile.TemporaryDirectory(prefix=f"asmond-{capture_platform}-") as temporary:
        bundle = Path(temporary) / name
        bundle.mkdir()

        def write_json(filename, data):
            (bundle / f"{filename}.json").write_text(json.dumps(clean(data), indent=2, allow_nan=False) + "\n", encoding="utf-8")

        def capture(filename, command, kind="text", timeout=25):
            code, stdout, stderr = run(command, timeout)
            metadata = {"exit_code": code, "stderr": clean_text(stderr.decode("utf-8", "replace"))}
            try:
                if kind == "plist":
                    write_json(filename, parse_plists(stdout) if stdout else [])
                elif kind == "json":
                    write_json(filename, json.loads(stdout))
                else:
                    text = network_rows(stdout) if kind == "network" else stdout.decode("utf-8", "replace")
                    (bundle / f"{filename}.txt").write_text(clean_text(text), encoding="utf-8")
            except Exception as exc:
                # Never fall back to exporting unparsed structured data.
                metadata["parse_error"] = type(exc).__name__
            write_json(f"{filename}.status", metadata)
            return stdout

        write_json("capture", {"collector_version": 3, "label": args.label, "utc": stamp, "python": platform.python_version(), "native_arch": platform.machine(), "platform": capture_platform, "asmond_origin": "source" if args.source_dir is not None else "installed", "terminal_size": list(shutil.get_terminal_size())})
        write_json("display-settings", settings_visibility())
        capture("macos", ["/usr/bin/sw_vers"])
        for key in ("hw.model", "hw.memsize", "hw.ncpu", "hw.physicalcpu", "hw.logicalcpu", "machdep.cpu.brand_string", "hw.perflevel0.logicalcpu", "hw.perflevel1.logicalcpu", "sysctl.proc_translated"):
            capture(f"sysctl-{key}", ["/usr/sbin/sysctl", key])
        if args.intel:
            for key in ("machdep.cpu.family", "machdep.cpu.model", "machdep.cpu.stepping", "machdep.cpu.core_count", "machdep.cpu.thread_count", "machdep.cpu.microcode_version"):
                capture(f"sysctl-{key}", ["/usr/sbin/sysctl", key])
        for option in ("batt", "ps", "therm"):
            capture(f"pmset-{option}", ["/usr/bin/pmset", "-g", option])
        capture("hardware-profile", ["/usr/sbin/system_profiler", "SPHardwareDataType", "-xml"], "plist")
        capture("display-profile", ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-xml"], "plist")
        for class_name in ("AppleSmartBattery", "AppleSmartBatteryManager", "AppleHPMInterface", "AppleTCController", "IOPort", "IOPortTransportComponentCCUSBPDSOPp", "IOPortTransportComponentCCUSBPDSOPpp", "AppleUSBXHCIPort", "IOUSBHostDevice", "AppleSMC", "AppleSMCFan", "IOHWSensor", "IOPlatformPluginDevice"):
            capture(f"ioreg-{class_name}", ["/usr/sbin/ioreg", "-a", "-r", "-c", class_name], "plist")
        capture("sensor-discovery", [sys.executable, str(Path(__file__).resolve()), "--sensor-worker", str(libexec)], "json", 45)
        for sample in range(3):
            write_json(f"io-{sample}-time", {"monotonic_s": time.monotonic()})
            capture(f"disk-{sample}", ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOBlockStorageDriver", "-k", "Statistics"], "plist")
            capture(f"network-{sample}", ["/usr/sbin/netstat", "-ibn"], "network")
            time.sleep(1)
        capture("asmond-version", [*asmond_command, "--version"])
        capture("asmond-doctor", [*asmond_command, "doctor", "--json"], "json")
        capture("asmond-report", [*asmond_command, "report", "--json"], "json")
        help_text = capture("powermetrics-help", ["/usr/bin/powermetrics", "--help"]).decode("utf-8", "replace")
        sudo_ready = False
        if not args.skip_sudo and sys.stdin.isatty():
            print("Sudo authorization for powermetrics only (no fan/control writes).", flush=True)
            sudo_ready = subprocess.run(["/usr/bin/sudo", "-v"], check=False).returncode == 0
        write_json("privileged-sampling", {"authorized": sudo_ready, "skip_requested": args.skip_sudo})
        if sudo_ready:
            candidates = ("cpu_power", "gpu_power", "thermal", "smc", "gpu_agpm_stats") if args.intel else ("cpu_power", "gpu_power", "ane_power", "thermal", "smc")
            samplers = [sampler for sampler in candidates if re.search(r"(?<![\w])" + sampler + r"(?![\w])", help_text)]
            if samplers:
                command = ["/usr/bin/sudo", "-n", "/usr/bin/powermetrics", "--samplers", ",".join(samplers), "--sample-rate", "1000", "--sample-count", "5"]
                if "--handle-invalid-values" in help_text:
                    command.append("--handle-invalid-values")
                write_json("powermetrics-selection", {"samplers": samplers})
                capture("powermetrics", command + ["--format", "plist"], "plist", 20)
                capture("powermetrics-text", command + ["--format", "text"], timeout=20)
            capture("asmond-live-report", [*asmond_command, "report", "--live", "--json"], "json", 30)
        (bundle / "README.txt").write_text(
            "Read-only Mac research; no runtime/settings changes or artificial load.\n"
            "Raw sensor keys are NOT validated CPU/GPU/core mappings.\n"
            "No full process/network/configuration dumps. Common identifiers are redacted;\n"
            "binary registry payloads are replaced by lengths (4-byte identity VDOs\n"
            "become numeric PD words). Check before sharing.\n"
            "JSON files retain plist structure (except redactions); command exit/error/parse\n"
            "status is separate. Failed/empty captures are not proof of absent hardware.\n"
            "Only powermetrics needs sudo. Sensor worker failure is recorded separately.\n",
            encoding="utf-8",
        )
        files = sorted(bundle.iterdir())
        (bundle / "manifest.sha256").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files), encoding="utf-8")
        with tarfile.open(archive, "x:gz") as tar:
            for path in sorted(bundle.iterdir()):
                info = tar.gettarinfo(str(path), arcname=f"{name}/{path.name}")
                info.uid = info.gid = 0
                info.uname = "root"
                info.gname = "wheel"
                info.mode = 0o600
                info.pax_headers = {}
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    with checksum.open("x", encoding="utf-8") as handle:
        handle.write(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    print(f"Done: {archive}\nSHA256: {checksum}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--sensor-worker":
        sensor_worker(Path(sys.argv[2]))
    else:
        main()
