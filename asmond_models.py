from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


PROCESS_GPU_UNAVAILABLE_BACKOFF = 60.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class CoreMetric:
    label: str
    usage_pct: float | None = None
    freq_mhz: float | None = None


@dataclass
class MemoryStats:
    total_bytes: int = 0
    available_bytes: int = 0
    cached_bytes: int = 0
    free_bytes: int = 0
    active_bytes: int = 0
    wired_bytes: int = 0
    compressed_bytes: int = 0
    swap_total_bytes: int = 0
    swap_used_bytes: int = 0
    swap_free_bytes: int = 0
    system_free_pct: float | None = None

    @property
    def used_bytes(self) -> int:
        direct_used = self.active_bytes + self.wired_bytes
        if direct_used > 0:
            return direct_used
        if self.total_bytes <= 0:
            return 0
        return max(0, self.total_bytes - min(self.total_bytes, self.available_bytes))

    @property
    def used_pct(self) -> float | None:
        if self.total_bytes <= 0:
            return None
        return _clamp(self.used_bytes / self.total_bytes * 100.0, 0.0, 100.0)

    @property
    def pressure_pct(self) -> float | None:
        if self.total_bytes <= 0:
            return None
        if self.system_free_pct is not None:
            return _clamp(100.0 - self.system_free_pct, 0.0, 100.0)
        return _clamp(100.0 - self.available_bytes / self.total_bytes * 100.0, 0.0, 100.0)

    @property
    def physical_used_bytes(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return max(0, self.total_bytes - min(self.total_bytes, self.free_bytes))

    @property
    def physical_used_pct(self) -> float | None:
        if self.total_bytes <= 0:
            return None
        return _clamp(self.physical_used_bytes / self.total_bytes * 100.0, 0.0, 100.0)


@dataclass
class BatteryStats:
    power_mw: float | None = None
    temperature_c: float | None = None
    charge_pct: float | None = None
    health_pct: float | None = None
    cycle_count: int | None = None
    time_remaining_min: int | None = None
    charging: bool | None = None
    external_connected: bool | None = None
    design_capacity: int | None = None
    max_capacity: int | None = None
    raw_max_capacity: int | None = None


@dataclass
class UsbCPortStats:
    label: str
    connected: bool = False
    role: str = "unknown"
    voltage_v: float | None = None
    current_a: float | None = None
    power_w: float | None = None
    max_power_w: float | None = None
    pdo_labels: list[str] = field(default_factory=list)
    cable: str = "unknown"


@dataclass
class HpmPortInfo:
    label: str
    connected: bool = False
    port_type: str = ""


@dataclass
class UsbCStats:
    ports: list[UsbCPortStats] = field(default_factory=list)
    active_index: int | None = None
    external_connected: bool | None = None
    charging: bool | None = None
    system_voltage_v: float | None = None
    system_current_a: float | None = None
    system_power_w: float | None = None
    adapter_voltage_v: float | None = None
    adapter_current_a: float | None = None
    adapter_contract_power_w: float | None = None
    adapter_power_w: float | None = None
    adapter_name: str = ""

    @property
    def active_port(self) -> UsbCPortStats | None:
        if self.active_index is None:
            return None
        if 0 <= self.active_index < len(self.ports):
            return self.ports[self.active_index]
        return None


@dataclass
class IoSnapshot:
    timestamp: float = field(default_factory=time.monotonic)
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    net_in_bytes: int = 0
    net_out_bytes: int = 0


@dataclass
class IoStats:
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None
    net_in_bps: float | None = None
    net_out_bps: float | None = None

    @property
    def disk_bps(self) -> float | None:
        if self.disk_read_bps is None and self.disk_write_bps is None:
            return None
        return (self.disk_read_bps or 0.0) + (self.disk_write_bps or 0.0)


@dataclass
class ProcessInfo:
    pid: int
    cpu_pct: float
    mem_pct: float
    rss_kib: int
    command: str
    ppid: int | None = None
    etime: str = ""
    full_command: str = ""
    user: str = ""
    gpu_pct: float | None = None


@dataclass
class SideMetricsUpdate:
    memory: MemoryStats | None = None
    battery: BatteryStats | None = None
    usb_c: UsbCStats | None = None
    io_stats: IoStats | None = None
    processes: list[ProcessInfo] | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SideMetricsPollState:
    poll_io: bool = False
    poll_processes: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, poll_io: bool, poll_processes: bool) -> None:
        with self.lock:
            self.poll_io = poll_io
            self.poll_processes = poll_processes

    def snapshot(self) -> tuple[bool, bool]:
        with self.lock:
            return self.poll_io, self.poll_processes


@dataclass
class ProcessGpuProbeState:
    unavailable_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def can_probe(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self.lock:
            return current >= self.unavailable_until

    def mark_unavailable(self, now: float | None = None, backoff_s: float = PROCESS_GPU_UNAVAILABLE_BACKOFF) -> None:
        current = time.monotonic() if now is None else now
        with self.lock:
            self.unavailable_until = current + backoff_s

    def mark_available(self) -> None:
        with self.lock:
            self.unavailable_until = 0.0


@dataclass
class PendingKill:
    pid: int
    ppid: int | None
    user: str
    full_command: str
    command: str
    until: float

    @classmethod
    def from_process(cls, process: ProcessInfo, until: float) -> "PendingKill":
        return cls(process.pid, process.ppid, process.user, process.full_command, process.command, until)

    def matches(self, process: ProcessInfo) -> bool:
        return (
            self.pid == process.pid
            and self.ppid == process.ppid
            and self.user == process.user
            and self.full_command == process.full_command
        )


@dataclass
class MetricSample:
    timestamp: float = field(default_factory=time.time)
    cpu_power_mw: float | None = None
    gpu_power_mw: float | None = None
    ane_power_mw: float | None = None
    media_power_mw: float | None = None
    soc_power_mw: float | None = None
    battery_power_mw: float | None = None
    p_usage_pct: float | None = None
    e_usage_pct: float | None = None
    cpu_usage_pct: float | None = None
    gpu_usage_pct: float | None = None
    ane_usage_pct: float | None = None
    media_usage_pct: float | None = None
    p_freq_mhz: float | None = None
    e_freq_mhz: float | None = None
    gpu_freq_mhz: float | None = None
    soc_temp_c: float | None = None
    temp_max_c: float | None = None
    cores: list[CoreMetric] = field(default_factory=list)
    thermal_pressure: str | None = None
    throttled: bool | None = None
    throttle_reasons: list[str] = field(default_factory=list)
    memory_bandwidth_gbps: dict[str, float] = field(default_factory=dict)
    raw_keys: int = 0
    warning: str | None = None
