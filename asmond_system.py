from __future__ import annotations

import ctypes as c
import os
import platform
import subprocess
import threading
import time
import uuid
from typing import Callable


SUDO_PATH = "/usr/bin/sudo"
POWERMETRICS_PATH = "/usr/bin/powermetrics"
IOREG_PATH = "/usr/sbin/ioreg"
SYSCTL_PATH = "/usr/sbin/sysctl"
VM_STAT_PATH = "/usr/bin/vm_stat"
MEMORY_PRESSURE_PATH = "/usr/bin/memory_pressure"
NETSTAT_PATH = "/usr/sbin/netstat"
PS_PATH = "/bin/ps"

SYSTEM_COMMANDS = {
    "sudo": SUDO_PATH,
    "powermetrics": POWERMETRICS_PATH,
    "ioreg": IOREG_PATH,
    "sysctl": SYSCTL_PATH,
    "vm_stat": VM_STAT_PATH,
    "memory_pressure": MEMORY_PRESSURE_PATH,
    "ps": PS_PATH,
    "netstat": NETSTAT_PATH,
}

RunCommand = Callable[..., subprocess.CompletedProcess]


def system_environment() -> dict[str, str]:
    """Return a small environment for trusted macOS command-line tools."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
    for name in ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def physical_machine(
    machine: str | None = None,
    run: RunCommand = subprocess.run,
) -> tuple[str, bool]:
    """Return the physical Mac architecture and whether Python runs via Rosetta."""
    process_machine = (machine or platform.machine() or "unknown").lower()
    if process_machine not in {"x86_64", "amd64"}:
        return process_machine, False
    try:
        proc = run(
            [SYSCTL_PATH, "-n", "sysctl.proc_translated"],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
    except Exception:
        return process_machine, False
    translated = proc.returncode == 0 and proc.stdout.decode("utf-8", "ignore").strip() == "1"
    return ("arm64", True) if translated else (process_machine, False)


# Read-only internal NVMe SMART via Apple NVMeSMARTLibExternal.h/IOCFPlugIn.h.
# This stays in the existing system module so installed 0.9.x updaters can
# accept future archives with their original, strict runtime-file allowlist.
V = c.c_void_p
U = c.c_uint32


class _UUIDBytes(c.Structure):
    _fields_ = [("bytes", c.c_ubyte * 16)]


class _SMARTInterface(c.Structure):
    # IUNKNOWN_C_GUTS followed by Apple's version/revision and SMARTReadData.
    _fields_ = [("reserved", V), ("query", V), ("add_ref", V), ("release", V),
                ("version", c.c_uint16), ("revision", c.c_uint16), ("smart_read_data", V)]


def nvme_temperature_c(data: bytes) -> float | None:
    if len(data) != 512:
        return None
    kelvin = int.from_bytes(data[1:3], "little")
    value = kelvin - 273.15
    return value if 0 < value < 150 else None


class InternalNVMeReader:
    def __init__(self) -> None:
        self.cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.io = c.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        for lib, name, args, result in (
            (self.cf, "CFStringCreateWithCString", [V, c.c_char_p, U], V),
            (self.cf, "CFStringGetCString", [V, V, c.c_long, U], c.c_bool),
            (self.cf, "CFUUIDCreateFromString", [V, V], V),
            (self.cf, "CFGetTypeID", [V], c.c_ulong),
            (self.cf, "CFBooleanGetTypeID", [], c.c_ulong),
            (self.cf, "CFDictionaryGetTypeID", [], c.c_ulong),
            (self.cf, "CFStringGetTypeID", [], c.c_ulong),
            (self.cf, "CFDictionaryGetValue", [V, V], V),
            (self.cf, "CFBooleanGetValue", [V], c.c_bool),
            (self.cf, "CFRelease", [V], None),
            (self.io, "IOServiceMatching", [c.c_char_p], V),
            (self.io, "IOServiceGetMatchingServices", [U, V, c.POINTER(U)], c.c_int),
            (self.io, "IOIteratorNext", [U], U),
            (self.io, "IOObjectRelease", [U], c.c_int),
            (self.io, "IORegistryEntryCreateCFProperty", [U, V, V, U], V),
            (self.io, "IOCreatePlugInInterfaceForService", [U, V, V, c.POINTER(V), c.POINTER(c.c_int32)], c.c_int),
            (self.io, "IODestroyPlugInInterface", [V], c.c_int),
        ):
            function = getattr(lib, name)
            function.argtypes, function.restype = args, result

    def _string(self, text: str):
        return self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)

    def _uuid(self, text: str):
        string = self._string(text)
        try:
            return self.cf.CFUUIDCreateFromString(None, string) if string else None
        finally:
            if string:
                self.cf.CFRelease(string)

    def _property(self, device: int, name: str):
        key = self._string(name)
        try:
            return self.io.IORegistryEntryCreateCFProperty(device, key, None, 0) if key else None
        finally:
            if key:
                self.cf.CFRelease(key)

    def _internal(self, device: int) -> bool:
        prop = self._property(device, "Protocol Characteristics")
        key = self._string("Physical Interconnect Location")
        try:
            if not prop or not key or self.cf.CFGetTypeID(prop) != self.cf.CFDictionaryGetTypeID():
                return False
            location = self.cf.CFDictionaryGetValue(prop, key)
            if not location or self.cf.CFGetTypeID(location) != self.cf.CFStringGetTypeID():
                return False
            text = c.create_string_buffer(128)
            return bool(self.cf.CFStringGetCString(location, text, len(text), 0x08000100)) and text.value == b"Internal"
        finally:
            for obj in (key, prop):
                if obj:
                    self.cf.CFRelease(obj)

    def _capable(self, device: int) -> bool:
        prop = self._property(device, "NVMe SMART Capable")
        try:
            return bool(prop and self.cf.CFGetTypeID(prop) == self.cf.CFBooleanGetTypeID()
                        and self.cf.CFBooleanGetValue(prop))
        finally:
            if prop:
                self.cf.CFRelease(prop)

    def _read_device(self, device: int) -> float | None:
        plugin, smart = V(), V()
        client = self._uuid("AA0FA6F9-C2D6-457F-B10B-59A13253292F")
        interface = self._uuid("C244E858-109C-11D4-91D4-0050E4C6426F")
        try:
            if not client or not interface:
                return None
            score = c.c_int32()
            if self.io.IOCreatePlugInInterfaceForService(device, client, interface, c.byref(plugin), c.byref(score)) or not plugin.value:
                return None
            table = c.cast(plugin, c.POINTER(c.POINTER(_SMARTInterface))).contents.contents
            if not table.query:
                return None
            identity = _UUIDBytes((c.c_ubyte * 16).from_buffer_copy(uuid.UUID("CCD1DB19-FD9A-4DAF-BF95-12454B230AB6").bytes))
            if c.CFUNCTYPE(c.c_int32, V, _UUIDBytes, c.POINTER(V))(table.query)(plugin, identity, c.byref(smart)) or not smart.value:
                return None
            table = c.cast(smart, c.POINTER(c.POINTER(_SMARTInterface))).contents.contents
            if table.version != 1 or not table.smart_read_data:
                return None
            data = (c.c_ubyte * 512)()
            if c.CFUNCTYPE(c.c_int, V, V)(table.smart_read_data)(smart, c.byref(data)):
                return None
            return nvme_temperature_c(bytes(data))
        finally:
            if smart.value:
                table = c.cast(smart, c.POINTER(c.POINTER(_SMARTInterface))).contents.contents
                if table.release:
                    c.CFUNCTYPE(U, V)(table.release)(smart)
            if plugin.value:
                self.io.IODestroyPlugInInterface(plugin)
            for obj in (client, interface):
                if obj:
                    self.cf.CFRelease(obj)

    def read(self) -> float | None:
        iterator = U()
        devices = []
        try:
            matching = self.io.IOServiceMatching(b"IOBlockStorageDevice")
            if not matching or self.io.IOServiceGetMatchingServices(0, matching, c.byref(iterator)):
                return None
            # Bound enumeration and reject ambiguity, including truncated lists.
            for _ in range(64):
                device = self.io.IOIteratorNext(iterator)
                if not device:
                    return self._read_device(devices[0]) if len(devices) == 1 else None
                try:
                    if self._internal(device) and self._capable(device):
                        devices.append(device)
                        device = 0
                finally:
                    if device:
                        self.io.IOObjectRelease(device)
            return None
        finally:
            for device in devices:
                self.io.IOObjectRelease(device)
            if iterator.value:
                self.io.IOObjectRelease(iterator)


class StorageTemperatureCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: float | None = None
        self.started_at = 0.0
        self.value_started_at = 0.0
        self.refreshing = False
        self.thread: threading.Thread | None = None
        self.generation = 0

    def invalidate(self) -> None:
        with self.lock:
            self.generation += 1
            self.value = None
            self.started_at = self.value_started_at = 0.0
            # A running native call keeps its worker slot, but its old result
            # cannot repopulate the cache after a clock gap.

    def _refresh(self, generation: int, started_at: float) -> None:
        try:
            value = InternalNVMeReader().read()
        except Exception:
            value = None
        with self.lock:
            if generation == self.generation:
                self.value = value
                self.value_started_at = started_at
            self.refreshing = False

    def snapshot(self, *, wait_s: float = 0.0, now: float | None = None) -> float | None:
        if platform.system() != "Darwin":
            return None
        current = time.monotonic() if now is None else now
        with self.lock:
            if not self.refreshing and (self.started_at == 0.0 or current - self.started_at >= 10.0):
                thread = threading.Thread(target=self._refresh, args=(self.generation, current), daemon=True)
                try:
                    thread.start()
                except RuntimeError:
                    pass  # Keep the previous snapshot and allow the next request to retry.
                else:
                    self.started_at = current
                    self.refreshing = True
                    self.thread = thread
            thread = self.thread
        if thread is not None and wait_s > 0:
            thread.join(timeout=min(wait_s, 1.0))
        with self.lock:
            current = time.monotonic() if now is None else now
            # A stuck native call cannot create more workers or keep old data.
            return self.value if self.value_started_at > 0 and 0 <= current - self.value_started_at <= 30.0 else None
