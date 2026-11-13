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
    # Live readers record which counters were acquired. Legacy callers and
    # demo samples can still provide the calculated fields directly.
    vm_page_size_bytes: int | None = None
    vm_pages: dict[str, int] = field(default_factory=dict)
    vm_sampled_at: float | None = None
    read_status: dict[str, str] = field(default_factory=dict)

    def vm_value(self, value: int, *keys: str) -> int | None:
        if "vm_stat" not in self.read_status:
            return value
        if self.read_status["vm_stat"] == "ok" and all(key in self.vm_pages for key in keys):
            return value
        return None

    @property
    def reclaimable_bytes(self) -> int | None:
        return self.vm_value(self.available_bytes, "pages free", "pages speculative",
                             "file-backed pages", "pages purgeable")

    @property
    def swap_usage_bytes(self) -> int | None:
        return self.swap_used_bytes if self.read_status.get("swap", "ok") == "ok" else None

    @property
    def used_bytes(self) -> int | None:
        direct_used = self.active_bytes + self.wired_bytes
        if "vm_stat" in self.read_status:
            measured = self.vm_value(direct_used, "pages active", "pages wired down")
            if measured is not None:
                return measured
        elif direct_used > 0:
            return direct_used
        available = self.reclaimable_bytes
        if self.total_bytes <= 0 or available is None:
            return None
        return max(0, self.total_bytes - min(self.total_bytes, available))

    @property
    def used_pct(self) -> float | None:
        used = self.used_bytes
        if self.total_bytes <= 0 or used is None:
            return None
        return _clamp(used / self.total_bytes * 100.0, 0.0, 100.0)

    @property
    def pressure_pct(self) -> float | None:
        if self.system_free_pct is not None:
            return _clamp(100.0 - self.system_free_pct, 0.0, 100.0)
        available = self.reclaimable_bytes
        if self.total_bytes <= 0 or available is None:
            return None
        return _clamp(100.0 - available / self.total_bytes * 100.0, 0.0, 100.0)

    @property
    def physical_used_bytes(self) -> int | None:
        free = self.vm_value(self.free_bytes, "pages free", "pages speculative")
        if self.total_bytes <= 0 or free is None:
            return None
        return max(0, self.total_bytes - min(self.total_bytes, free))

    @property
    def physical_used_pct(self) -> float | None:
        used = self.physical_used_bytes
        if self.total_bytes <= 0 or used is None:
            return None
        return _clamp(used / self.total_bytes * 100.0, 0.0, 100.0)


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
class CableInfo:
    cable_type: str = ""
    current_label: str = ""
    max_voltage_v: float | None = None
    max_power_w: float | None = None
    speed_label: str = ""
    vendor_id: int | None = None
    product_id: int | None = None
    pd_revision: str = ""


@dataclass
class UsbDeviceInfo:
    manufacturer: str = ""
    product: str = ""
    storage_manufacturer: str = ""
    storage_model: str = ""
    capacity_bytes: int | None = None
    medium_type: str = ""


@dataclass
class PortDiagnostic:
    key: str
    label: str
    mapped: bool = True
    connected: bool | None = None
    liquid_detected: bool | None = None
    liquid_state: str = ""
    measurement_status: str = ""
    counters: dict[str, int] = field(default_factory=dict)
    deltas: dict[str, int] = field(default_factory=dict)
    events: list[tuple[float, str]] = field(default_factory=list)
    raw_trace: bytes = b""
    source: str = ""
    overcurrent_active: bool | None = None
    counter_sources: dict[str, str] = field(default_factory=dict)


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
    cable_info: CableInfo | None = None
    cable_query_supported: bool = False
    active_transports: list[str] = field(default_factory=list)
    data_link_speed_bps: int | None = None
    data_link_query_supported: bool = False
    device_info: UsbDeviceInfo | None = None
    charge_query_supported: bool = True


@dataclass
class HpmPortInfo:
    label: str
    connected: bool = False
    port_type: str = ""
    port_type_code: int | None = None
    port_number: int | None = None
    active_transports: list[str] = field(default_factory=list)
    label_is_explicit: bool = False
    identity_invalid: bool = False


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
    input_kind: str = ""
    diagnostics: list[PortDiagnostic] = field(default_factory=list)
    sampled_at: float | None = None

    @property
    def active_port(self) -> UsbCPortStats | None:
        if self.active_index is None:
            return None
        if 0 <= self.active_index < len(self.ports):
            return self.ports[self.active_index]
        return None


@dataclass
class ChargeObservation:
    battery: BatteryStats = field(default_factory=BatteryStats)
    usb_c: UsbCStats = field(default_factory=UsbCStats)
    received_at: float | None = None
    resume_port: str | None = None
    liquid_reports: dict[str, str] = field(default_factory=dict)

    def selected_port(self, index: int) -> str | None:
        ports = self.usb_c.diagnostics
        return ports[index % len(ports)].key if ports else self.resume_port

    def receive_battery(self, battery: BatteryStats, now: float) -> None:
        self.battery = battery
        self.received_at = now

    def receive_usb(self, usb_c: UsbCStats, index: int, now: float) -> str | None:
        selected = self.selected_port(index)
        self.usb_c = usb_c
        self.received_at = usb_c.sampled_at if usb_c.sampled_at is not None else now
        self.resume_port = None if usb_c.diagnostics else selected
        for port in usb_c.diagnostics:
            if port.liquid_detected is True:
                self.liquid_reports[port.key] = port.label
            elif port.liquid_detected is False:
                self.liquid_reports.pop(port.key, None)
        return selected

    def invalidate(self, index: int) -> None:
        self.resume_port = self.selected_port(index)
        self.battery, self.usb_c = BatteryStats(), UsbCStats()
        self.received_at = None

    def expired(self, now: float, max_age: float) -> bool:
        return self.received_at is not None and now - self.received_at > max_age


@dataclass
class IoSnapshot:
    timestamp: float = field(default_factory=time.monotonic)
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    net_in_bytes: int = 0
    net_out_bytes: int = 0
    # None denotes a legacy totals-only snapshot; an empty map has no verified identities.
    disk_counters: dict[int, tuple[int, int]] | None = None
    net_counters: dict[tuple[str, str, str], tuple[int, int]] | None = None
    disk_sampled_at: float | None = None
    net_sampled_at: float | None = None
    failed_readers: tuple[str, ...] = ()


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
    start_time: str = ""
    cpu_time_s: float | None = None
    cpu_power_mw: float | None = None


ProcessIdentity = tuple[int, str, int | None, str, str]


def sampling_gap_limit_s(interval_s: float) -> float:
    return max(5.0, interval_s * 2.5)


def sampling_clock_gap(
    previous: tuple[float, float], current: tuple[float, float], interval_s: float,
) -> tuple[float, float] | None:
    wall_delta = current[0] - previous[0]
    monotonic_delta = current[1] - previous[1]
    if (
        monotonic_delta <= 0
        or monotonic_delta > sampling_gap_limit_s(interval_s)
        or abs(wall_delta - monotonic_delta) > max(2.0, interval_s * 0.5)
    ):
        return wall_delta, monotonic_delta
    return None


@dataclass
class SideMetricsUpdate:
    memory: MemoryStats | None = None
    battery: BatteryStats | None = None
    usb_c: UsbCStats | None = None
    cpu_usage_pct: float | None = None
    io_stats: IoStats | None = None
    processes: list[ProcessInfo] | None = None
    process_gpu_pcts: dict[ProcessIdentity, float] | None = None
    process_gpu_expires_at: float = 0.0
    warnings: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    observation_epoch: int = 0


@dataclass
class SideMetricsPollState:
    poll_io: bool = False
    poll_processes: bool = False
    sample_interval_s: float = 1.0
    revision: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    _observation_epoch: int = field(default=0, init=False, repr=False)
    _last_clock: tuple[float, float] | None = field(default=None, init=False, repr=False)

    def observe_clock(self) -> int:
        # All dashboard readers share the gap, whichever thread wakes first.
        with self.lock:
            clock = (time.time(), time.monotonic())
            previous = self._last_clock
            if previous is not None and clock != previous and sampling_clock_gap(previous, clock, self.sample_interval_s):
                self._observation_epoch += 1
            self._last_clock = clock
            return self._observation_epoch

    def update(self, poll_io: bool, poll_processes: bool, sample_interval_s: float | None = None) -> None:
        with self.lock:
            old_state = (self.poll_io, self.poll_processes, self.sample_interval_s)
            self.poll_io = poll_io
            self.poll_processes = poll_processes
            if sample_interval_s is not None:
                self.sample_interval_s = max(0.1, float(sample_interval_s))
            if (self.poll_io, self.poll_processes, self.sample_interval_s) != old_state:
                self.revision += 1

    def snapshot(self) -> tuple[bool, bool, float, int]:
        with self.lock:
            return self.poll_io, self.poll_processes, self.sample_interval_s, self.revision


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
class SamplerRetryState:
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    failures: int = 0
    retry_at: float = 0.0

    def schedule(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        delay = min(self.max_delay_s, self.base_delay_s * (2 ** min(self.failures, 10)))
        self.failures += 1
        self.retry_at = current + delay
        return delay

    def ready(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return self.retry_at > 0.0 and current >= self.retry_at

    def reset(self) -> None:
        self.failures = 0
        self.retry_at = 0.0


@dataclass
class PendingKill:
    pid: int
    ppid: int | None
    user: str
    full_command: str
    command: str
    start_time: str
    until: float

    @classmethod
    def from_process(cls, process: ProcessInfo, until: float) -> "PendingKill":
        return cls(process.pid, process.ppid, process.user, process.full_command, process.command, process.start_time, until)

    def matches(self, process: ProcessInfo) -> bool:
        return (
            self.pid == process.pid
            and self.ppid == process.ppid
            and self.user == process.user
            and self.full_command == process.full_command
            and self.start_time == process.start_time
        )


@dataclass
class MetricSample:
    timestamp: float = field(default_factory=time.time)
    telemetry_source: str | None = None
    cpu_power_mw: float | None = None
    gpu_power_mw: float | None = None
    ane_power_mw: float | None = None
    ram_power_mw: float | None = None
    media_power_mw: float | None = None
    soc_power_mw: float | None = None
    battery_power_mw: float | None = None
    p_usage_pct: float | None = None
    e_usage_pct: float | None = None
    cpu_usage_pct: float | None = None
    cpu_usage_source: str | None = None
    gpu_usage_pct: float | None = None
    ane_usage_pct: float | None = None
    media_usage_pct: float | None = None
    p_freq_mhz: float | None = None
    e_freq_mhz: float | None = None
    gpu_freq_mhz: float | None = None
    cpu_temp_avg_c: float | None = None
    cpu_temp_max_c: float | None = None
    cpu_temp_max_label: str | None = None
    gpu_temp_avg_c: float | None = None
    gpu_temp_max_c: float | None = None
    pmu_temp_avg_c: float | None = None
    pmu_temp_max_c: float | None = None
    airport_temp_c: float | None = None
    power_supply_temp_c: float | None = None
    trackpad_temp_c: float | None = None
    trackpad_actuator_temp_c: float | None = None
    gpu_die_temp_c: float | None = None
    cpu_peci_temp_c: float | None = None
    cpu_proximity_temp_c: float | None = None
    memory_proximity_temp_c: float | None = None
    pch_die_temp_c: float | None = None
    ssd_temp_c: float | None = None
    temperature_source: str | None = None
    soc_temp_c: float | None = None
    temp_max_c: float | None = None
    temp_max_source: str | None = None
    fan_rpm: float | None = None
    fan_rpms: dict[str, float] = field(default_factory=dict)
    raw_smc_temperatures_c: dict[str, float] = field(default_factory=dict)
    cores: list[CoreMetric] = field(default_factory=list)
    thermal_pressure: str | None = None
    throttled: bool | None = None
    throttle_reasons: list[str] = field(default_factory=list)
    performance_limit_reasons: list[str] = field(default_factory=list)
    memory_bandwidth_gbps: dict[str, float] = field(default_factory=dict)
    raw_keys: int = 0
    warning: str | None = None
