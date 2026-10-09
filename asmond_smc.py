from __future__ import annotations

import ctypes
import math
import platform
import struct
import subprocess
import threading
from dataclasses import dataclass


IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
LIBSYSTEM_PATH = "/usr/lib/libSystem.B.dylib"
SYSCTL_PATH = "/usr/sbin/sysctl"

KERN_SUCCESS = 0
SMC_SELECTOR = 2
SMC_READ_BYTES = 5
SMC_READ_KEY_INFO = 9
SMC_KEY_NOT_FOUND = 0x84

# Verified on the base Apple M4 (4 P + 6 E cores). The CPU keys were matched
# against the ten per-core readings shown by Macs Fan Control and checked under
# CPU load. The GPU keys are the established M4 set and were checked under a
# dedicated Metal compute load. Other SoCs deliberately remain unsupported
# here until their key sets have equally strong evidence.
M4_BASE_CPU_KEYS = (
    "Te05",
    "Te09",
    "Te0H",
    "Te0S",
    "Te0U",
    "Te0X",
    "Tp01",
    "Tp05",
    "Tp09",
    "Tp0D",
)
M4_BASE_CPU_LABELS = (
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
    "E6",
    "P1",
    "P2",
    "P3",
    "P4",
)
M4_BASE_GPU_KEYS = (
    "Tg0G",
    "Tg0H",
    "Tg0K",
    "Tg0L",
    "Tg0d",
    "Tg0e",
    "Tg0j",
    "Tg0k",
)
M4_BASE_AUX_KEYS = {
    "airport_proximity_c": "TW0P",
    "power_supply_proximity_c": "TPSP",
    "trackpad_c": "Ts0P",
    "trackpad_actuator_c": "Ts1P",
}


@dataclass(frozen=True)
class TemperatureKeyGroups:
    cpu: tuple[str, ...] = ()
    cpu_labels: tuple[str, ...] = ()
    gpu: tuple[str, ...] = ()
    auxiliary: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SMCTemperatureSample:
    cpu_avg_c: float | None = None
    cpu_max_c: float | None = None
    gpu_avg_c: float | None = None
    gpu_max_c: float | None = None
    airport_proximity_c: float | None = None
    power_supply_proximity_c: float | None = None
    trackpad_c: float | None = None
    trackpad_actuator_c: float | None = None
    cpu_max_label: str | None = None


def temperature_key_groups(brand: str, performance_cores: int, efficiency_cores: int) -> TemperatureKeyGroups:
    if brand.strip() == "Apple M4" and (performance_cores, efficiency_cores) == (4, 6):
        return TemperatureKeyGroups(
            cpu=M4_BASE_CPU_KEYS,
            cpu_labels=M4_BASE_CPU_LABELS,
            gpu=M4_BASE_GPU_KEYS,
            auxiliary=tuple(M4_BASE_AUX_KEYS.items()),
        )
    return TemperatureKeyGroups()


def summarize_temperatures(values: list[float | None], expected_count: int) -> tuple[float | None, float | None]:
    if expected_count <= 0:
        return None, None
    valid = [value for value in values if value is not None and math.isfinite(value) and 0.0 < value < 150.0]
    if len(valid) != expected_count:
        return None, None
    return sum(valid) / len(valid), max(valid)


class _SMCVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_ubyte),
        ("minor", ctypes.c_ubyte),
        ("build", ctypes.c_ubyte),
        ("reserved", ctypes.c_ubyte),
        ("release", ctypes.c_ushort),
    ]


class _SMCLimitData(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_ushort),
        ("length", ctypes.c_ushort),
        ("cpu_limit", ctypes.c_uint32),
        ("gpu_limit", ctypes.c_uint32),
        ("memory_limit", ctypes.c_uint32),
    ]


class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [
        ("data_size", ctypes.c_uint32),
        ("data_type", ctypes.c_uint32),
        ("data_attributes", ctypes.c_ubyte),
    ]


class _SMCKeyData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("version", _SMCVersion),
        ("limit_data", _SMCLimitData),
        ("key_info", _SMCKeyInfo),
        ("result", ctypes.c_ubyte),
        ("status", ctypes.c_ubyte),
        ("data8", ctypes.c_ubyte),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * 32),
    ]


def _sysctl_text(name: str) -> str:
    try:
        proc = subprocess.run(
            [SYSCTL_PATH, "-n", name],
            check=False,
            capture_output=True,
            timeout=1.0,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"},
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "ignore").strip()


def _sysctl_int(name: str) -> int:
    try:
        return int(_sysctl_text(name))
    except ValueError:
        return 0


class AppleSMCTemperatureReader:
    """Read a small, verified set of AppleSMC temperature keys.

    This class exposes no write operation. Unsupported hardware and incomplete
    key groups return an empty sample rather than a guessed temperature.
    """

    def __init__(
        self,
        brand: str | None = None,
        performance_cores: int | None = None,
        efficiency_cores: int | None = None,
    ) -> None:
        self.available = False
        self.connection = ctypes.c_uint32(0)
        self._key_info: dict[int, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self.groups = TemperatureKeyGroups()
        if platform.system() != "Darwin":
            return
        brand = brand if brand is not None else _sysctl_text("machdep.cpu.brand_string")
        performance_cores = performance_cores if performance_cores is not None else _sysctl_int("hw.perflevel0.logicalcpu")
        efficiency_cores = efficiency_cores if efficiency_cores is not None else _sysctl_int("hw.perflevel1.logicalcpu")
        self.groups = temperature_key_groups(brand, performance_cores, efficiency_cores)
        if not self.groups.cpu and not self.groups.gpu:
            return
        if ctypes.sizeof(_SMCKeyData) != 80:
            return
        try:
            self.iokit = ctypes.CDLL(IOKIT_PATH)
            self.libsystem = ctypes.CDLL(LIBSYSTEM_PATH)
            self._bind()
            self._open()
            self.available = bool(self.connection.value)
        except Exception:
            self.close()

    def _bind(self) -> None:
        self.iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        self.iokit.IOServiceMatching.restype = ctypes.c_void_p
        self.iokit.IOServiceGetMatchingServices.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
        self.iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
        self.iokit.IOIteratorNext.restype = ctypes.c_uint32
        self.iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        self.iokit.IOObjectRelease.restype = ctypes.c_int
        self.iokit.IOServiceOpen.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.iokit.IOServiceOpen.restype = ctypes.c_int
        self.iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
        self.iokit.IOServiceClose.restype = ctypes.c_int
        self.iokit.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.iokit.IOConnectCallStructMethod.restype = ctypes.c_int

    def _open(self) -> None:
        iterator = ctypes.c_uint32(0)
        matching = self.iokit.IOServiceMatching(b"AppleSMC")
        if not matching:
            return
        if self.iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)) != KERN_SUCCESS:
            return
        device = self.iokit.IOIteratorNext(iterator)
        self.iokit.IOObjectRelease(iterator)
        if not device:
            return
        try:
            task = ctypes.c_uint32.in_dll(self.libsystem, "mach_task_self_").value
            result = self.iokit.IOServiceOpen(device, task, 0, ctypes.byref(self.connection))
            if result != KERN_SUCCESS:
                self.connection = ctypes.c_uint32(0)
        finally:
            self.iokit.IOObjectRelease(device)

    def close(self) -> None:
        connection = getattr(self, "connection", ctypes.c_uint32(0))
        iokit = getattr(self, "iokit", None)
        if connection.value and iokit is not None:
            try:
                iokit.IOServiceClose(connection)
            except Exception:
                pass
        self.connection = ctypes.c_uint32(0)
        self.available = False

    def _call(self, input_data: _SMCKeyData) -> tuple[int, _SMCKeyData]:
        output_data = _SMCKeyData()
        output_size = ctypes.c_size_t(ctypes.sizeof(output_data))
        result = self.iokit.IOConnectCallStructMethod(
            self.connection,
            SMC_SELECTOR,
            ctypes.byref(input_data),
            ctypes.sizeof(input_data),
            ctypes.byref(output_data),
            ctypes.byref(output_size),
        )
        return result, output_data

    def _read_value(self, key: str) -> float | None:
        if len(key.encode("ascii", "strict")) != 4:
            return None
        key_code = int.from_bytes(key.encode("ascii"), "big")
        key_info = self._key_info.get(key_code)
        if key_info is None:
            input_data = _SMCKeyData()
            input_data.key = key_code
            input_data.data8 = SMC_READ_KEY_INFO
            result, output_data = self._call(input_data)
            if result != KERN_SUCCESS or output_data.result in {SMC_KEY_NOT_FOUND} or output_data.result != 0:
                return None
            data_size = int(output_data.key_info.data_size)
            data_type = int(output_data.key_info.data_type)
            if not (0 < data_size <= 32) or data_type == 0:
                return None
            key_info = (data_size, data_type)

        data_size, data_type = key_info
        input_data = _SMCKeyData()
        input_data.key = key_code
        input_data.key_info.data_size = data_size
        input_data.data8 = SMC_READ_BYTES
        result, output_data = self._call(input_data)
        if result != KERN_SUCCESS or output_data.result != 0:
            self._key_info.pop(key_code, None)
            return None
        self._key_info[key_code] = key_info
        raw = bytes(output_data.bytes[:data_size])
        type_name = data_type.to_bytes(4, "big").decode("ascii", "ignore")
        if type_name == "flt " and len(raw) >= 4:
            return float(struct.unpack("<f", raw[:4])[0])
        if type_name == "sp78" and len(raw) >= 2:
            return float(int.from_bytes(raw[:2], "big", signed=True) / 256.0)
        return None

    def read(self) -> SMCTemperatureSample:
        if not self.available:
            return SMCTemperatureSample()
        with self._lock:
            try:
                cpu_values = [self._read_value(key) for key in self.groups.cpu]
                gpu_values = [self._read_value(key) for key in self.groups.gpu]
                auxiliary = {name: self._read_value(key) for name, key in self.groups.auxiliary}
            except Exception:
                return SMCTemperatureSample()
        cpu_avg, cpu_max = summarize_temperatures(cpu_values, len(self.groups.cpu))
        gpu_avg, gpu_max = summarize_temperatures(gpu_values, len(self.groups.gpu))
        cpu_max_label = None
        if cpu_max is not None and len(self.groups.cpu_labels) == len(cpu_values):
            cpu_max_label = self.groups.cpu_labels[cpu_values.index(cpu_max)]
        return SMCTemperatureSample(
            cpu_avg_c=cpu_avg,
            cpu_max_c=cpu_max,
            gpu_avg_c=gpu_avg,
            gpu_max_c=gpu_max,
            cpu_max_label=cpu_max_label,
            **{
                name: value if value is not None and math.isfinite(value) and 0.0 < value < 150.0 else None
                for name, value in auxiliary.items()
            },
        )
