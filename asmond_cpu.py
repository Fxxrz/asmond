from __future__ import annotations

import ctypes
import ctypes.util
import time
from dataclasses import dataclass, field


HOST_CPU_LOAD_INFO = 3
HOST_CPU_LOAD_INFO_COUNT = 4
CPU_STATE_USER = 0
CPU_STATE_SYSTEM = 1
CPU_STATE_IDLE = 2
CPU_STATE_NICE = 3
_SYSTEM_LIB: ctypes.CDLL | None = None
_HOST_PORT: int | None = None


@dataclass
class CpuLoadSnapshot:
    ticks: tuple[int, int, int, int]
    timestamp: float = field(default_factory=time.monotonic)


def system_library() -> ctypes.CDLL:
    global _SYSTEM_LIB
    if _SYSTEM_LIB is None:
        system_path = ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib"
        lib = ctypes.CDLL(system_path)
        lib.mach_host_self.restype = ctypes.c_uint
        lib.host_statistics.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.host_statistics.restype = ctypes.c_int
        _SYSTEM_LIB = lib
    return _SYSTEM_LIB


def host_port() -> int:
    global _HOST_PORT
    if _HOST_PORT is None:
        _HOST_PORT = int(system_library().mach_host_self())
    return _HOST_PORT


def read_cpu_load_snapshot() -> CpuLoadSnapshot:
    lib = system_library()
    values = (ctypes.c_uint * HOST_CPU_LOAD_INFO_COUNT)()
    count = ctypes.c_uint(HOST_CPU_LOAD_INFO_COUNT)
    result = lib.host_statistics(host_port(), HOST_CPU_LOAD_INFO, values, ctypes.byref(count))
    if result != 0:
        raise OSError(result)
    return CpuLoadSnapshot(tuple(int(values[index]) for index in range(HOST_CPU_LOAD_INFO_COUNT)))


def cpu_usage_from_snapshots(previous: CpuLoadSnapshot | None, current: CpuLoadSnapshot | None) -> float | None:
    if previous is None or current is None:
        return None
    deltas = [current.ticks[index] - previous.ticks[index] for index in range(HOST_CPU_LOAD_INFO_COUNT)]
    if any(delta < 0 for delta in deltas):
        return None
    total = sum(deltas)
    if total <= 0:
        return None
    active = deltas[CPU_STATE_USER] + deltas[CPU_STATE_SYSTEM] + deltas[CPU_STATE_NICE]
    return max(0.0, min(100.0, active * 100.0 / total))
