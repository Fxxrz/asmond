#!/usr/bin/env python3
"""
Small macOS power/thermal monitor.

The tool intentionally stays dependency-free. It reads Apple's powermetrics
plist stream and renders a compact curses dashboard.
"""

from __future__ import annotations

import argparse
import ctypes
import curses
import json
import math
import os
import platform
import plistlib
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from asmond_formatting import (
    fmt_bytes,
    fmt_bytes_zero,
    fmt_current,
    fmt_freq,
    fmt_gb_s,
    fmt_minutes,
    fmt_pct,
    fmt_power,
    fmt_rate,
    fmt_temp,
    fmt_voltage,
    fmt_watts,
    interval_step,
    interval_text,
)
from asmond_cpu import (
    CpuLoadSnapshot,
    cpu_usage_from_snapshots,
    read_cpu_load_snapshot,
)
from asmond_metrics_memory import (
    io_stats_from_snapshots,
    parse_number_text as parse_number_text,
    parse_swapusage as parse_swapusage,
    parse_sysctl_size as parse_sysctl_size,
    read_disk_counters as _read_disk_counters,
    read_io_snapshot as _read_io_snapshot,
    read_memory_stats as _read_memory_stats,
    read_network_bytes as _read_network_bytes,
    read_swap_stats as _read_swap_stats,
)
from asmond_models import (
    BatteryStats,
    CableInfo,
    CoreMetric,
    HpmPortInfo,
    IoSnapshot,
    IoStats,
    MemoryStats,
    MetricSample,
    PendingKill,
    ProcessGpuProbeState,
    ProcessInfo,
    SamplerRetryState,
    SideMetricsPollState,
    SideMetricsUpdate,
    UsbCPortStats,
    UsbCStats,
)
from asmond_process import (
    gpu_duration_pct as gpu_duration_pct,
    gpu_pct_from_process_mapping as gpu_pct_from_process_mapping,
    iter_dicts as iter_dicts,
    merge_process_gpu_pcts,
    parse_process_gpu_text_line as parse_process_gpu_text_line,
    parse_process_line as parse_process_line,
    pid_from_mapping as pid_from_mapping,
    process_gpu_pcts_from_plist as process_gpu_pcts_from_plist,
    process_gpu_pcts_from_text as process_gpu_pcts_from_text,
    read_process_gpu_pcts as _read_process_gpu_pcts,
    read_processes as _read_processes,
    sorted_processes,
)
from asmond_process import process_gpu_command as _process_gpu_command
from asmond_powermetrics import (
    INVALID_SAMPLE_WARNING,
    any_present,
    as_mhz as as_mhz,
    as_mw as as_mw,
    as_pct as as_pct,
    as_temp_c as as_temp_c,
    average,
    bandwidth_counters_from_plist as bandwidth_counters_from_plist,
    dict_number,
    find_best as find_best,
    first_non_none,
    flatten,
    max_present,
    normalize_key as normalize_key,
    parse_value as parse_value,
    sample_from_plist,
    string_contains as string_contains,
)
from asmond_settings import SettingsSchema
from asmond_settings import clean_custom_name as _clean_custom_name
from asmond_settings import custom_name_error as _custom_name_error
from asmond_settings import default_settings_path_for as _default_settings_path_for
from asmond_settings import load_settings as _load_settings
from asmond_settings import real_user_home as _real_user_home
from asmond_settings import remove_settings as _remove_settings
from asmond_settings import save_settings as _save_settings
from asmond_smc import AppleSMCTemperatureReader, SMCTemperatureSample
from asmond_system import (
    IOREG_PATH,
    POWERMETRICS_PATH,
    SUDO_PATH,
    SYSTEM_COMMANDS,
    physical_machine,
    system_environment,
)


APP_NAME = "Asmond"
VERSION = "0.6.0"
HOST_MACHINE, _HOST_TRANSLATED = physical_machine()
POWER_SAMPLERS = "cpu_power,gpu_power,ane_power,thermal,battery"
INTEL_POWER_SAMPLERS = "cpu_power,gpu_power,thermal,smc,gpu_agpm_stats"
IOHID_TEMP_TYPE = 15
IOHID_TEMP_FIELD = IOHID_TEMP_TYPE << 16
POWER_MODES = ("soc", "cpu", "gpu", "ane")
LAYOUTS = ("full", "compact", "focus", "custom")
LEGACY_LAYOUTS = {"power-only": "focus", "thermals-only": "focus"}
DETAIL_LEVELS = ("compact", "normal", "detail")
LOAD_VIEWS = ("rows", "graph")
PROCESS_PANEL_MODES = ("hidden", "left", "right")
PROCESS_SORTS = ("cpu", "gpu", "ram", "pid", "name")
CHARGE_PANEL_MODES = ("battery", "usb", "cable")
CUSTOM_PANEL_IDS = ("power", "thermals", "load", "clocks", "ram", "charge", "io", "process")
CUSTOM_SLOT_IDS = ("upper_left", "upper_right", "lower_left", "right_top", "right_middle", "right_lower", "right_bottom")
CUSTOM_SLOT_LABELS = {
    "upper_left": "upper left",
    "upper_right": "upper right",
    "lower_left": "lower left",
    "right_top": "right top",
    "right_middle": "right middle",
    "right_lower": "right lower",
    "right_bottom": "right bottom",
}
TAILOR_MENU_ITEMS = ("panel", "detail", "name")
RESERVED_CUSTOM_NAMES = frozenset((*LAYOUTS, *LEGACY_LAYOUTS))
IO_MODES = ("disk_read", "disk_write", "net_in", "net_out")
IO_MODE_LABELS = {
    "disk_read": "disk read",
    "disk_write": "disk write",
    "net_in": "net in",
    "net_out": "net out",
}
MENU_ITEMS = (
    ("theme", "Theme", "Color palette"),
    ("layout", "Layout", "Dashboard preset"),
    ("interval", "Interval", "Sampler interval"),
    ("show_io", "Disk/Net", "Show compact I/O graph"),
    ("upper_power", "Upper power", "Top power graph source"),
    ("lower_power", "Lower power", "Bottom power graph source"),
    ("upper_io", "Upper I/O", "Top Disk/Net graph source"),
    ("lower_io", "Lower I/O", "Bottom Disk/Net graph source"),
    ("load_view", "Load view", "CPU/GPU avg rows or graph"),
    ("process_panel", "Processes", "Built-in full layout process panel"),
    ("process_sort", "Proc sort", "Process sort key"),
    ("charge_panel", "Charge panel", "Battery, power-input or cable panel"),
    ("allow_root_kill", "Root kill", "Allow process kill when running as root"),
    ("alert_temp", "Temp alert", "High temperature threshold"),
    ("alert_swap", "Swap alert", "Swap-used threshold"),
    ("alert_battery", "Battery alert", "Battery drain threshold"),
)
LOGO_LINES = (
    "  /$$$$$$                                                   /$$",
    " /$$__  $$                                                 | $$",
    "| $$  \\ $$  /$$$$$$$ /$$$$$$/$$$$   /$$$$$$  /$$$$$$$  /$$$$$$$",
    "| $$$$$$$$ /$$_____/| $$_  $$_  $$ /$$__  $$| $$__  $$ /$$__  $$",
    "| $$__  $$|  $$$$$$ | $$ \\ $$ \\ $$| $$  \\ $$| $$  \\ $$| $$  | $$",
    "| $$  | $$ \\____  $$| $$ | $$ | $$| $$  | $$| $$  | $$| $$  | $$",
    "| $$  | $$ /$$$$$$$/| $$ | $$ | $$|  $$$$$$/| $$  | $$|  $$$$$$$",
    "|__/  |__/|_______/ |__/ |__/ |__/ \\______/ |__/  |__/ \\_______/",
)
LOGO_WIDTH = max(len(line) for line in LOGO_LINES)
LOGO_SPLIT = 22
SETTINGS_DIR_ENV = "ASMOND_SETTINGS_DIR"
SETTINGS_FILENAME = "settings.json"
BRAILLE_LEFT_DOTS = (0x40, 0x04, 0x02, 0x01)
BRAILLE_RIGHT_DOTS = (0x80, 0x20, 0x10, 0x08)
HIGH_TEMP_C = 85.0
HIGH_SWAP_BYTES = 1024**3
HIGH_BATTERY_DRAIN_MW = 15000.0
DEFAULT_ALERT_SWAP_GIB = HIGH_SWAP_BYTES / 1024**3
DEFAULT_ALERT_BATTERY_DRAIN_W = HIGH_BATTERY_DRAIN_MW / 1000.0
MIN_RAM_PANEL_H = 9
BATTERY_PANEL_H = 6
CHARGE_PANEL_H = 8
IO_PANEL_H = 7
GRAPH_GRADIENT_COLORS = (46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196)
ROUNDED_BOX_GLYPHS = ("─", "│", "╭", "╮", "╰", "╯")
KILL_CONFIRM_SECONDS = 3.0
MIN_INTERVAL = 0.1
MAX_INTERVAL = 10.0
MEMORY_BATTERY_INTERVAL = 2.0
CHARGE_POLL_INTERVAL = 0.75
CABLE_IDENTITY_REFRESH_INTERVAL = 5.0
AUX_TEMPERATURE_REFRESH_INTERVAL = 0.75
MAX_HPM_PORT_NUMBER = 16
CPU_LOAD_POLL_INTERVAL = 0.5
IO_POLL_INTERVAL = 1.0
PROCESS_POLL_INTERVAL = 2.0
PROCESS_GPU_SAMPLE_MS = 1000
PROCESS_GPU_UNAVAILABLE_BACKOFF = 60.0
SUDO_REFRESH_INTERVAL = 60.0
PROCESS_GPU_PROBE_STATE = ProcessGpuProbeState()


@dataclass
class CableIdentityCache:
    port_signature: tuple[tuple[int, int], ...] = ()
    items: tuple[dict[str, Any], ...] = ()
    refreshed_at: float = 0.0


THEMES = {
    "classic": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_WHITE,
        "muted": curses.COLOR_CYAN,
        "good": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_MAGENTA,
    },
    "mono": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_WHITE,
        "muted": curses.COLOR_WHITE,
        "good": curses.COLOR_WHITE,
        "warn": curses.COLOR_WHITE,
        "bad": curses.COLOR_WHITE,
        "accent": curses.COLOR_WHITE,
    },
    "matrix": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_GREEN,
        "muted": curses.COLOR_CYAN,
        "good": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_GREEN,
    },
    "solar": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_YELLOW,
        "muted": curses.COLOR_CYAN,
        "good": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_BLUE,
    },
    "nord": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_WHITE,
        "muted": curses.COLOR_BLUE,
        "good": curses.COLOR_CYAN,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_MAGENTA,
    },
    "dracula": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_WHITE,
        "muted": curses.COLOR_MAGENTA,
        "good": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_CYAN,
    },
    "ocean": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_WHITE,
        "muted": curses.COLOR_CYAN,
        "good": curses.COLOR_BLUE,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_GREEN,
    },
    "ember": {
        "bg": curses.COLOR_BLACK,
        "fg": curses.COLOR_WHITE,
        "muted": curses.COLOR_YELLOW,
        "good": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "bad": curses.COLOR_RED,
        "accent": curses.COLOR_RED,
    },
}



def settings_schema() -> SettingsSchema:
    return SettingsSchema(
        app_name=APP_NAME,
        settings_dir_env=SETTINGS_DIR_ENV,
        settings_filename=SETTINGS_FILENAME,
        themes=tuple(THEMES),
        layouts=LAYOUTS,
        power_modes=POWER_MODES,
        io_modes=IO_MODES,
        load_views=LOAD_VIEWS,
        process_panel_modes=PROCESS_PANEL_MODES,
        process_sorts=PROCESS_SORTS,
        charge_panel_modes=CHARGE_PANEL_MODES,
        custom_slot_ids=CUSTOM_SLOT_IDS,
        reserved_custom_names=RESERVED_CUSTOM_NAMES,
        min_interval=MIN_INTERVAL,
        max_interval=MAX_INTERVAL,
        high_temp_c=HIGH_TEMP_C,
        default_alert_swap_gib=DEFAULT_ALERT_SWAP_GIB,
        default_alert_battery_drain_w=DEFAULT_ALERT_BATTERY_DRAIN_W,
        normalize_layout=normalize_layout,
        sanitize_custom_layout=sanitize_custom_layout,
    )


def real_user_home() -> Path:
    return _real_user_home()


def default_settings_path() -> Path:
    return _default_settings_path_for(APP_NAME, SETTINGS_DIR_ENV, SETTINGS_FILENAME)


SETTINGS_PATH = default_settings_path()


def load_settings() -> dict[str, Any]:
    return _load_settings(SETTINGS_PATH, settings_schema())


def clean_custom_name(value: Any) -> str:
    return _clean_custom_name(value)


def custom_name_error(value: str) -> str | None:
    return _custom_name_error(value, RESERVED_CUSTOM_NAMES)


def default_panel_for_slot(slot_id: str) -> str:
    template = LAYOUT_TEMPLATES.get("custom")
    if template is not None:
        for slot in template.slots:
            if slot.slot_id == slot_id:
                return slot.default_panel_id
    return "ram"


def default_detail_for_panel(panel_id: str) -> str:
    spec = PANEL_SPECS.get(panel_id)
    return spec.default_detail if spec is not None else "normal"


def detail_levels_for_panel(panel_id: str) -> tuple[str, ...]:
    spec = PANEL_SPECS.get(panel_id)
    if spec is None:
        return ("normal",)
    return spec.detail_levels or (spec.default_detail,)


def custom_layout_label(args: argparse.Namespace) -> str:
    layout = normalize_layout(getattr(args, "layout", "full"))
    if layout != "custom":
        return layout
    name = clean_custom_name(getattr(args, "custom_name", ""))
    return f"custom:{name}" if name else "custom"


def sanitize_custom_layout(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for slot_id, raw_config in value.items():
        if slot_id not in CUSTOM_SLOT_IDS or not isinstance(raw_config, dict):
            continue
        panel_id = raw_config.get("panel")
        detail = raw_config.get("detail")
        if panel_id not in CUSTOM_PANEL_IDS:
            continue
        if detail not in detail_levels_for_panel(panel_id):
            detail = default_detail_for_panel(panel_id)
        cleaned[slot_id] = {"panel": panel_id, "detail": detail}
    return cleaned


def custom_slot_config(custom_layout: dict[str, dict[str, str]], slot_id: str) -> dict[str, str]:
    config = sanitize_custom_layout(custom_layout).get(slot_id, {})
    panel_id = config.get("panel", default_panel_for_slot(slot_id))
    detail = config.get("detail", default_detail_for_panel(panel_id))
    return {"panel": panel_id, "detail": detail}


def custom_layout_effective_has_panel(custom_layout: dict[str, dict[str, str]], panel_id: str) -> bool:
    cleaned = sanitize_custom_layout(custom_layout)
    for slot_id in CUSTOM_SLOT_IDS:
        if slot_id == "right_bottom" and slot_id not in cleaned:
            continue
        if custom_slot_config(cleaned, slot_id)["panel"] == panel_id:
            return True
    return False


def layout_uses_io(args: argparse.Namespace) -> bool:
    layout = normalize_layout(getattr(args, "layout", "full"))
    if layout == "custom":
        return bool(getattr(args, "show_io", False) or custom_layout_effective_has_panel(getattr(args, "custom_layout", {}), "io"))
    return bool(getattr(args, "show_io", False) and layout != "focus")


def layout_uses_process(args: argparse.Namespace, process_panel: str) -> bool:
    layout = normalize_layout(getattr(args, "layout", "full"))
    if layout == "custom":
        return custom_layout_effective_has_panel(getattr(args, "custom_layout", {}), "process")
    return bool(layout == "full" and process_panel != "hidden")


def normalize_layout(value: str) -> str:
    return LEGACY_LAYOUTS.get(value, value)


def layout_arg(value: str) -> str:
    layout = normalize_layout(value)
    if layout not in LAYOUTS:
        raise argparse.ArgumentTypeError(f"choose one of: {', '.join(LAYOUTS)}")
    return layout


def finite_float_arg(value: str) -> float:
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a finite number") from exc
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be a finite number")
    return number


def save_settings(args: argparse.Namespace) -> str | None:
    if is_root_process():
        return "settings are not saved while Asmond runs as root"
    return _save_settings(SETTINGS_PATH, args, settings_schema())


def remove_settings() -> str | None:
    return _remove_settings(SETTINGS_PATH)


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    h: int
    w: int


@dataclass(frozen=True)
class PanelSpec:
    panel_id: str
    title: str
    min_w: int
    min_h: int
    preferred_shape: str
    default_detail: str = "normal"
    detail_levels: tuple[str, ...] = DETAIL_LEVELS


@dataclass(frozen=True)
class LayoutSlot:
    slot_id: str
    default_panel_id: str
    optional: bool = False


@dataclass(frozen=True)
class LayoutTemplate:
    name: str
    description: str
    slots: tuple[LayoutSlot, ...]


PANEL_SPECS: dict[str, PanelSpec] = {
    "power_graph": PanelSpec("power_graph", "Power Graph", 72, 7, "wide", "detail", ("detail",)),
    "power": PanelSpec("power", "Power", 30, 5, "normal", "normal", ("compact", "normal")),
    "thermals": PanelSpec("thermals", "Thermals", 30, 5, "normal", "normal", ("normal",)),
    "load": PanelSpec("load", "CPU / GPU Load", 48, 8, "wide", "detail", ("detail",)),
    "clocks": PanelSpec("clocks", "Clocks", 30, 5, "normal", "normal", ("normal",)),
    "ram": PanelSpec("ram", "RAM", 38, 7, "tall", "detail", ("compact", "detail")),
    # Six outer rows leave four content rows, which is the complete two-column
    # battery view. Only shorter charge panels should auto-select compact detail.
    "charge": PanelSpec("charge", "Battery / Input", 30, 5, "normal", "normal", DETAIL_LEVELS),
    "io": PanelSpec("io", "Disk / Net", 38, 5, "wide", "normal", ("normal",)),
    "process": PanelSpec("process", "Processes", 48, 8, "tall", "detail", ("detail",)),
}


LAYOUT_TEMPLATES: dict[str, LayoutTemplate] = {
    "full": LayoutTemplate(
        "full",
        "wide graph plus two-column dashboard",
        (
            LayoutSlot("top", "power_graph"),
            LayoutSlot("upper_left", "power"),
            LayoutSlot("upper_right", "thermals"),
            LayoutSlot("lower_left", "load"),
            LayoutSlot("right_top", "clocks"),
            LayoutSlot("right_middle", "ram"),
            LayoutSlot("right_lower", "charge", optional=True),
            LayoutSlot("right_bottom", "io", optional=True),
        ),
    ),
    "compact": LayoutTemplate(
        "compact",
        "same information with shorter panels",
        (
            LayoutSlot("top", "power_graph"),
            LayoutSlot("upper_left", "power"),
            LayoutSlot("upper_right", "thermals"),
            LayoutSlot("lower_left", "load"),
            LayoutSlot("right_top", "clocks"),
            LayoutSlot("right_middle", "ram"),
            LayoutSlot("right_lower", "charge", optional=True),
        ),
    ),
    "focus": LayoutTemplate(
        "focus",
        "power, thermals and charging focus",
        (
            LayoutSlot("top", "power_graph"),
            LayoutSlot("upper_left", "power"),
            LayoutSlot("upper_right", "thermals"),
            LayoutSlot("main", "charge"),
        ),
    ),
    "custom": LayoutTemplate(
        "custom",
        "editable full-grid dashboard",
        (
            LayoutSlot("top", "power_graph"),
            LayoutSlot("upper_left", "power"),
            LayoutSlot("upper_right", "thermals"),
            LayoutSlot("lower_left", "load"),
            LayoutSlot("right_top", "clocks"),
            LayoutSlot("right_middle", "ram"),
            LayoutSlot("right_lower", "charge", optional=True),
            LayoutSlot("right_bottom", "io", optional=True),
        ),
    ),
}


@dataclass(frozen=True)
class PanelPlacement:
    panel_id: str
    rect: Rect
    detail: str
    slot: str = ""


@dataclass(frozen=True)
class DashboardLayout:
    left_io_panel: bool = False
    panels: tuple[PanelPlacement, ...] = ()

    def panel(self, panel_id: str, slot: str = "") -> PanelPlacement | None:
        for placement in self.panels:
            if placement.panel_id != panel_id:
                continue
            if slot and placement.slot != slot:
                continue
            return placement
        return None

    def slot(self, slot: str) -> PanelPlacement | None:
        for placement in self.panels:
            if placement.slot == slot:
                return placement
        return None



def bytes_from_pages(pages: int | float, page_size: int) -> int:
    return max(0, int(pages) * page_size)



def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))



def read_ioreg_class_items(class_name: str) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            [IOREG_PATH, "-a", "-r", "-c", class_name],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    try:
        items = plistlib.loads(proc.stdout)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def read_battery_items() -> dict[str, Any] | None:
    items = read_ioreg_class_items("AppleSmartBattery")
    return items[0] if items else None


def read_hpm_port_items() -> list[dict[str, Any]]:
    return read_ioreg_class_items("AppleHPMInterface")


def read_cable_identity_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for class_name in (
        "IOPortTransportComponentCCUSBPDSOPp",
        "IOPortTransportComponentCCUSBPDSOPpp",
    ):
        items.extend(read_ioreg_class_items(class_name))
    return items


def read_usb_port_items() -> list[dict[str, Any]]:
    return read_ioreg_class_items("AppleUSBXHCIPort")


def read_usb_device_items() -> list[dict[str, Any]]:
    return read_ioreg_class_items("IOUSBHostDevice")


def battery_power_from_item(battery: dict[str, Any]) -> float | None:
    telemetry = battery.get("PowerTelemetryData")
    if isinstance(telemetry, dict):
        direct_mw = dict_number(telemetry, "BatteryPower")
        if direct_mw is not None:
            return direct_mw
    amps_ma = dict_number(battery, "InstantAmperage")
    if amps_ma is None:
        amps_ma = dict_number(battery, "Amperage")
    volts_mv = dict_number(battery, "Voltage")
    if volts_mv is None:
        volts_mv = dict_number(battery, "AppleRawBatteryVoltage")
    if amps_ma is None or volts_mv is None:
        return None
    # mA * mV / 1000 = mW. Keep the sign: negative means discharging.
    return amps_ma * volts_mv / 1000.0


def normalize_battery_temp_c(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 1000:
        temp = value / 100.0
    elif value > 150:
        temp = value / 10.0
    else:
        temp = value
    if -40.0 <= temp <= 125.0:
        return temp
    return None


def nested_dict_number(mapping: dict[str, Any], *path: str) -> float | None:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, bool):
        return None
    if isinstance(current, int | float) and math.isfinite(float(current)):
        return float(current)
    return None


def signed_u64_number(value: float | None) -> float | None:
    if value is None:
        return None
    if value >= 2**63:
        return value - 2**64
    return value


def unsigned32(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value) & 0xFFFFFFFF


def bounded_decimal(value: Any, maximum: int) -> int | None:
    text = str(value)
    if (
        not text
        or len(text) > len(str(maximum))
        or re.fullmatch(r"[0-9]+", text) is None
    ):
        return None
    number = int(text)
    return number if 1 <= number <= maximum else None


def battery_temperature_from_item(battery: dict[str, Any]) -> float | None:
    for value in (
        dict_number(battery, "Temperature"),
        dict_number(battery, "VirtualTemperature"),
        nested_dict_number(battery, "AdapterDetails", "AverageBattSkinTemp"),
        nested_dict_number(battery, "AdapterDetails", "AverageBattVirtualTemp"),
        nested_dict_number(battery, "PowerTelemetryData", "AverageTemperature"),
    ):
        temp = normalize_battery_temp_c(value)
        if temp is not None:
            return temp
    return None


def battery_temperature_from_hid_readings(readings: Iterable[tuple[str, float]]) -> float | None:
    temperatures = [
        temperature
        for name, temperature in readings
        if name.casefold().startswith("gas gauge battery")
        and math.isfinite(temperature)
        and 0.0 < temperature < 150.0
    ]
    return average(temperatures) if temperatures else None


def format_pd_value(value: float, unit: str) -> str:
    if abs(value - round(value)) < 0.01:
        return f"{value:.0f}{unit}"
    return f"{value:.1f}{unit}"


def decode_fixed_pdo(raw_value: Any) -> tuple[str, float | None, float | None, float | None]:
    raw = unsigned32(raw_value if isinstance(raw_value, int | float) else None)
    if raw is None or raw == 0:
        return "", None, None, None
    pdo_type = (raw >> 30) & 0x3
    if pdo_type == 0:
        voltage_v = ((raw >> 10) & 0x3FF) * 0.05
        current_a = (raw & 0x3FF) * 0.01
        power_w = voltage_v * current_a
        return f"{format_pd_value(voltage_v, 'V')} {format_pd_value(current_a, 'A')}", voltage_v, current_a, power_w
    if pdo_type == 1:
        min_v = ((raw >> 10) & 0x3FF) * 0.05
        max_v = ((raw >> 20) & 0x3FF) * 0.05
        power_w = (raw & 0x3FF) * 0.25
        return f"{format_pd_value(min_v, 'V')}-{format_pd_value(max_v, 'V')} {format_pd_value(power_w, 'W')}", None, None, power_w
    if pdo_type == 2:
        min_v = ((raw >> 10) & 0x3FF) * 0.05
        max_v = ((raw >> 20) & 0x3FF) * 0.05
        current_a = (raw & 0x3FF) * 0.01
        return f"{format_pd_value(min_v, 'V')}-{format_pd_value(max_v, 'V')} {format_pd_value(current_a, 'A')}", None, current_a, max_v * current_a
    subtype = (raw >> 28) & 0x3
    if subtype == 0:
        max_v = ((raw >> 17) & 0xFF) * 0.1
        min_v = ((raw >> 8) & 0xFF) * 0.1
        current_a = (raw & 0x7F) * 0.05
        power_w = max_v * current_a if max_v and current_a else None
        return f"PPS {format_pd_value(min_v, 'V')}-{format_pd_value(max_v, 'V')} {format_pd_value(current_a, 'A')}", None, current_a, power_w
    return "APDO", None, None, None


def decode_active_contract(rdo_value: Any, pdo_value: Any) -> tuple[float | None, float | None, float | None]:
    rdo = unsigned32(rdo_value if isinstance(rdo_value, int | float) else None)
    pdo = unsigned32(pdo_value if isinstance(pdo_value, int | float) else None)
    if not rdo or not pdo:
        return None, None, None
    pdo_type = (pdo >> 30) & 0x3
    if pdo_type == 0:
        voltage_v = ((pdo >> 10) & 0x3FF) * 0.05
        current_a = ((rdo >> 10) & 0x3FF) * 0.01
        return voltage_v, current_a, voltage_v * current_a
    if pdo_type == 1:
        power_w = ((rdo >> 10) & 0x3FF) * 0.25
        return None, None, power_w
    if pdo_type == 2:
        current_a = ((rdo >> 10) & 0x3FF) * 0.01
        return None, current_a, None
    apdo_subtype = (pdo >> 28) & 0x3
    if apdo_subtype == 0:
        voltage_v = ((rdo >> 9) & 0x7FF) * 0.02
        current_a = (rdo & 0x7F) * 0.05
        return voltage_v, current_a, voltage_v * current_a
    return None, None, None


def pdo_list_from_port(port: dict[str, Any]) -> list[int | None]:
    values = port.get("PortControllerPortPDO")
    pdos: list[int | None] = []
    if isinstance(values, list):
        count = min(7, int(dict_number(port, "PortControllerNPDOs") or len(values)))
        for value in values[: max(0, count)]:
            raw = unsigned32(value if isinstance(value, int | float) else None)
            pdos.append(raw if raw else None)
    if any(pdo is not None for pdo in pdos):
        return pdos
    count = min(7, int(dict_number(port, "PortControllerSrcPdoCount") or 0))
    indexes: Iterable[int] = range(1, max(0, count) + 1)
    if count <= 0:
        found = []
        for key in port:
            match = re.fullmatch(r"PortControllerSrcPdo(\d+)", str(key))
            if match and (index := bounded_decimal(match.group(1), 7)) is not None:
                found.append(index)
        indexes = range(1, max(found, default=0) + 1)
    pdos = []
    for index in indexes:
        raw = unsigned32(dict_number(port, f"PortControllerSrcPdo{index}"))
        pdos.append(raw if raw else None)
    return pdos


def charge_port_label(index: int, total: int) -> str:
    if total >= 3 and index == total - 1:
        return "MagSafe"
    return f"USB-C {index + 1}"


def truthy_iokit_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "active"}
    return False


def clean_hpm_port_label(value: Any, fallback_index: int) -> str:
    text = str(value).strip() if value is not None else ""
    if text.startswith("Port-"):
        text = text[5:]
    if text.startswith("USB-C@"):
        suffix = text.split("@", 1)[1].strip()
        port_number = bounded_decimal(suffix, MAX_HPM_PORT_NUMBER)
        return f"USB-C {port_number}" if port_number is not None else f"Port {fallback_index + 1}"
    if text.startswith("MagSafe"):
        return "MagSafe"
    return text or f"Port {fallback_index + 1}"


def iokit_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def iokit_vdo(value: Any) -> int | None:
    if isinstance(value, bytes | bytearray) and len(value) >= 4:
        return int.from_bytes(value[:4], "little")
    integer = iokit_integer(value)
    return integer & 0xFFFFFFFF if integer is not None else None


def iokit_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def usb_data_link_speeds_by_port(
    usb_port_items: list[dict[str, Any]],
    usb_device_items: list[dict[str, Any]],
) -> dict[tuple[int, int], int]:
    devices_by_location: dict[int, list[int]] = {}
    for item in usb_device_items:
        location = iokit_integer(item.get("locationID"))
        speed_bps = iokit_integer(item.get("UsbLinkSpeed"))
        if location is None or location <= 0 or speed_bps is None or speed_bps <= 0:
            continue
        devices_by_location.setdefault(location, []).append(speed_bps)

    speeds_by_port: dict[tuple[int, int], list[int]] = {}
    for item in usb_port_items:
        usb_io_port = item.get("UsbIOPort")
        if not isinstance(usb_io_port, str):
            continue
        match = re.search(r"/Port-USB-C@([0-9]+)(?:/|$)", usb_io_port)
        location = iokit_integer(item.get("locationID"))
        port_number = (
            bounded_decimal(match.group(1), MAX_HPM_PORT_NUMBER)
            if match is not None
            else None
        )
        if port_number is None or location is None:
            continue
        speeds = devices_by_location.get(location, [])
        if len(speeds) != 1:
            continue
        speeds_by_port.setdefault((2, port_number), []).append(speeds[0])
    return {port: max(speeds) for port, speeds in speeds_by_port.items()}


def usb_data_link_label(active_transports: list[str], speed_bps: int | None) -> str:
    if "CIO" in active_transports:
        return "Thunderbolt"
    if speed_bps is not None and any(name in active_transports for name in ("USB2", "USB3")):
        known_speeds = {
            1_500_000: "USB 1.x · 1.5 Mb/s",
            12_000_000: "USB 1.1 · 12 Mb/s",
            480_000_000: "USB 2.0 · 480 Mb/s",
            5_000_000_000: "USB 3.2 Gen 1 · 5 Gb/s",
            10_000_000_000: "USB 3.2 Gen 2 · 10 Gb/s",
            20_000_000_000: "USB 3.2 Gen 2x2 · 20 Gb/s",
        }
        if speed_bps in known_speeds:
            return known_speeds[speed_bps]
        if speed_bps >= 1_000_000_000:
            return f"USB · {speed_bps / 1_000_000_000:g} Gb/s"
        return f"USB · {speed_bps / 1_000_000:g} Mb/s"
    labels = {"USB2": "USB2", "USB3": "USB3", "USB4": "USB4"}
    active_links = [labels[name] for name in active_transports if name in labels]
    return " + ".join(active_links) if active_links else "none active"


def cable_info_from_item(item: dict[str, Any]) -> CableInfo | None:
    metadata = item.get("Metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw_vdos = metadata.get("VDOs")
    if not isinstance(raw_vdos, list):
        return None
    vdos = [iokit_vdo(value) for value in raw_vdos]
    if len(vdos) <= 3 or any(value is None for value in vdos[:4]):
        return None

    id_header = vdos[0]
    cable_vdo = vdos[3]
    assert id_header is not None and cable_vdo is not None
    product_type = (id_header >> 27) & 0b111
    if product_type not in (3, 4):
        return None
    speed_bits = cable_vdo & 0b111
    current_bits = (cable_vdo >> 5) & 0b11
    max_voltage_bits = (cable_vdo >> 9) & 0b11
    revision = iokit_integer(item.get("Specification Revision"))
    speed_labels = {
        0: "USB 2.0 / 480 Mb/s",
        1: "USB 3.2 / 5 Gb/s",
        2: "USB 3.2 / 10 Gb/s",
    }
    if revision == 3:
        speed_labels.update({3: "USB4 Gen 3", 4: "USB4 Gen 4"})
    speed_label = speed_labels.get(speed_bits, "")
    current_label = {0: "USB default", 1: "3 A", 2: "5 A"}.get(current_bits, "")
    if revision == 2:
        max_voltage_v = 20.0
        maximum_fixed_voltage_v = 20.0
    elif revision == 3:
        max_voltage_v = {0: 20.0, 1: 30.0, 2: 40.0, 3: 50.0}[max_voltage_bits]
        maximum_fixed_voltage_v = {0: 20.0, 1: 28.0, 2: 36.0, 3: 48.0}[max_voltage_bits]
    else:
        max_voltage_v = None
        maximum_fixed_voltage_v = None
    rated_current_a = {1: 3.0, 2: 5.0}.get(current_bits)
    max_power_w = (
        maximum_fixed_voltage_v * rated_current_a
        if maximum_fixed_voltage_v is not None and rated_current_a is not None
        else None
    )
    vendor_id = first_non_none(
        iokit_integer(metadata.get("Vendor ID")),
        iokit_integer(metadata.get("Vendor ID (SOP1)")),
        iokit_integer(item.get("Vendor ID (SOP1)")),
        iokit_integer(item.get("Vendor ID")),
    )
    product_id = first_non_none(
        iokit_integer(metadata.get("Product ID")),
        iokit_integer(metadata.get("Product ID (SOP1)")),
        iokit_integer(item.get("Product ID (SOP1)")),
        iokit_integer(item.get("Product ID")),
    )
    return CableInfo(
        cable_type="active" if product_type == 4 else "passive",
        current_label=current_label,
        max_voltage_v=max_voltage_v,
        max_power_w=max_power_w,
        speed_label=speed_label,
        vendor_id=int(vendor_id) if vendor_id is not None else None,
        product_id=int(product_id) if product_id is not None else None,
        pd_revision={2: "PD 2.0", 3: "PD 3.0"}.get(revision, ""),
    )


def cable_info_by_port(items: list[dict[str, Any]]) -> dict[tuple[int, int], CableInfo]:
    result: dict[tuple[int, int], CableInfo] = {}
    for item in items:
        port_type = first_non_none(
            iokit_integer(item.get("ParentBuiltInPortType")),
            iokit_integer(item.get("ParentPortType")),
        )
        port_number = first_non_none(
            iokit_integer(item.get("ParentBuiltInPortNumber")),
            iokit_integer(item.get("ParentPortNumber")),
            (priority & 0xFF) if (priority := iokit_integer(item.get("Priority"))) is not None else None,
        )
        info = cable_info_from_item(item)
        if port_type is None or port_number is None or info is None:
            continue
        result.setdefault((int(port_type), int(port_number)), info)
    return result


def hpm_ports_from_items(items: list[dict[str, Any]]) -> list[HpmPortInfo]:
    ports: list[HpmPortInfo] = []
    for index, item in enumerate(items):
        raw_label = first_non_none(item.get("PortDescription"), item.get("IORegistryEntryName"))
        label = clean_hpm_port_label(
            raw_label,
            index,
        )
        port_type = str(first_non_none(item.get("PortTypeDescription"), item.get("IOClass")) or "")
        port_type_code = iokit_integer(item.get("PortType"))
        raw_port_number = iokit_integer(item.get("PortNumber"))
        port_number = (
            raw_port_number
            if raw_port_number is not None and 1 <= raw_port_number <= MAX_HPM_PORT_NUMBER
            else None
        )
        label_match = re.fullmatch(r"USB-C ([0-9]+)", label)
        label_number = (
            bounded_decimal(label_match.group(1), MAX_HPM_PORT_NUMBER)
            if label_match
            else None
        )
        raw_label_text = str(raw_label).strip() if raw_label is not None else ""
        raw_usb_match = re.fullmatch(r"(?:Port-)?USB-C@([0-9]+)", raw_label_text)
        identity_invalid = bool(
            (raw_port_number is not None and port_number is None)
            or (label_match is not None and label_number is None)
            or (
                raw_usb_match is not None
                and bounded_decimal(raw_usb_match.group(1), MAX_HPM_PORT_NUMBER) is None
            )
        )
        ports.append(
            HpmPortInfo(
                label=label,
                label_is_explicit=isinstance(raw_label, str) and bool(raw_label.strip()),
                connected=truthy_iokit_value(item.get("ConnectionActive")),
                port_type=port_type,
                port_type_code=port_type_code,
                port_number=port_number,
                active_transports=iokit_string_list(item.get("TransportsActive")),
                identity_invalid=identity_invalid,
            )
        )
    return ports


def hpm_port_physical_label(port: HpmPortInfo) -> str | None:
    if port.identity_invalid:
        return None
    identities: set[str] = set()
    label_match = re.fullmatch(r"USB-C ([0-9]+)", port.label)
    label_number = (
        bounded_decimal(label_match.group(1), MAX_HPM_PORT_NUMBER)
        if label_match
        else None
    )
    if (
        port.label_is_explicit
        and label_number is not None
    ):
        identities.add(f"USB-C {label_number}")
    if (port.label_is_explicit and port.label == "MagSafe") or "magsafe" in port.port_type.casefold():
        identities.add("MagSafe")
    if port.port_type_code == 2 and port.port_number is not None and port.port_number > 0:
        identities.add(f"USB-C {port.port_number}")
    return next(iter(identities)) if len(identities) == 1 else None


def hpm_port_identity_conflicts(port: HpmPortInfo) -> bool:
    if port.identity_invalid:
        return True
    identities: set[str] = set()
    label_match = re.fullmatch(r"USB-C ([0-9]+)", port.label)
    label_number = (
        bounded_decimal(label_match.group(1), MAX_HPM_PORT_NUMBER)
        if label_match
        else None
    )
    if (
        port.label_is_explicit
        and label_number is not None
    ):
        identities.add(f"USB-C {label_number}")
    if (port.label_is_explicit and port.label == "MagSafe") or "magsafe" in port.port_type.casefold():
        identities.add("MagSafe")
    if port.port_type_code == 2 and port.port_number is not None and port.port_number > 0:
        identities.add(f"USB-C {port.port_number}")
    return len(identities) > 1


def hpm_identity_counts(ports: Iterable[HpmPortInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for port in ports:
        if hpm_port_identity_conflicts(port):
            continue
        identity = hpm_port_physical_label(port)
        if identity is not None:
            counts[identity] = counts.get(identity, 0) + 1
    return counts


def hpm_alignment_size(ports: list[HpmPortInfo], controller_slots: int) -> int:
    identities = [
        identity
        for port in ports
        if not hpm_port_identity_conflicts(port)
        and (identity := hpm_port_physical_label(port)) is not None
    ]
    usb_numbers = [
        int(identity.split()[-1])
        for identity in identities
        if identity.startswith("USB-C ")
    ]
    usb_max = max(usb_numbers, default=0)
    has_magsafe = "MagSafe" in identities
    size = max(controller_slots, len(ports), usb_max)
    if has_magsafe:
        size = max(size, usb_max + 1, 1)
    return size


def align_hpm_ports(
    ports: list[HpmPortInfo],
    total_ports: int,
) -> list[HpmPortInfo | None]:
    aligned: list[HpmPortInfo | None] = []
    used: set[int] = set()
    identity_counts = hpm_identity_counts(ports)
    identities = [
        hpm_port_physical_label(port)
        for port in ports
        if not hpm_port_identity_conflicts(port)
    ]
    has_identified_ports = any(identity is not None for identity in identities)
    has_magsafe = "MagSafe" in identities
    for index in range(total_ports):
        expected_label = (
            "MagSafe"
            if has_identified_ports and has_magsafe and index == total_ports - 1
            else f"USB-C {index + 1}"
            if has_identified_ports
            else charge_port_label(index, total_ports)
        )
        match_index = next(
            (
                candidate_index
                for candidate_index, port in enumerate(ports)
                if candidate_index not in used
                and identity_counts.get(expected_label) == 1
                and hpm_port_physical_label(port) == expected_label
            ),
            None,
        )
        if (
            match_index is None
            and index < len(ports)
            and index not in used
            and not hpm_port_identity_conflicts(ports[index])
            and hpm_port_physical_label(ports[index]) is None
        ):
            match_index = index
        if match_index is None:
            aligned.append(None)
            continue
        used.add(match_index)
        aligned.append(ports[match_index])
    return aligned


def usb_c_stats_from_item(
    battery: dict[str, Any],
    hpm_items: list[dict[str, Any]] | None = None,
    cable_items: list[dict[str, Any]] | None = None,
    usb_port_items: list[dict[str, Any]] | None = None,
    usb_device_items: list[dict[str, Any]] | None = None,
) -> UsbCStats:
    telemetry = battery.get("PowerTelemetryData")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    adapter = battery.get("AdapterDetails")
    adapter = adapter if isinstance(adapter, dict) else {}
    port_infos = battery.get("PortControllerInfo")
    port_infos = port_infos if isinstance(port_infos, list) else []
    fed_details = battery.get("FedDetails")
    fed_details = fed_details if isinstance(fed_details, list) else []
    raw_hpm_ports = hpm_ports_from_items(hpm_items or [])
    raw_hpm_identity_counts = hpm_identity_counts(raw_hpm_ports)
    controller_slots = max(len(port_infos), len(fed_details))
    if controller_slots == 0:
        total_ports = len(raw_hpm_ports)
        hpm_ports: list[HpmPortInfo | None] = list(raw_hpm_ports)
    else:
        total_ports = hpm_alignment_size(raw_hpm_ports, controller_slots)
        hpm_ports = align_hpm_ports(raw_hpm_ports, total_ports)
    cable_by_port = cable_info_by_port(cable_items or [])
    link_speed_by_port = usb_data_link_speeds_by_port(usb_port_items or [], usb_device_items or [])
    has_hpm_ports = any(port is not None for port in hpm_ports)
    external_connected = bool(battery.get("ExternalConnected")) if "ExternalConnected" in battery else None
    charging = bool(battery.get("IsCharging")) if "IsCharging" in battery else None

    system_voltage_mv = signed_u64_number(dict_number(telemetry, "SystemVoltageIn"))
    system_current_ma = signed_u64_number(dict_number(telemetry, "SystemCurrentIn"))
    system_power_mw = signed_u64_number(dict_number(telemetry, "SystemPowerIn"))
    adapter_voltage_mv = dict_number(adapter, "AdapterVoltage")
    adapter_current_ma = first_non_none(dict_number(adapter, "Current"), dict_number(adapter, "PMUConfiguration"))
    adapter_voltage_v = adapter_voltage_mv / 1000.0 if adapter_voltage_mv and adapter_voltage_mv > 0 else None
    adapter_current_a = adapter_current_ma / 1000.0 if adapter_current_ma and adapter_current_ma > 0 else None
    adapter_contract_power_w = (
        adapter_voltage_v * adapter_current_a
        if adapter_voltage_v is not None and adapter_current_a is not None
        else None
    )
    adapter_power_w = first_non_none(
        dict_number(adapter, "Watts"),
        dict_number(adapter, "AdapterPower"),
        dict_number(adapter, "Power"),
    )
    if adapter_power_w is not None and adapter_power_w > 1000:
        adapter_power_w /= 1000.0
    adapter_evidence = any(
        value is not None and value > 0
        for value in (
            adapter_voltage_v,
            adapter_current_a,
            adapter_power_w,
            dict_number(adapter, "AdapterID"),
        )
    )
    adapter_name = ""
    for key in ("Description", "Name", "Manufacturer", "SerialString"):
        value = adapter.get(key)
        if isinstance(value, str) and value.strip():
            adapter_name = value.strip()
            break

    best_index_value = dict_number(battery, "BestAdapterIndex")
    best_index = int(best_index_value) if best_index_value is not None else None
    hpm_connected_indices = [
        index for index, port in enumerate(hpm_ports) if port is not None and port.connected
    ]
    fed_active_indices: list[int] = []
    rdo_active_indices: list[int] = []
    fallback_index: int | None = None
    ports: list[UsbCPortStats] = []
    for index in range(total_ports):
        raw_port = port_infos[index] if index < len(port_infos) and isinstance(port_infos[index], dict) else {}
        hpm_port = hpm_ports[index]
        pdos = pdo_list_from_port(raw_port)
        decoded = [decode_fixed_pdo(value) for value in pdos]
        pdo_labels = [label for label, _, _, _ in decoded if label]
        max_power_w = max_present(power for _, _, _, power in decoded)
        rdo = unsigned32(dict_number(raw_port, "PortControllerActiveContractRdo")) or 0
        object_position = (rdo >> 28) & 0x7 if rdo else 0
        selected = pdos[object_position - 1] if 1 <= object_position <= len(pdos) else None
        voltage_v, current_a, power_w = decode_active_contract(rdo, selected)
        fed = fed_details[index] if index < len(fed_details) and isinstance(fed_details[index], dict) else {}
        fed_connected = bool(fed.get("FedExternalConnected")) if isinstance(fed, dict) and "FedExternalConnected" in fed else False
        connected = bool(
            hpm_port.connected
            if hpm_port is not None
            else external_connected is not False and (fed_connected or rdo)
        )
        if not connected:
            voltage_v = None
            current_a = None
            power_w = None
        if fallback_index is None and best_index is not None and external_connected and index == best_index:
            fallback_index = len(ports)
        role = "source/data" if connected else "idle"
        hpm_identity_conflict = bool(
            hpm_port is not None and hpm_port_identity_conflicts(hpm_port)
        )
        hpm_identity = (
            hpm_port_physical_label(hpm_port)
            if hpm_port is not None and not hpm_identity_conflict
            else None
        )
        hpm_identity_ambiguous = bool(
            hpm_identity is not None
            and raw_hpm_identity_counts.get(hpm_identity, 0) != 1
        )
        hpm_identity_conflict = hpm_identity_conflict or hpm_identity_ambiguous
        if hpm_identity_conflict:
            hpm_identity = None
        cable_key = (
            (hpm_port.port_type_code, hpm_port.port_number)
            if hpm_port is not None
            and hpm_port.port_type_code is not None
            and hpm_port.port_number is not None
            and hpm_identity == f"USB-C {hpm_port.port_number}"
            else None
        )
        direct_usb_active = bool(
            connected
            and hpm_port is not None
            and any(name in {"USB2", "USB3"} for name in hpm_port.active_transports)
        )
        port = UsbCPortStats(
            label=(
                f"Port {index + 1}"
                if hpm_identity_conflict
                else hpm_identity or hpm_port.label
                if hpm_port is not None
                else charge_port_label(index, total_ports)
            ),
            connected=connected,
            role=role,
            voltage_v=voltage_v,
            current_a=current_a,
            power_w=power_w,
            max_power_w=max_power_w,
            pdo_labels=pdo_labels,
            cable_info=cable_by_port.get(cable_key) if connected and cable_key is not None else None,
            cable_query_supported=cable_key is not None,
            active_transports=list(hpm_port.active_transports) if hpm_port is not None else [],
            data_link_speed_bps=(
                link_speed_by_port.get(cable_key)
                if direct_usb_active and cable_key is not None
                else None
            ),
        )
        if fed_connected:
            fed_active_indices.append(len(ports))
        if rdo:
            rdo_active_indices.append(len(ports))
        ports.append(port)
    controller_evidence = set(fed_active_indices) | set(rdo_active_indices)
    if has_hpm_ports:
        controller_evidence.intersection_update(hpm_connected_indices)
    input_present = bool(external_connected) or (
        external_connected is not False
        and (bool(controller_evidence) or adapter_evidence)
    )
    charging = normalized_charging_flag(
        charging,
        external_connected if input_present else False,
    )
    active_index = None
    if input_present:
        connected_indices = [index for index, port in enumerate(ports) if port.connected]
        contract_indices = [
            index
            for index in connected_indices
            if index in fed_active_indices or index in rdo_active_indices
        ]
        if len(contract_indices) == 1:
            active_index = contract_indices[0]
        elif fallback_index is not None and fallback_index in connected_indices:
            active_index = fallback_index
        elif not has_hpm_ports:
            unique_evidence = list(dict.fromkeys([*fed_active_indices, *rdo_active_indices]))
            if len(unique_evidence) == 1:
                active_index = unique_evidence[0]

    if active_index is not None and 0 <= active_index < len(ports):
        ports[active_index].role = "sink"

    input_kind = ""
    selected_port = ports[active_index] if active_index is not None and 0 <= active_index < len(ports) else None
    if selected_port is not None:
        input_kind = "magsafe" if selected_port and selected_port.label == "MagSafe" else "usb-c"
    elif not ports and input_present and adapter_evidence:
        input_kind = "magsafe"

    return UsbCStats(
        ports=ports,
        active_index=active_index,
        external_connected=external_connected,
        charging=charging,
        system_voltage_v=system_voltage_mv / 1000.0 if input_present and system_voltage_mv and system_voltage_mv > 0 else None,
        system_current_a=system_current_ma / 1000.0 if input_present and system_current_ma and system_current_ma > 0 else None,
        system_power_w=system_power_mw / 1000.0 if input_present and system_power_mw and system_power_mw > 0 else None,
        adapter_voltage_v=adapter_voltage_v if input_present else None,
        adapter_current_a=adapter_current_a if input_present else None,
        adapter_contract_power_w=adapter_contract_power_w if input_present else None,
        adapter_power_w=adapter_power_w if input_present else None,
        adapter_name=adapter_name if input_present else "",
        input_kind=input_kind,
    )


def valid_percentage(value: float | None) -> float | None:
    if value is None or not 0.0 <= value <= 100.0:
        return None
    return value


def battery_charge_percentage(battery: dict[str, Any]) -> float | None:
    current = dict_number(battery, "CurrentCapacity")
    direct = valid_percentage(current)
    if direct is not None:
        return direct

    battery_data = battery.get("BatteryData")
    battery_data = battery_data if isinstance(battery_data, dict) else {}
    state_of_charge = valid_percentage(dict_number(battery_data, "StateOfCharge"))
    if state_of_charge is not None:
        return state_of_charge

    raw_current = first_non_none(dict_number(battery, "AppleRawCurrentCapacity"), current)
    raw_max = first_non_none(dict_number(battery, "AppleRawMaxCapacity"), dict_number(battery, "MaxCapacity"))
    if raw_current is not None and raw_current >= 0 and raw_max is not None and raw_max > 0:
        return clamp(raw_current / raw_max * 100.0, 0.0, 100.0)
    return None


def usable_battery_capacity(value: float | None) -> float | None:
    return value if value is not None and value > 100 else None


def battery_health_percentage(design_capacity: float | None, full_charge_capacity: float | None) -> float | None:
    if design_capacity is None or design_capacity <= 0 or full_charge_capacity is None:
        return None
    return clamp(full_charge_capacity / design_capacity * 100.0, 0.0, 150.0)


def normalized_charging_flag(
    charging: bool | None,
    external_connected: bool | None,
    *,
    power_mw: float | None = None,
) -> bool | None:
    if power_mw is not None and power_mw < 0:
        return False
    if external_connected is False:
        return False
    return charging


def battery_stats_from_item(battery: dict[str, Any]) -> BatteryStats:
    battery_data = battery.get("BatteryData")
    battery_data = battery_data if isinstance(battery_data, dict) else {}
    raw_max_capacity = dict_number(battery, "AppleRawMaxCapacity")
    design_capacity = first_non_none(
        usable_battery_capacity(dict_number(battery, "DesignCapacity")),
        usable_battery_capacity(dict_number(battery_data, "DesignCapacity")),
    )
    current_capacity = first_non_none(
        dict_number(battery, "CurrentCapacity"),
        dict_number(battery, "AppleRawCurrentCapacity"),
    )
    legacy_capacity = current_capacity is not None and current_capacity > 100
    nominal_capacity = first_non_none(
        usable_battery_capacity(dict_number(battery_data, "NominalChargeCapacity")),
        usable_battery_capacity(dict_number(battery, "NominalChargeCapacity")),
    )
    full_charge_capacity = first_non_none(
        usable_battery_capacity(dict_number(battery_data, "FullChargeCapacity")),
        usable_battery_capacity(dict_number(battery, "FullChargeCapacity")),
    )
    reported_max_capacity = usable_battery_capacity(dict_number(battery, "MaxCapacity"))
    if legacy_capacity:
        max_capacity = first_non_none(
            usable_battery_capacity(raw_max_capacity),
            nominal_capacity,
            full_charge_capacity,
            reported_max_capacity,
        )
    else:
        max_capacity = first_non_none(
            nominal_capacity,
            full_charge_capacity,
            usable_battery_capacity(raw_max_capacity),
            reported_max_capacity,
        )
    health_pct = battery_health_percentage(design_capacity, max_capacity)
    power_mw = battery_power_from_item(battery)
    external_connected = bool(battery.get("ExternalConnected")) if "ExternalConnected" in battery else None
    is_charging = normalized_charging_flag(
        bool(battery.get("IsCharging")) if "IsCharging" in battery else None,
        external_connected,
        power_mw=power_mw,
    )
    time_remaining = dict_number(battery, "TimeRemaining")
    if time_remaining is None:
        time_remaining = dict_number(battery, "AvgTimeToFull" if is_charging else "AvgTimeToEmpty")
    if time_remaining is not None and time_remaining >= 65535:
        time_remaining = None
    return BatteryStats(
        power_mw=power_mw,
        temperature_c=battery_temperature_from_item(battery),
        charge_pct=battery_charge_percentage(battery),
        health_pct=health_pct,
        cycle_count=int(dict_number(battery, "CycleCount") or 0) if dict_number(battery, "CycleCount") is not None else None,
        time_remaining_min=int(time_remaining) if time_remaining is not None else None,
        charging=is_charging,
        external_connected=external_connected,
        design_capacity=int(design_capacity) if design_capacity is not None else None,
        max_capacity=int(max_capacity) if max_capacity is not None else None,
        raw_max_capacity=int(raw_max_capacity) if raw_max_capacity is not None else None,
    )


def cable_identity_items_for_hpm(
    hpm_items: list[dict[str, Any]],
    cache: CableIdentityCache | None = None,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    port_signature = tuple(
        sorted(
            (
                2,
                int(first_non_none(iokit_integer(item.get("PortNumber")), -1)),
            )
            for item in hpm_items
            if iokit_integer(item.get("PortType")) == 2
            and truthy_iokit_value(item.get("ConnectionActive"))
        )
    )
    if not port_signature:
        if cache is not None:
            cache.port_signature = ()
            cache.items = ()
            cache.refreshed_at = 0.0
        return []
    if cache is None:
        return read_cable_identity_items()

    current = time.monotonic() if now is None else now
    if (
        port_signature != cache.port_signature
        or current - cache.refreshed_at >= CABLE_IDENTITY_REFRESH_INTERVAL
    ):
        cache.port_signature = port_signature
        cache.items = tuple(read_cable_identity_items())
        cache.refreshed_at = current
    return list(cache.items)


def read_charge_stats(cable_cache: CableIdentityCache | None = None) -> tuple[BatteryStats, UsbCStats]:
    battery = read_battery_items()
    battery_stats = battery_stats_from_item(battery) if battery is not None else BatteryStats()
    if battery is not None and battery_stats.temperature_c is None:
        battery_stats.temperature_c = HID_TEMPS.read_battery_temperature()
    hpm_items = read_hpm_port_items()
    cable_items = cable_identity_items_for_hpm(hpm_items, cable_cache)
    has_active_usb_data = any(
        truthy_iokit_value(item.get("ConnectionActive"))
        and any(name in {"USB2", "USB3"} for name in iokit_string_list(item.get("TransportsActive")))
        for item in hpm_items
    )
    usb_port_items = read_usb_port_items() if has_active_usb_data else []
    usb_device_items = read_usb_device_items() if has_active_usb_data else []
    return battery_stats, usb_c_stats_from_item(
        battery or {},
        hpm_items,
        cable_items,
        usb_port_items,
        usb_device_items,
    )


def read_swap_stats(stats: MemoryStats) -> None:
    _read_swap_stats(stats, run=subprocess.run)


def read_memory_stats() -> MemoryStats:
    return _read_memory_stats(run=subprocess.run)


def read_disk_counters() -> tuple[int, int]:
    return _read_disk_counters(run=subprocess.run)


def read_network_bytes() -> tuple[int, int]:
    return _read_network_bytes(run=subprocess.run)


def read_io_snapshot() -> IoSnapshot:
    return _read_io_snapshot(run=subprocess.run)


def process_gpu_command(sample_ms: int, output_format: str = "plist") -> list[str]:
    return _process_gpu_command(sample_ms, output_format, needs_sudo=powermetrics_needs_sudo)


def read_process_gpu_pcts(
    sample_ms: int = PROCESS_GPU_SAMPLE_MS,
    probe_state: ProcessGpuProbeState = PROCESS_GPU_PROBE_STATE,
) -> dict[int, float]:
    return _read_process_gpu_pcts(
        sample_ms=sample_ms,
        probe_state=probe_state,
        run=subprocess.run,
        refresh_sudo=refresh_sudo_credentials,
        needs_sudo=powermetrics_needs_sudo,
    )


def read_processes(include_gpu: bool = False, full_command: bool = False) -> list[ProcessInfo]:
    return _read_processes(include_gpu=include_gpu, run=subprocess.run, read_gpu=read_process_gpu_pcts, full_command=full_command)


def read_process_for_kill(pid: int) -> ProcessInfo | None:
    return next((process for process in read_processes(full_command=True) if process.pid == pid), None)


def process_identity_matches(left: ProcessInfo, right: ProcessInfo) -> bool:
    same_executable = (
        left.full_command == right.full_command
        or bool(left.full_command and right.full_command.startswith(f"{left.full_command} "))
        or left.command == right.command
    )
    return (
        left.pid == right.pid
        and left.ppid == right.ppid
        and left.user == right.user
        and same_executable
        and left.start_time == right.start_time
    )


def side_metric_warning(source: str, exc: Exception) -> str:
    return f"{source}:{type(exc).__name__}"


def side_metrics_worker(
    updates: queue.Queue[SideMetricsUpdate],
    stop_event: threading.Event,
    poll_state: SideMetricsPollState,
    mock: bool = False,
) -> None:
    previous_io: IoSnapshot | None = None
    previous_cpu: CpuLoadSnapshot | None = None
    cable_cache = CableIdentityCache()
    next_memory = 0.0
    next_charge = 0.0
    next_cpu = 0.0
    next_io = 0.0
    next_process = 0.0
    last_poll_revision = -1
    while not stop_event.is_set():
        now = time.monotonic()
        poll_io, poll_processes, sample_interval, poll_revision = poll_state.snapshot()
        memory_interval = max(MEMORY_BATTERY_INTERVAL, sample_interval)
        cpu_interval = max(CPU_LOAD_POLL_INTERVAL, sample_interval)
        io_interval = max(IO_POLL_INTERVAL, sample_interval)
        process_interval = max(PROCESS_POLL_INTERVAL, sample_interval)
        if poll_revision != last_poll_revision:
            last_poll_revision = poll_revision
            next_memory = min(next_memory, now)
            next_cpu = min(next_cpu, now)
            if poll_io:
                next_io = min(next_io, now)
            if poll_processes:
                next_process = min(next_process, now)
        if mock:
            if now >= next_memory:
                updates.put(
                    SideMetricsUpdate(
                        memory=mock_memory_stats(now),
                        battery=mock_battery_stats(now),
                        usb_c=mock_usb_c_stats(now),
                    )
                )
                next_memory = now + memory_interval
                next_charge = now + CHARGE_POLL_INTERVAL
            if now >= next_cpu:
                updates.put(SideMetricsUpdate(cpu_usage_pct=max(0.0, min(100.0, 24.0 + 18.0 * math.sin(now / 4.0)))))
                next_cpu = now + cpu_interval
            if poll_io and now >= next_io:
                updates.put(SideMetricsUpdate(io_stats=mock_io_stats(now)))
                next_io = now + io_interval
            if poll_processes and now >= next_process:
                updates.put(SideMetricsUpdate(processes=mock_processes(now)))
                next_process = now + process_interval
            wait_for = [next_memory, next_cpu]
            if poll_io:
                wait_for.append(next_io)
            if poll_processes:
                wait_for.append(next_process)
            stop_event.wait(max(0.05, min(wait_for) - time.monotonic()))
            continue
        if now >= next_memory:
            try:
                updates.put(SideMetricsUpdate(memory=read_memory_stats(), recovered=["vm"]))
            except Exception as exc:
                updates.put(SideMetricsUpdate(warnings=[side_metric_warning("vm", exc)]))
            next_memory = time.monotonic() + memory_interval
        if now >= next_charge:
            try:
                battery, usb_c = read_charge_stats(cable_cache)
                updates.put(SideMetricsUpdate(battery=battery, usb_c=usb_c, recovered=["ioreg"]))
            except Exception as exc:
                updates.put(SideMetricsUpdate(warnings=[side_metric_warning("ioreg", exc)]))
            next_charge = time.monotonic() + CHARGE_POLL_INTERVAL

        now = time.monotonic()
        if now >= next_cpu:
            try:
                current_cpu = read_cpu_load_snapshot()
                cpu_usage = cpu_usage_from_snapshots(previous_cpu, current_cpu)
                previous_cpu = current_cpu
                if cpu_usage is not None:
                    updates.put(SideMetricsUpdate(cpu_usage_pct=cpu_usage, recovered=["cpu"]))
                else:
                    updates.put(SideMetricsUpdate(recovered=["cpu"]))
            except Exception as exc:
                updates.put(SideMetricsUpdate(warnings=[side_metric_warning("cpu", exc)]))
            next_cpu = time.monotonic() + cpu_interval

        now = time.monotonic()
        if poll_io and now >= next_io:
            try:
                current_io = read_io_snapshot()
                updates.put(SideMetricsUpdate(io_stats=io_stats_from_snapshots(previous_io, current_io), recovered=["io"]))
                previous_io = current_io
            except Exception as exc:
                updates.put(SideMetricsUpdate(warnings=[side_metric_warning("io", exc)]))
            next_io = time.monotonic() + io_interval
        elif not poll_io:
            previous_io = None
            next_io = now + io_interval

        now = time.monotonic()
        if poll_processes and now >= next_process:
            try:
                updates.put(SideMetricsUpdate(processes=read_processes(include_gpu=False), recovered=["ps"]))
            except Exception as exc:
                updates.put(SideMetricsUpdate(warnings=[side_metric_warning("ps", exc)]))
            next_process = time.monotonic() + process_interval
        elif not poll_processes:
            next_process = now + process_interval

        due_times = [next_memory, next_charge, next_cpu]
        if poll_io:
            due_times.append(next_io)
        if poll_processes:
            due_times.append(next_process)
        next_due = min(due_times)
        timeout = max(0.05, min(0.5, next_due - time.monotonic()))
        if stop_event.wait(timeout):
            break


def process_gpu_metrics_worker(
    updates: queue.Queue[SideMetricsUpdate],
    stop_event: threading.Event,
    poll_state: SideMetricsPollState,
    mock: bool = False,
) -> None:
    next_poll = 0.0
    last_revision = -1
    while not stop_event.is_set():
        poll_io, poll_processes, sample_interval, revision = poll_state.snapshot()
        del poll_io
        now = time.monotonic()
        if revision != last_revision:
            last_revision = revision
            if poll_processes:
                next_poll = min(next_poll, now)
        if not poll_processes:
            next_poll = now + max(PROCESS_POLL_INTERVAL, sample_interval)
            stop_event.wait(0.25)
            continue
        if now < next_poll:
            stop_event.wait(min(0.25, next_poll - now))
            continue
        try:
            values = {process.pid: process.gpu_pct for process in mock_processes(now) if process.gpu_pct is not None} if mock else read_process_gpu_pcts()
            updates.put(SideMetricsUpdate(process_gpu_pcts=values, recovered=["process-gpu"]))
        except Exception as exc:
            updates.put(SideMetricsUpdate(warnings=[side_metric_warning("process-gpu", exc)]))
        next_poll = time.monotonic() + max(PROCESS_POLL_INTERVAL, sample_interval)


def cycle_value(values: tuple[str, ...], current: str, delta: int) -> str:
    if current not in values:
        return values[0]
    return values[(values.index(current) + delta) % len(values)]


@dataclass(frozen=True)
class HIDTemperatureSample:
    average_c: float | None = None
    maximum_c: float | None = None
    battery_c: float | None = None
    pmu_average_c: float | None = None
    pmu_maximum_c: float | None = None


class AuxiliaryTemperatureCache:
    def __init__(self) -> None:
        self.smc = SMCTemperatureSample()
        self.hid = HIDTemperatureSample()
        self.refreshed_at = 0.0
        self.refreshing = False
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def _refresh(self) -> None:
        try:
            smc = SMC_TEMPS.read()
        except Exception:
            smc = SMCTemperatureSample()
        try:
            hid = HID_TEMPS.read_metrics()
        except Exception:
            hid = HIDTemperatureSample()
        with self.lock:
            self.smc = smc
            self.hid = hid
            self.refreshed_at = time.monotonic()
            self.refreshing = False

    def refresh_async(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self.lock:
            if self.refreshing or (
                self.refreshed_at > 0.0
                and current - self.refreshed_at < AUX_TEMPERATURE_REFRESH_INTERVAL
            ):
                return
            self.refreshing = True
            self.thread = threading.Thread(target=self._refresh, daemon=True)
            thread = self.thread
        thread.start()

    def snapshot(
        self,
        *,
        now: float | None = None,
    ) -> tuple[SMCTemperatureSample, HIDTemperatureSample]:
        self.refresh_async(now=now)
        with self.lock:
            return self.smc, self.hid


class HIDTemperatureReader:
    def __init__(self) -> None:
        self.available = False
        self.client: ctypes.c_void_p | None = None
        self.match: ctypes.c_void_p | None = None
        self.product_key: ctypes.c_void_p | None = None
        self._keepalive: list[ctypes.c_void_p] = []
        self._read_lock = threading.Lock()
        try:
            self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            self._bind()
            self._init_client()
            self.available = bool(self.client and self.product_key)
        except Exception:
            self.available = False

    def _bind(self) -> None:
        c_void_p = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.cf.CFStringCreateWithCString.restype = c_void_p
        self.cf.CFNumberCreate.argtypes = [c_void_p, ctypes.c_int, c_void_p]
        self.cf.CFNumberCreate.restype = c_void_p
        self.cf.CFDictionaryCreate.argtypes = [
            c_void_p,
            ctypes.POINTER(c_void_p),
            ctypes.POINTER(c_void_p),
            ctypes.c_long,
            c_void_p,
            c_void_p,
        ]
        self.cf.CFDictionaryCreate.restype = c_void_p
        self.cf.CFArrayGetCount.argtypes = [c_void_p]
        self.cf.CFArrayGetCount.restype = ctypes.c_long
        self.cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, ctypes.c_long]
        self.cf.CFArrayGetValueAtIndex.restype = c_void_p
        self.cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_int]
        self.cf.CFStringGetCString.restype = ctypes.c_int
        self.cf.CFRelease.argtypes = [c_void_p]
        self.cf.CFRelease.restype = None

        self.iokit.IOHIDEventSystemClientCreate.argtypes = [c_void_p]
        self.iokit.IOHIDEventSystemClientCreate.restype = c_void_p
        self.iokit.IOHIDEventSystemClientSetMatching.argtypes = [c_void_p, c_void_p]
        self.iokit.IOHIDEventSystemClientSetMatching.restype = ctypes.c_int
        self.iokit.IOHIDEventSystemClientCopyServices.argtypes = [c_void_p]
        self.iokit.IOHIDEventSystemClientCopyServices.restype = c_void_p
        self.iokit.IOHIDServiceClientCopyProperty.argtypes = [c_void_p, c_void_p]
        self.iokit.IOHIDServiceClientCopyProperty.restype = c_void_p
        self.iokit.IOHIDServiceClientCopyEvent.argtypes = [
            c_void_p,
            ctypes.c_int64,
            ctypes.c_int32,
            ctypes.c_int64,
        ]
        self.iokit.IOHIDServiceClientCopyEvent.restype = c_void_p
        self.iokit.IOHIDEventGetFloatValue.argtypes = [c_void_p, ctypes.c_int32]
        self.iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double

    def _cfstr(self, value: str) -> ctypes.c_void_p:
        return self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100)

    def _matching(self) -> ctypes.c_void_p:
        keys = (ctypes.c_void_p * 2)()
        values = (ctypes.c_void_p * 2)()
        page = ctypes.c_int32(0xFF00)
        usage = ctypes.c_int32(5)
        keys[0] = self._cfstr("PrimaryUsagePage")
        keys[1] = self._cfstr("PrimaryUsage")
        values[0] = self.cf.CFNumberCreate(None, 3, ctypes.byref(page))
        values[1] = self.cf.CFNumberCreate(None, 3, ctypes.byref(usage))
        match = self.cf.CFDictionaryCreate(None, keys, values, 2, None, None)
        # The dictionary is created without CoreFoundation retain callbacks.
        # Keep keys/values alive for the lifetime of the process; freeing them
        # can later crash inside IOHIDEventSystem.
        self._keepalive.extend(ptr for ptr in (*keys, *values) if ptr)
        return match

    def _init_client(self) -> None:
        self.match = self._matching()
        self.product_key = self._cfstr("Product")
        self.client = self.iokit.IOHIDEventSystemClientCreate(None)
        if self.client and self.match:
            self.iokit.IOHIDEventSystemClientSetMatching(self.client, self.match)

    def read(self) -> tuple[float | None, float | None]:
        sample = self.read_metrics()
        return sample.average_c, sample.maximum_c

    def read_battery_temperature(self) -> float | None:
        return self.read_metrics().battery_c

    def read_all(self) -> tuple[float | None, float | None, float | None]:
        sample = self.read_metrics()
        return sample.average_c, sample.maximum_c, sample.battery_c

    def read_metrics(self) -> HIDTemperatureSample:
        with self._read_lock:
            return self._read_all()

    def _read_all(self) -> HIDTemperatureSample:
        if not self.available:
            return HIDTemperatureSample()
        services = None
        acc_temps: list[float] = []
        die_temps: list[float] = []
        soc_temps: list[float] = []
        readings: list[tuple[str, float]] = []
        try:
            if not self.client or not self.product_key:
                return HIDTemperatureSample()
            services = self.iokit.IOHIDEventSystemClientCopyServices(self.client)
            if not services:
                return HIDTemperatureSample()
            count = int(self.cf.CFArrayGetCount(services))
            for index in range(count):
                service = self.cf.CFArrayGetValueAtIndex(services, index)
                if not service:
                    continue
                name = self._service_name(service, self.product_key)
                event = self.iokit.IOHIDServiceClientCopyEvent(service, IOHID_TEMP_TYPE, 0, 0)
                if not event:
                    continue
                try:
                    temp = float(self.iokit.IOHIDEventGetFloatValue(event, IOHID_TEMP_FIELD))
                finally:
                    self.cf.CFRelease(event)
                if not (0.0 < temp < 150.0):
                    continue
                readings.append((name, temp))
                if name.startswith(("eACC", "pACC")):
                    acc_temps.append(temp)
                elif name.startswith("PMU tdie") or name.startswith("PMU2 tdie"):
                    die_temps.append(temp)
                elif name.startswith("SOC MTR Temp Sensor"):
                    soc_temps.append(temp)
            temps = acc_temps or die_temps or soc_temps
            battery_temp = battery_temperature_from_hid_readings(readings)
            return HIDTemperatureSample(
                average_c=average(temps) if temps else None,
                maximum_c=max(temps) if temps else None,
                battery_c=battery_temp,
                pmu_average_c=average(die_temps) if die_temps else None,
                pmu_maximum_c=max(die_temps) if die_temps else None,
            )
        except Exception:
            return HIDTemperatureSample()
        finally:
            if services:
                try:
                    self.cf.CFRelease(services)
                except Exception:
                    pass

    def _service_name(self, service: ctypes.c_void_p, product_key: ctypes.c_void_p) -> str:
        name_ref = self.iokit.IOHIDServiceClientCopyProperty(service, product_key)
        if not name_ref:
            return ""
        try:
            buf = ctypes.create_string_buffer(256)
            if self.cf.CFStringGetCString(name_ref, buf, len(buf), 0x08000100):
                return buf.value.decode("utf-8", "ignore")
            return ""
        finally:
            self.cf.CFRelease(name_ref)


HID_TEMPS = HIDTemperatureReader()
SMC_TEMPS = AppleSMCTemperatureReader()


def is_root_process() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def powermetrics_needs_sudo() -> bool:
    return os.name == "posix" and not is_root_process()


def refresh_sudo_credentials(prompt: bool) -> tuple[bool, str]:
    if not powermetrics_needs_sudo():
        return True, ""
    if not os.access(SUDO_PATH, os.X_OK):
        return False, f"trusted sudo was not found at {SUDO_PATH}"
    command = [SUDO_PATH, "-v"] if prompt else [SUDO_PATH, "-n", "-v"]
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=not prompt,
            timeout=None if prompt else 8.0,
            env=system_environment(),
        )
    except subprocess.TimeoutExpired:
        return False, "sudo credential refresh timed out"
    except Exception as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    if prompt:
        return False, f"sudo exited with {proc.returncode}"
    stderr = proc.stderr.decode("utf-8", "ignore").strip() if proc.stderr else ""
    return False, stderr or f"sudo exited with {proc.returncode}"


def powermetrics_command(interval_ms: int, sample_count: int | str) -> list[str]:
    machine, _ = physical_machine()
    samplers = INTEL_POWER_SAMPLERS if machine in {"x86_64", "amd64"} else POWER_SAMPLERS
    command = [
        POWERMETRICS_PATH,
        "--samplers",
        samplers,
        "--sample-rate",
        str(interval_ms),
        "--sample-count",
        str(sample_count),
        "--format",
        "plist",
        "--buffer-size",
        "1",
        "--poweravg",
        "1",
        "--show-plimits",
        "--show-extra-power-info",
        "--handle-invalid-values",
    ]
    if powermetrics_needs_sudo():
        return [SUDO_PATH, "-n", *command]
    return command


def ensure_powermetrics_access(args: argparse.Namespace) -> None:
    if args.mock or os.name != "posix" or is_root_process():
        return
    print_console(f"{APP_NAME} keeps the UI unprivileged and asks sudo only for powermetrics.", file=sys.stderr)
    ok, error = refresh_sudo_credentials(prompt=True)
    if not ok:
        print_console(f"{APP_NAME} could not get sudo access for powermetrics: {error}", file=sys.stderr)
        sys.exit(1)


def ensure_ui_not_root(args: argparse.Namespace) -> None:
    if args.mock or args.command in {"probe", "doctor", "report"} or not is_root_process() or getattr(args, "allow_root_ui", False):
        return
    print_console(
        f"{APP_NAME} refuses to run the full terminal UI as root.\n"
        "Run `asmond` normally; only powermetrics will be started with sudo.\n"
        f"If you really want the whole UI as root, pass --allow-root-ui.",
        file=sys.stderr,
    )
    sys.exit(1)


class SudoKeeper:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if not powermetrics_needs_sudo():
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            ok, error = refresh_sudo_credentials(prompt=False)
            self.last_error = "" if ok else error
            if self.stop_event.wait(SUDO_REFRESH_INTERVAL):
                break

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.2)


def physical_temperature_candidates(sample: MetricSample) -> list[tuple[float | None, str]]:
    cpu_source = f"CPU@{sample.cpu_temp_max_label}" if sample.cpu_temp_max_label else "CPU"
    return [
        (sample.cpu_temp_max_c, cpu_source),
        (sample.gpu_temp_max_c, "GPU"),
        (sample.pmu_temp_max_c, "PMU"),
        (sample.power_supply_temp_c, "power supply"),
        (sample.airport_temp_c, "airport"),
        (sample.trackpad_temp_c, "trackpad"),
        (sample.trackpad_actuator_temp_c, "actuator"),
    ]


def hottest_named_temperature(candidates: Iterable[tuple[float | None, str]]) -> tuple[float | None, str | None]:
    present = [(value, source) for value, source in candidates if value is not None and math.isfinite(value)]
    if not present:
        return None, None
    return max(present, key=lambda item: item[0])


def auxiliary_temperature_samples(
    cache: AuxiliaryTemperatureCache | None = None,
    *,
    now: float | None = None,
) -> tuple[SMCTemperatureSample, HIDTemperatureSample]:
    if cache is None:
        return SMC_TEMPS.read(), HID_TEMPS.read_metrics()
    return cache.snapshot(now=now)


def apply_hid_temperatures(
    sample: MetricSample,
    cache: AuxiliaryTemperatureCache | None = None,
    *,
    now: float | None = None,
) -> None:
    existing_temp_max = sample.temp_max_c
    existing_soc_temp = sample.soc_temp_c
    existing_temp_source = sample.temp_max_source or sample.temperature_source
    existing_pmu_avg = sample.pmu_temp_avg_c
    existing_pmu_max = sample.pmu_temp_max_c
    if existing_temp_source is None and (existing_temp_max is not None or existing_soc_temp is not None):
        existing_temp_source = "powermetrics"
    smc, hid = auxiliary_temperature_samples(cache, now=now)
    sample.cpu_temp_avg_c = first_non_none(sample.cpu_temp_avg_c, smc.cpu_avg_c)
    sample.cpu_temp_max_c = first_non_none(sample.cpu_temp_max_c, smc.cpu_max_c)
    sample.cpu_temp_max_label = sample.cpu_temp_max_label or smc.cpu_max_label
    sample.gpu_temp_avg_c = first_non_none(sample.gpu_temp_avg_c, smc.gpu_avg_c)
    sample.gpu_temp_max_c = first_non_none(sample.gpu_temp_max_c, smc.gpu_max_c)
    sample.airport_temp_c = first_non_none(sample.airport_temp_c, smc.airport_proximity_c)
    sample.power_supply_temp_c = first_non_none(sample.power_supply_temp_c, smc.power_supply_proximity_c)
    sample.trackpad_temp_c = first_non_none(sample.trackpad_temp_c, smc.trackpad_c)
    sample.trackpad_actuator_temp_c = first_non_none(sample.trackpad_actuator_temp_c, smc.trackpad_actuator_c)
    sample.pmu_temp_avg_c = first_non_none(sample.pmu_temp_avg_c, hid.pmu_average_c)
    sample.pmu_temp_max_c = first_non_none(sample.pmu_temp_max_c, hid.pmu_maximum_c)
    hid_pmu_used = (
        existing_pmu_avg is None and hid.pmu_average_c is not None
    ) or (
        existing_pmu_max is None and hid.pmu_maximum_c is not None
    )
    smc_present = any(
        value is not None
        for value in (
            smc.cpu_avg_c,
            smc.cpu_max_c,
            smc.gpu_avg_c,
            smc.gpu_max_c,
            smc.airport_proximity_c,
            smc.power_supply_proximity_c,
            smc.trackpad_c,
            smc.trackpad_actuator_c,
        )
    )
    hid_present = (
        hid.average_c is not None
        or hid.maximum_c is not None
        or hid_pmu_used
    )
    if smc.cpu_avg_c is not None:
        sample.temperature_source = "AppleSMC + IOHID" if hid_pmu_used else "AppleSMC"
        # Keep the legacy aggregate field aligned with the verified CPU group.
        # It is retained for history/alert compatibility and is not labelled SoC.
        sample.soc_temp_c = sample.cpu_temp_avg_c
    else:
        sample.soc_temp_c = first_non_none(sample.soc_temp_c, hid.average_c)
        source_parts: list[str] = []
        if smc_present:
            source_parts.append("AppleSMC")
        if hid_present:
            source_parts.append("IOHID")
        if existing_temp_source and existing_temp_source not in source_parts:
            source_parts.append(existing_temp_source)
        if source_parts:
            sample.temperature_source = " + ".join(source_parts)
    physical_candidates = physical_temperature_candidates(sample)
    if smc.cpu_avg_c is not None:
        sample.temp_max_c, sample.temp_max_source = hottest_named_temperature(
            [*physical_candidates, (sample.cpu_temp_avg_c, "CPU")]
        )
    else:
        maximum_candidates = [
            *physical_candidates,
            (hid.maximum_c, "IOHID"),
            (existing_temp_max, existing_temp_source or "powermetrics"),
            (
                sample.soc_temp_c,
                existing_temp_source if existing_soc_temp is not None else "IOHID",
            ),
        ]
        sample.temp_max_c, sample.temp_max_source = hottest_named_temperature(
            maximum_candidates
        )


class PowerMetricsStream:
    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self.proc: subprocess.Popen[bytes] | None = None
        self.stop_event = threading.Event()
        self.stderr_chunks: deque[str] = deque(maxlen=8)
        self.stderr_thread: threading.Thread | None = None
        self.temperature_cache = AuxiliaryTemperatureCache()
        self.process_lock = threading.Lock()

    def command(self) -> list[str]:
        return powermetrics_command(self.interval_ms, "-1")

    def drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            for chunk in iter(lambda: self.proc.stderr.readline(), b""):
                text = chunk.decode("utf-8", "ignore").strip()
                if text:
                    self.stderr_chunks.append(text)
                if self.stop_event.is_set():
                    break
        except Exception:
            return

    def terminate_process(self, proc: subprocess.Popen[bytes]) -> None:
        with self.process_lock:
            if proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

    def samples(self) -> Iterable[MetricSample]:
        ok, error = refresh_sudo_credentials(prompt=False)
        if not ok:
            yield MetricSample(warning=f"sudo credential unavailable for powermetrics: {error}")
            return
        if self.stop_event.is_set():
            return
        machine, _translated = physical_machine()
        if machine in {"arm64", "aarch64"}:
            self.temperature_cache.refresh_async()
        proc = subprocess.Popen(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=system_environment(),
        )
        with self.process_lock:
            self.proc = proc
        if self.stop_event.is_set():
            self.terminate_process(proc)
            return
        self.stderr_thread = threading.Thread(target=self.drain_stderr, daemon=True)
        self.stderr_thread.start()
        assert self.proc.stdout is not None
        buffer = b""
        while not self.stop_event.is_set():
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\0" in buffer:
                raw, buffer = buffer.split(b"\0", 1)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = plistlib.loads(raw)
                    if isinstance(obj, dict):
                        sample = sample_from_plist(obj, interval_s=self.interval_ms / 1000.0)
                        if powermetrics_sample_is_usable(sample) and (
                            not is_intel_sample(sample)
                            or sample.soc_temp_c is None
                            or sample.temp_max_c is None
                        ):
                            apply_hid_temperatures(sample, self.temperature_cache)
                        yield sample
                except Exception as exc:
                    yield MetricSample(warning=f"plist parse failed: {exc}")
        if not self.stop_event.is_set():
            return_code = self.proc.poll() if self.proc is not None else None
            detail = "\n".join(self.stderr_chunks) if self.stderr_chunks else f"powermetrics stream ended (exit {return_code})"
            yield MetricSample(warning=detail)

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc:
            self.terminate_process(self.proc)
        if self.stderr_thread and self.stderr_thread.is_alive():
            self.stderr_thread.join(timeout=0.2)


class MockStream:
    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self.stop_event = threading.Event()
        self.t = 0.0

    def samples(self) -> Iterable[MetricSample]:
        while not self.stop_event.is_set():
            self.t += self.interval_ms / 1000.0
            cores = []
            for idx in range(4):
                cores.append(
                    CoreMetric(
                        label=f"P{idx}",
                        usage_pct=clamp(35 + 35 * math.sin(self.t / 2 + idx), 0, 100),
                        freq_mhz=2600 + 600 * max(0, math.sin(self.t / 2 + idx)),
                    )
                )
            for idx in range(4, 10):
                cores.append(
                    CoreMetric(
                        label=f"E{idx}",
                        usage_pct=clamp(18 + 18 * math.cos(self.t / 3 + idx), 0, 100),
                        freq_mhz=1100 + 300 * max(0, math.cos(self.t / 3 + idx)),
                    )
                )
            yield MetricSample(
                cpu_power_mw=900 + 650 * (math.sin(self.t / 2) + 1) + random.random() * 100,
                gpu_power_mw=250 + 500 * max(0.0, math.sin(self.t / 3)),
                ane_power_mw=60 if int(self.t) % 9 else 350,
                p_usage_pct=clamp(45 + 35 * math.sin(self.t / 2), 0, 100),
                e_usage_pct=clamp(25 + 20 * math.cos(self.t / 3), 0, 100),
                gpu_usage_pct=clamp(15 + 55 * max(0, math.sin(self.t / 4)), 0, 100),
                ane_usage_pct=1 if int(self.t) % 9 else 4,
                p_freq_mhz=2400 + 800 * max(0, math.sin(self.t / 2)),
                e_freq_mhz=1100 + 500 * max(0, math.cos(self.t / 3)),
                gpu_freq_mhz=300 + 800 * max(0, math.sin(self.t / 4)),
                soc_temp_c=42 + 16 * max(0, math.sin(self.t / 5)),
                temp_max_c=46 + 18 * max(0, math.sin(self.t / 5)),
                cores=cores,
                thermal_pressure="Nominal",
                throttled=False,
                memory_bandwidth_gbps={
                    "CPU": 18.0 + 10.0 * max(0, math.sin(self.t / 2)),
                    "GPU": 9.0 + 18.0 * max(0, math.sin(self.t / 4)),
                    "ANE": 0.4 if int(self.t) % 9 else 4.8,
                    "DRAM": 30.0 + 20.0 * max(0, math.sin(self.t / 3)),
                },
                raw_keys=42,
            )
            time.sleep(self.interval_ms / 1000.0)

    def stop(self) -> None:
        self.stop_event.set()


def mock_memory_stats(t: float) -> MemoryStats:
    total = 24 * 1024**3
    used_pct = 42.0 + 8.0 * max(0.0, math.sin(t / 8.0))
    pressure_pct = 18.0 + 10.0 * max(0.0, math.sin(t / 5.0))
    active = int(total * used_pct / 100.0 * 0.72)
    wired = int(total * used_pct / 100.0 * 0.18)
    compressed = int(total * 0.07)
    cached = int(total * 0.28)
    free = max(0, total - active - wired - compressed - cached)
    return MemoryStats(
        total_bytes=total,
        available_bytes=free + cached,
        cached_bytes=cached,
        free_bytes=free,
        active_bytes=active,
        wired_bytes=wired,
        compressed_bytes=compressed,
        swap_total_bytes=2 * 1024**3,
        swap_used_bytes=0,
        swap_free_bytes=2 * 1024**3,
        system_free_pct=100.0 - pressure_pct,
    )


def mock_battery_stats(t: float) -> BatteryStats:
    return BatteryStats(
        power_mw=28500.0 + 2500.0 * math.sin(t / 6.0),
        temperature_c=30.0 + 1.5 * max(0.0, math.sin(t / 12.0)),
        charge_pct=64.0,
        health_pct=96.0,
        cycle_count=31,
        time_remaining_min=72,
        charging=True,
        external_connected=True,
        design_capacity=4382,
        max_capacity=4208,
        raw_max_capacity=4208,
    )


def mock_usb_c_stats(t: float) -> UsbCStats:
    power = 32.0 + 2.5 * math.sin(t / 5.0)
    port = UsbCPortStats(
        label="MagSafe",
        connected=True,
        role="sink",
        voltage_v=19.7,
        current_a=power / 19.7,
        power_w=power,
        max_power_w=35.0,
        pdo_labels=["5V 3A", "9V 3A", "15V 2.3A", "20V 1.7A"],
    )
    return UsbCStats(
        ports=[
            UsbCPortStats(
                label="USB-C 1",
                connected=True,
                role="source/data",
                cable_query_supported=True,
                active_transports=["CC", "USB3"],
                data_link_speed_bps=10_000_000_000,
                cable_info=CableInfo(
                    cable_type="passive",
                    current_label="5 A",
                    max_voltage_v=50.0,
                    max_power_w=240.0,
                    speed_label="USB4 Gen 3",
                    vendor_id=0x05AC,
                    product_id=0x1234,
                    pd_revision="PD 3.0",
                ),
            ),
            UsbCPortStats(label="USB-C 2", cable_query_supported=True),
            port,
        ],
        active_index=2,
        external_connected=True,
        charging=True,
        system_voltage_v=19.7,
        system_current_a=power / 19.7,
        system_power_w=power,
        adapter_voltage_v=20.0,
        adapter_current_a=1.7,
        adapter_contract_power_w=34.0,
        adapter_power_w=35.0,
        adapter_name="Demo MagSafe Power Adapter",
        input_kind="magsafe",
    )


def mock_io_stats(t: float) -> IoStats:
    return IoStats(
        disk_read_bps=40_000 + 30_000 * max(0.0, math.sin(t / 3.0)),
        disk_write_bps=1_200_000 + 900_000 * max(0.0, math.sin(t / 5.0)),
        net_in_bps=80_000 + 70_000 * max(0.0, math.cos(t / 4.0)),
        net_out_bps=35_000 + 30_000 * max(0.0, math.sin(t / 6.0)),
    )


def mock_processes(t: float) -> list[ProcessInfo]:
    names = ("python3", "WindowServer", "Safari", "kernel_task", "llama-runner")
    processes: list[ProcessInfo] = []
    for idx, name in enumerate(names, start=1):
        cpu = max(0.0, 7.0 + 8.0 * math.sin(t / (idx + 1) + idx))
        mem = max(0.1, 1.0 + 1.5 * math.cos(t / (idx + 2) + idx))
        processes.append(
            ProcessInfo(
                pid=1000 + idx,
                cpu_pct=cpu,
                mem_pct=mem,
                rss_kib=int((160 + idx * 120) * 1024),
                command=name,
                ppid=1,
                etime=f"0{idx}:2{idx}",
                full_command=f"/usr/bin/{name}",
                user="demo",
                gpu_pct=max(0.0, 4.0 + 10.0 * math.sin(t / (idx + 3) + idx)),
            )
        )
    return processes


class History:
    def __init__(self, length: int) -> None:
        self.length = length
        self.soc_power = deque(maxlen=length)
        self.cpu_power = deque(maxlen=length)
        self.gpu_power = deque(maxlen=length)
        self.ane_power = deque(maxlen=length)
        self.power_times = deque(maxlen=length)
        self.cpu_usage = deque(maxlen=length)
        self.gpu_usage = deque(maxlen=length)
        self.ane_usage = deque(maxlen=length)
        self.core_usage: dict[str, deque] = {}
        self.temp = deque(maxlen=length)
        self.disk_read_io = deque(maxlen=length)
        self.disk_write_io = deque(maxlen=length)
        self.net_in_io = deque(maxlen=length)
        self.net_out_io = deque(maxlen=length)

    def add(self, sample: MetricSample, now: float | None = None) -> None:
        cpu_usage = sample.cpu_usage_pct
        if cpu_usage is None and sample.p_usage_pct is not None and sample.e_usage_pct is not None:
            cpu_usage = (sample.p_usage_pct + sample.e_usage_pct) / 2.0
        self.soc_power.append(effective_total_power_mw(sample))
        self.cpu_power.append(sample.cpu_power_mw)
        self.gpu_power.append(sample.gpu_power_mw)
        self.ane_power.append(sample.ane_power_mw)
        self.power_times.append(time.monotonic() if now is None else now)
        self.cpu_usage.append(cpu_usage)
        self.gpu_usage.append(sample.gpu_usage_pct)
        self.ane_usage.append(sample.ane_usage_pct)
        for core in sample.cores:
            if core.label not in self.core_usage:
                self.core_usage[core.label] = deque(maxlen=self.length)
            self.core_usage[core.label].append(core.usage_pct)
        self.temp.append(sample.soc_temp_c)

    def update_latest_cpu_usage(self, cpu_usage: float | None) -> None:
        if self.cpu_usage:
            self.cpu_usage[-1] = cpu_usage

    def add_io(self, io_stats: IoStats) -> None:
        self.disk_read_io.append(io_stats.disk_read_bps)
        self.disk_write_io.append(io_stats.disk_write_bps)
        self.net_in_io.append(io_stats.net_in_bps)
        self.net_out_io.append(io_stats.net_out_bps)

    def clear_power(self) -> None:
        self.soc_power.clear()
        self.cpu_power.clear()
        self.gpu_power.clear()
        self.ane_power.clear()
        self.power_times.clear()

    def resize(self, length: int) -> None:
        if length <= self.length:
            return
        self.length = length
        self.soc_power = deque(self.soc_power, maxlen=length)
        self.cpu_power = deque(self.cpu_power, maxlen=length)
        self.gpu_power = deque(self.gpu_power, maxlen=length)
        self.ane_power = deque(self.ane_power, maxlen=length)
        self.power_times = deque(self.power_times, maxlen=length)
        self.cpu_usage = deque(self.cpu_usage, maxlen=length)
        self.gpu_usage = deque(self.gpu_usage, maxlen=length)
        self.ane_usage = deque(self.ane_usage, maxlen=length)
        self.core_usage = {label: deque(values, maxlen=length) for label, values in self.core_usage.items()}
        self.temp = deque(self.temp, maxlen=length)
        self.disk_read_io = deque(self.disk_read_io, maxlen=length)
        self.disk_write_io = deque(self.disk_write_io, maxlen=length)
        self.net_in_io = deque(self.net_in_io, maxlen=length)
        self.net_out_io = deque(self.net_out_io, maxlen=length)


def effective_total_power_mw(sample: MetricSample) -> float | None:
    if sample.soc_power_mw is not None:
        return sample.soc_power_mw
    parts = [sample.cpu_power_mw, sample.gpu_power_mw, sample.ane_power_mw, sample.media_power_mw]
    if any(part is not None for part in parts):
        return sum(part or 0 for part in parts)
    return None


def selected_power_history(history: History, mode: str) -> deque:
    if mode == "soc":
        return history.soc_power
    if mode == "gpu":
        return history.gpu_power
    if mode == "ane":
        return history.ane_power
    return history.cpu_power


def selected_power_value(sample: MetricSample, mode: str) -> float | None:
    if mode == "soc":
        return effective_total_power_mw(sample)
    if mode == "gpu":
        return sample.gpu_power_mw
    if mode == "ane":
        return sample.ane_power_mw
    return sample.cpu_power_mw


def is_intel_sample(sample: MetricSample | None = None) -> bool:
    if sample is not None and sample.telemetry_source is not None:
        return sample.telemetry_source == "intel"
    return HOST_MACHINE in {"x86_64", "amd64"}


def total_power_label(sample: MetricSample | None = None) -> str:
    return "Package" if is_intel_sample(sample) else "SoC"


def selected_power_label(mode: str, sample: MetricSample | None = None) -> str:
    return {"soc": total_power_label(sample), "cpu": "CPU", "gpu": "GPU", "ane": "ANE/NPU"}.get(mode, "CPU")


def finite_tail(values: Iterable[float | None], count: int | None = None) -> list[float]:
    raw = list(values)
    if count is not None:
        raw = raw[-max(1, count):]
    return [float(value) for value in raw if value is not None and math.isfinite(float(value))]


def avg_power(values: Iterable[float | None], count: int) -> float | None:
    present = finite_tail(values, count)
    if not present:
        return None
    return sum(present) / len(present)


def avg_power_window(
    history: History,
    values: Iterable[float | None],
    window_s: float = 30.0,
    now: float | None = None,
) -> float | None:
    current = time.monotonic() if now is None else now
    cutoff = current - max(0.0, window_s)
    present = [
        float(value)
        for timestamp, value in zip(history.power_times, values)
        if timestamp >= cutoff and value is not None and math.isfinite(float(value))
    ]
    return sum(present) / len(present) if present else None


def peak_power(values: Iterable[float | None]) -> float | None:
    present = finite_tail(values)
    if not present:
        return None
    return max(present)


def power_history_for_row(history: History, mode: str) -> deque:
    return selected_power_history(history, mode)


def power_row_supported(sample: MetricSample, history: History, mode: str) -> bool:
    if selected_power_value(sample, mode) is not None:
        return True
    return any(value is not None for value in power_history_for_row(history, mode))


def supported_power_modes(sample: MetricSample, history: History) -> tuple[str, ...]:
    return tuple(mode for mode in POWER_MODES if power_row_supported(sample, history, mode))


def power_graph_live_label(sample: MetricSample, history: History) -> str:
    parts: list[str] = []
    for mode in supported_power_modes(sample, history):
        label = selected_power_label(mode, sample)
        parts.append(f"{label} {fmt_power(selected_power_value(sample, mode))}")
    return "  ".join(parts)


def battery_supported(battery: BatteryStats) -> bool:
    return any_present(
        battery.power_mw,
        battery.temperature_c,
        battery.charge_pct,
        battery.health_pct,
        battery.cycle_count,
        battery.time_remaining_min,
        battery.charging,
        battery.external_connected,
        battery.design_capacity,
        battery.max_capacity,
        battery.raw_max_capacity,
    )


def usb_c_supported(usb_c: UsbCStats) -> bool:
    return bool(usb_c.ports) or any_present(
        usb_c.active_index,
        usb_c.system_voltage_v,
        usb_c.system_current_a,
        usb_c.system_power_w,
        usb_c.adapter_voltage_v,
        usb_c.adapter_current_a,
        usb_c.adapter_contract_power_w,
        usb_c.adapter_power_w,
    ) or bool(usb_c.adapter_name or usb_c.input_kind)


def charge_input_label(usb_c: UsbCStats) -> str:
    if usb_c.input_kind == "magsafe":
        return "MagSafe"
    if usb_c.input_kind == "ac":
        return "AC Input"
    return "USB-C"


def charge_input_active(usb_c: UsbCStats) -> bool:
    if usb_c.external_connected is False:
        return False
    active = usb_c.active_port
    if active and active.connected:
        return True
    return usb_c.external_connected is True and bool(usb_c.input_kind)


def cable_panel_supported(usb_c: UsbCStats) -> bool:
    return any(port.cable_query_supported for port in usb_c.ports)


def selected_cable_port(usb_c: UsbCStats) -> UsbCPortStats | None:
    candidates = [port for port in usb_c.ports if port.cable_query_supported]
    if not candidates:
        return None
    active = usb_c.active_port
    if active is not None and active in candidates and active.connected:
        return active
    return (
        next((port for port in candidates if port.connected and port.cable_info is not None), None)
        or next((port for port in candidates if port.connected), None)
        or candidates[0]
    )


def effective_charge_panel(requested: str, battery: BatteryStats, usb_c: UsbCStats) -> str:
    if requested == "battery" and not battery_supported(battery) and usb_c_supported(usb_c):
        return "usb"
    if requested == "usb" and not usb_c_supported(usb_c) and battery_supported(battery):
        return "battery"
    if requested == "cable" and not cable_panel_supported(usb_c):
        if usb_c_supported(usb_c):
            return "usb"
        if battery_supported(battery):
            return "battery"
    return requested if requested in CHARGE_PANEL_MODES else "battery"


def keep_last_nonzero_frequencies(sample: MetricSample, cache: dict[str, float]) -> None:
    for attr in ("p_freq_mhz", "e_freq_mhz", "gpu_freq_mhz"):
        value = getattr(sample, attr)
        if value is not None and value > 0:
            cache[attr] = value
        elif attr in cache:
            setattr(sample, attr, cache[attr])


def metric_sample_has_telemetry(sample: MetricSample) -> bool:
    numeric_values = (
        sample.cpu_power_mw,
        sample.gpu_power_mw,
        sample.ane_power_mw,
        sample.media_power_mw,
        sample.soc_power_mw,
        sample.battery_power_mw,
        sample.p_usage_pct,
        sample.e_usage_pct,
        sample.cpu_usage_pct,
        sample.gpu_usage_pct,
        sample.ane_usage_pct,
        sample.media_usage_pct,
        sample.p_freq_mhz,
        sample.e_freq_mhz,
        sample.gpu_freq_mhz,
        sample.cpu_temp_avg_c,
        sample.cpu_temp_max_c,
        sample.gpu_temp_avg_c,
        sample.gpu_temp_max_c,
        sample.pmu_temp_avg_c,
        sample.pmu_temp_max_c,
        sample.airport_temp_c,
        sample.power_supply_temp_c,
        sample.trackpad_temp_c,
        sample.trackpad_actuator_temp_c,
        sample.soc_temp_c,
        sample.temp_max_c,
        sample.fan_rpm,
    )
    return (
        any_present(*numeric_values)
        or bool(sample.cores)
        or bool(sample.memory_bandwidth_gbps)
        or sample.thermal_pressure is not None
        or sample.throttled is not None
        or bool(sample.throttle_reasons)
        or bool(sample.performance_limit_reasons)
    )


def powermetrics_sample_is_usable(sample: MetricSample) -> bool:
    return sample.warning != INVALID_SAMPLE_WARNING and metric_sample_has_telemetry(sample)



def safe_addstr(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        encoding = getattr(win, "encoding", None) or getattr(sys.stdout, "encoding", None)
        text = encoding_safe_text(text, encoding)
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y or x >= max_x:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: max(0, max_x - x - 1)]
        if text:
            win.addstr(y, x, text, attr)
    except (curses.error, UnicodeError):
        pass


def sanitize_terminal_text(value: Any) -> str:
    return "".join(
        char if unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"} else "?"
        for char in str(value)
    )


def encoding_safe_text(value: Any, encoding: str | None) -> str:
    text = sanitize_terminal_text(value)
    if not encoding:
        return text
    try:
        text.encode(encoding)
        return text
    except (LookupError, UnicodeEncodeError):
        ascii_fallback = (
            text.replace("°C", " C")
            .replace("°", "")
            .replace("·", "-")
            .replace("–", "-")
            .replace("—", "-")
        )
        try:
            return ascii_fallback.encode(encoding, errors="replace").decode(encoding)
        except LookupError:
            return ascii_fallback.encode("ascii", errors="replace").decode("ascii")


def print_console(value: Any = "", *, file: Any = None) -> None:
    stream = sys.stdout if file is None else file
    print(encoding_safe_text(value, getattr(stream, "encoding", None)), file=stream)


def draw_unicode_box_border(win: curses.window, y: int, x: int, h: int, w: int, attr: int) -> bool:
    encoding = getattr(win, "encoding", None)
    if isinstance(encoding, str):
        try:
            "".join(ROUNDED_BOX_GLYPHS).encode(encoding)
        except (LookupError, UnicodeError):
            return False
    try:
        win.attron(attr)
        win.addstr(y, x, f"╭{'─' * (w - 2)}╮", attr)
        for row in range(y + 1, y + h - 1):
            win.addstr(row, x, "│", attr)
            win.addstr(row, x + w - 1, "│", attr)
        win.addstr(y + h - 1, x, f"╰{'─' * (w - 2)}╯", attr)
        win.attroff(attr)
    except (curses.error, UnicodeError):
        try:
            win.attroff(attr)
        except curses.error:
            pass
        return False
    return True


def draw_acs_box_border(win: curses.window, y: int, x: int, h: int, w: int, attr: int) -> None:
    try:
        win.attron(attr)
        win.hline(y, x + 1, curses.ACS_HLINE, w - 2)
        win.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
        win.vline(y + 1, x, curses.ACS_VLINE, h - 2)
        win.vline(y + 1, x + w - 1, curses.ACS_VLINE, h - 2)
        win.addch(y, x, curses.ACS_ULCORNER)
        win.addch(y, x + w - 1, curses.ACS_URCORNER)
        win.addch(y + h - 1, x, curses.ACS_LLCORNER)
        win.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
        win.attroff(attr)
    except curses.error:
        pass


def draw_box(win: curses.window, y: int, x: int, h: int, w: int, title: str, attr: int) -> None:
    if h < 3 or w < 8:
        return
    if not draw_unicode_box_border(win, y, x, h, w, attr):
        draw_acs_box_border(win, y, x, h, w, attr)
    safe_addstr(win, y, x + 2, f" {title} ", attr)


def draw_hotkey_text(
    win: curses.window,
    y: int,
    x: int,
    text: str,
    hotkey: str,
    base_attr: int,
    hotkey_attr: int,
) -> int:
    idx = text.lower().find(hotkey.lower())
    if idx < 0:
        safe_addstr(win, y, x, text, base_attr)
        return x + len(text)
    safe_addstr(win, y, x, text[:idx], base_attr)
    safe_addstr(win, y, x + idx, text[idx : idx + 1], hotkey_attr)
    safe_addstr(win, y, x + idx + 1, text[idx + 1 :], base_attr)
    return x + len(text)


def draw_label_hotkey(
    win: curses.window,
    y: int,
    x: int,
    text: str,
    hotkey: str,
    colors: dict[str, int],
    *,
    selected: bool = False,
) -> int:
    base_attr = colors["warn"] | curses.A_BOLD if selected else colors["muted"]
    key_attr = colors["warn"] | curses.A_BOLD
    return draw_hotkey_text(win, y, x, text, hotkey, base_attr, key_attr)


def draw_box_hotkey_title(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    title: str,
    attr: int,
    colors: dict[str, int],
    hotkey: str,
) -> None:
    draw_box(win, y, x, h, w, title, attr)
    draw_hotkey_text(win, y, x + 3, title, hotkey, attr, colors["warn"] | curses.A_BOLD)


def fill_rect(win: curses.window, y: int, x: int, h: int, w: int, attr: int = 0) -> None:
    for row in range(max(0, h)):
        safe_addstr(win, y + row, x, " " * max(0, w), attr)


def draw_logo(win: curses.window, y: int, x: int, w: int, colors: dict[str, int]) -> int:
    if w < LOGO_WIDTH + 2:
        safe_addstr(win, y, x + max(0, (w - len(APP_NAME)) // 2), APP_NAME, colors["accent"] | curses.A_BOLD)
        return 1
    left_attr = colors["accent"] | curses.A_BOLD
    right_attr = colors["muted"] | curses.A_BOLD
    for idx, line in enumerate(LOGO_LINES):
        xx = x + max(0, (w - len(line)) // 2)
        safe_addstr(win, y + idx, xx, line[:LOGO_SPLIT], left_attr)
        safe_addstr(win, y + idx, xx + LOGO_SPLIT, line[LOGO_SPLIT:], right_attr)
    return len(LOGO_LINES)


def draw_app_name(win: curses.window, y: int, x: int, colors: dict[str, int]) -> int:
    safe_addstr(win, y, x, APP_NAME[:2], colors["accent"] | curses.A_BOLD)
    safe_addstr(win, y, x + 2, APP_NAME[2:], colors["muted"] | curses.A_BOLD)
    return x + len(APP_NAME)


def draw_header(win: curses.window, args: argparse.Namespace, colors: dict[str, int]) -> None:
    _, max_x = win.getmaxyx()
    x = draw_app_name(win, 0, 1, colors)
    prefix = f" {VERSION}  {interval_text(args.interval)}  {args.theme}  {custom_layout_label(args)}  q quit  m menu  ? "
    safe_addstr(win, 0, x, prefix[: max(0, max_x - x - 1)], colors["bold"])
    x += len(prefix)
    if x < max_x - 1:
        draw_hotkey_text(win, 0, x, "help", "h", colors["bold"], colors["warn"] | curses.A_BOLD)


def draw_hotkey_box(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    title: str,
    attr: int,
    colors: dict[str, int],
    hotkey: str,
) -> None:
    draw_box_hotkey_title(win, y, x, h, w, title, attr, colors, hotkey)


def draw_usage_sparkline(
    win: curses.window,
    y: int,
    x: int,
    w: int,
    values: Iterable[float | None],
    colors: dict[str, int],
) -> None:
    vals = list(values)[-(w * 2) :]
    if len(vals) < w * 2:
        vals = [None] * (w * 2 - len(vals)) + vals
    for idx in range(w):
        pair = vals[idx * 2 : idx * 2 + 2]
        mask = 0
        strongest = 0.0
        for value, dot_masks in zip(pair, (BRAILLE_LEFT_DOTS, BRAILLE_RIGHT_DOTS), strict=False):
            if value is None:
                level = 0
            else:
                strongest = max(strongest, value)
                if value >= 90:
                    level = 4
                elif value >= 60:
                    level = 3
                elif value >= 30:
                    level = 2
                elif value > 0:
                    level = 1
                else:
                    level = 0
            if level <= 0:
                mask |= dot_masks[0]
            else:
                for dot in dot_masks[:level]:
                    mask |= dot
        attr = graph_gradient_attr(
            strongest / 100.0 if strongest > 0 else None,
            colors,
            warn_at=0.60,
            bad_at=0.90,
        )
        safe_addstr(win, y, x + idx, chr(0x2800 + mask), attr)


def tail_values(values: Iterable[float | None], width: int) -> list[float | None]:
    vals = list(values)[-width:]
    if len(vals) < width:
        vals = [None] * (width - len(vals)) + vals
    return vals


def scaled_columns(
    values: Iterable[float | None],
    width: int,
    height: int,
    max_value: float | None = None,
) -> list[int | None]:
    vals = tail_values(values, width)
    present = [value for value in vals if value is not None]
    if not present or height <= 0:
        return [None] * width
    scale = max(max_value or max(present), 1.0)
    cols: list[int | None] = []
    for value in vals:
        if value is None:
            cols.append(None)
        elif value <= 0:
            cols.append(0)
        else:
            cols.append(max(1, int(round(clamp(value, 0.0, scale) / scale * height))))
    return cols


def draw_power_mode_legend(
    win: curses.window,
    y: int,
    center_x: int,
    upper_mode: str,
    lower_mode: str,
    modes: tuple[str, ...],
    sample: MetricSample,
    colors: dict[str, int],
) -> None:
    total_label = total_power_label(sample)
    total_upper_label = total_label if "s" in total_label.casefold() else f"S {total_label}"
    total_lower_label = total_label.casefold() if "s" in total_label.casefold() else f"s {total_label}"
    all_labels = (
        (total_upper_label, total_lower_label, "S", "s", "soc"),
        ("CPU", "cpu", "C", "c", "cpu"),
        ("GPU", "gpu", "G", "g", "gpu"),
        ("ANE", "ane", "A", "a", "ane"),
    )
    labels = tuple(label for label in all_labels if label[4] in modes)
    if not labels:
        return
    total_width = len("upper ") + sum(len(label) + 1 for label, _, _, _, _ in labels) + len("u cycle  /  lower ")
    total_width += sum(len(label) + 1 for _, label, _, _, _ in labels) + len("n cycle")
    x = max(1, center_x - total_width // 2)
    safe_addstr(win, y, x, "upper ", colors["fg"])
    x += len("upper ")
    for upper_label, _, upper_key, _, mode in labels:
        x = draw_label_hotkey(win, y, x, upper_label, upper_key, colors, selected=mode == upper_mode)
        safe_addstr(win, y, x, " ", colors["fg"])
        x += 1
    safe_addstr(win, y, x, "u cycle  /  lower ", colors["fg"])
    x += len("u cycle  /  lower ")
    for _, lower_label, _, lower_key, mode in labels:
        x = draw_label_hotkey(win, y, x, lower_label, lower_key, colors, selected=mode == lower_mode)
        safe_addstr(win, y, x, " ", colors["fg"])
        x += 1
    safe_addstr(win, y, x, "n cycle", colors["fg"])


def graph_gradient_attr(
    ratio: float | None,
    colors: dict[str, int],
    *,
    warn_at: float,
    bad_at: float,
) -> int:
    if ratio is None or ratio <= 0:
        return colors["dim"]
    normalized = clamp(ratio, 0.0, 1.0)
    gradient_index = int(round(normalized * (len(GRAPH_GRADIENT_COLORS) - 1)))
    gradient_attr = colors.get(f"graph_{gradient_index}")
    if gradient_attr is not None:
        return gradient_attr
    if normalized >= bad_at:
        return colors["bad"]
    if normalized >= warn_at:
        return colors["warn"]
    return colors["good"]


def power_graph_attr(value: float | None, scale: float, colors: dict[str, int]) -> int:
    ratio = value / scale if value is not None and value > 0 and scale > 0 else None
    return graph_gradient_attr(ratio, colors, warn_at=0.55, bad_at=0.85)


def draw_split_power_graph(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    history: History,
    sample: MetricSample,
    upper_mode: str,
    lower_mode: str,
    colors: dict[str, int],
) -> None:
    if h < 7 or w < 30:
        return
    draw_box(win, y, x, h, w, "POWER GRAPH", colors["accent"])
    inner_x = x + 2
    inner_w = max(1, w - 4)
    graph_y = y + 1
    graph_h = h - 2
    baseline = graph_y + graph_h // 2
    upper_h = max(1, baseline - graph_y)
    lower_h = max(1, graph_y + graph_h - baseline - 1)

    upper_values = list(selected_power_history(history, upper_mode))
    lower_values = list(selected_power_history(history, lower_mode))
    present = [value for value in (*upper_values, *lower_values) if value is not None]
    shared_scale = max(max(present), 1.0) if present else 1.0

    safe_addstr(win, baseline, inner_x, "─" * inner_w, colors["dim"])
    dense_w = inner_w * 2
    upper_tail = tail_values(upper_values, dense_w)
    lower_tail = tail_values(lower_values, dense_w)
    upper_cols = scaled_columns(upper_values, dense_w, upper_h, shared_scale)
    lower_cols = scaled_columns(lower_values, dense_w, lower_h, shared_scale)

    for col in range(inner_w):
        left_idx = col * 2
        right_idx = left_idx + 1
        pair_values = [upper_tail[left_idx], upper_tail[right_idx]]
        attr_value = max((value or 0.0) for value in pair_values)
        attr = power_graph_attr(attr_value, shared_scale, colors)
        for step in range(upper_h):
            mask = 0
            left_amount = upper_cols[left_idx]
            right_amount = upper_cols[right_idx]
            if left_amount is not None and left_amount > step:
                mask |= sum(BRAILLE_LEFT_DOTS)
            if right_amount is not None and right_amount > step:
                mask |= sum(BRAILLE_RIGHT_DOTS)
            if mask:
                safe_addstr(win, baseline - 1 - step, inner_x + col, chr(0x2800 + mask), attr)
    for col in range(inner_w):
        left_idx = col * 2
        right_idx = left_idx + 1
        pair_values = [lower_tail[left_idx], lower_tail[right_idx]]
        attr_value = max((value or 0.0) for value in pair_values)
        attr = power_graph_attr(attr_value, shared_scale, colors)
        for step in range(lower_h):
            mask = 0
            left_amount = lower_cols[left_idx]
            right_amount = lower_cols[right_idx]
            if left_amount is not None and left_amount > step:
                mask |= sum(BRAILLE_LEFT_DOTS)
            if right_amount is not None and right_amount > step:
                mask |= sum(BRAILLE_RIGHT_DOTS)
            if mask:
                safe_addstr(win, baseline + 1 + step, inner_x + col, chr(0x2800 + mask), attr)

    upper_value = selected_power_value(sample, upper_mode)
    lower_value = selected_power_value(sample, lower_mode)
    live_label = power_graph_live_label(sample, history)
    if len(live_label) < w - 18:
        safe_addstr(win, y, x + w - len(live_label) - 3, f" {live_label} ", colors["fg"])
    else:
        top_label = f"Upper {selected_power_label(upper_mode, sample)} {fmt_power(upper_value)}"
        bottom_label = f"Lower {selected_power_label(lower_mode, sample)} {fmt_power(lower_value)}"
        safe_addstr(win, y, x + w - len(top_label) - len(bottom_label) - 8, f" {top_label} ", colors["good"])
        safe_addstr(win, y, x + w - len(bottom_label) - 3, f" {bottom_label} ", colors["accent"])
    scale_label = f"scale {fmt_power(shared_scale)}"
    safe_addstr(win, y + h - 1, x + max(2, w - len(scale_label) - 3), f" {scale_label} ", colors["muted"])
    draw_power_mode_legend(win, baseline, x + w // 2, upper_mode, lower_mode, supported_power_modes(sample, history), sample, colors)


def color_for_thermal(sample: MetricSample, colors: dict[str, int]) -> int:
    if sample.throttled:
        return colors["bad"]
    pressure = (sample.thermal_pressure or "").lower()
    if any(word in pressure for word in ("serious", "critical", "heavy")):
        return colors["bad"]
    if any(word in pressure for word in ("fair", "moderate", "warn")):
        return colors["warn"]
    return colors["good"]


def draw_usage_row(
    win: curses.window,
    y: int,
    x: int,
    w: int,
    label: str,
    values: Iterable[float | None],
    current: float | None,
    colors: dict[str, int],
) -> None:
    if w < 14:
        return
    pct_text = fmt_pct(current).strip()
    label_w = min(7, max(3, len(label)))
    pct_w = 5
    gap = 2
    spark_w = w - label_w - pct_w - (gap * 3)
    if spark_w < 3:
        spark_w = max(1, w - label_w - pct_w - 2)
    label_text = label[:label_w]
    pct_x = x + w - pct_w
    spark_x = x + label_w + gap
    spark_w = max(1, pct_x - spark_x - gap)
    safe_addstr(win, y, x, label_text, colors["muted"])
    draw_usage_sparkline(win, y, spark_x, spark_w, values, colors)
    safe_addstr(
        win,
        y,
        pct_x + max(0, pct_w - len(pct_text)),
        pct_text,
        colors["good"] if current is not None else colors["muted"],
    )


def current_cpu_usage(sample: MetricSample) -> float | None:
    if sample.cpu_usage_pct is not None:
        return sample.cpu_usage_pct
    if sample.p_usage_pct is not None and sample.e_usage_pct is not None:
        return (sample.p_usage_pct + sample.e_usage_pct) / 2.0
    if sample.p_usage_pct is not None:
        return sample.p_usage_pct
    return sample.e_usage_pct


def apply_system_cpu_usage(sample: MetricSample, cpu_usage_pct: float | None) -> bool:
    if cpu_usage_pct is None:
        return False
    if is_intel_sample(sample):
        sample.cpu_usage_pct = cpu_usage_pct
        sample.cpu_usage_source = "mach"
        return True
    return False


def load_graph_attr(value: float | None, colors: dict[str, int]) -> int:
    ratio = value / 100.0 if value is not None and value > 0 else None
    return graph_gradient_attr(ratio, colors, warn_at=0.60, bad_at=0.90)


def draw_dense_columns(
    win: curses.window,
    baseline: int,
    x: int,
    h: int,
    values: Iterable[float | None],
    width: int,
    scale: float,
    colors: dict[str, int],
    *,
    direction: int,
) -> None:
    dense_w = width * 2
    tails = tail_values(values, dense_w)
    cols = scaled_columns(values, dense_w, h, scale)
    for col in range(width):
        left_idx = col * 2
        right_idx = left_idx + 1
        attr_value = max((tails[left_idx] or 0.0), (tails[right_idx] or 0.0))
        attr = load_graph_attr(attr_value, colors)
        for step in range(h):
            mask = 0
            left_amount = cols[left_idx]
            right_amount = cols[right_idx]
            if left_amount is not None and left_amount > step:
                mask |= sum(BRAILLE_LEFT_DOTS)
            if right_amount is not None and right_amount > step:
                mask |= sum(BRAILLE_RIGHT_DOTS)
            if not mask:
                continue
            yy = baseline - 1 - step if direction < 0 else baseline + 1 + step
            safe_addstr(win, yy, x + col, chr(0x2800 + mask), attr)


def draw_avg_load_graph(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    sample: MetricSample,
    history: History,
    colors: dict[str, int],
) -> None:
    if h < 5 or w < 24:
        return
    baseline = y + h // 2
    upper_h = max(1, baseline - y)
    lower_h = max(1, y + h - baseline - 1)
    safe_addstr(win, baseline, x, "─" * w, colors["dim"])
    draw_dense_columns(win, baseline, x, upper_h, history.cpu_usage, w, 100.0, colors, direction=-1)
    draw_dense_columns(win, baseline, x, lower_h, history.gpu_usage, w, 100.0, colors, direction=1)

    cpu_value = current_cpu_usage(sample)
    gpu_value = sample.gpu_usage_pct
    live_label = f"CPU avg {fmt_pct(cpu_value).strip()}  GPU avg {fmt_pct(gpu_value).strip()}"
    if len(live_label) < w:
        safe_addstr(win, y, x + max(0, w - len(live_label)), live_label, colors["fg"])
    legend = "CPU avg / GPU avg"
    if len(legend) < w:
        safe_addstr(win, baseline, x + max(0, w // 2 - len(legend) // 2), f" {legend} ", colors["fg"])


def draw_memory_bar(
    win: curses.window,
    y: int,
    x: int,
    w: int,
    value: float | None,
    colors: dict[str, int],
    severity: float | None = None,
) -> None:
    if w <= 0:
        return
    if value is None:
        safe_addstr(win, y, x, "." * w, colors["dim"])
        return
    filled = int(round(clamp(value, 0, 100) / 100.0 * w))
    color_value = value if severity is None else severity
    attr = colors["good"]
    if color_value >= 90:
        attr = colors["bad"]
    elif color_value >= 70:
        attr = colors["warn"]
    safe_addstr(win, y, x, "#" * filled, attr)
    safe_addstr(win, y, x + filled, "." * (w - filled), colors["dim"])


def memory_pressure_attr(value: float | None, colors: dict[str, int]) -> int:
    if value is None:
        return colors["dim"]
    if value >= 85:
        return colors["bad"]
    if value >= 65:
        return colors["warn"]
    return colors["good"]


def memory_bandwidth_rows(sample: MetricSample | None) -> list[tuple[str, str]]:
    if sample is None or not sample.memory_bandwidth_gbps:
        return []
    priority = ("DRAM", "CPU", "GPU", "ANE", "Media", "DCS")
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label in priority:
        if label in sample.memory_bandwidth_gbps:
            rows.append((f"BW {label}", fmt_gb_s(sample.memory_bandwidth_gbps[label])))
            seen.add(label)
    for label, value in sorted(sample.memory_bandwidth_gbps.items()):
        if label not in seen:
            rows.append((f"BW {label}", fmt_gb_s(value)))
    return rows


def clock_panel_rows(sample: MetricSample) -> list[tuple[str, str]]:
    has_intel_core_rows = bool(sample.cores) and all(core.label.startswith("C") for core in sample.cores)
    if is_intel_sample(sample) or (has_intel_core_rows and sample.e_freq_mhz is None):
        core_freqs = [core.freq_mhz for core in sample.cores if core.freq_mhz is not None]
        cpu_freq_mhz = sample.p_freq_mhz
        if cpu_freq_mhz is None and core_freqs:
            cpu_freq_mhz = sum(core_freqs) / len(core_freqs)
        rows = [("CPU", fmt_freq(cpu_freq_mhz))]
    else:
        rows = [
            ("P cores", fmt_freq(sample.p_freq_mhz)),
            ("E cores", fmt_freq(sample.e_freq_mhz)),
        ]
    rows.extend(
        [
            ("GPU", fmt_freq(sample.gpu_freq_mhz)),
            ("Raw keys", str(sample.raw_keys or "n/a")),
        ]
    )
    return rows


def draw_memory_detail_rows(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    rows: list[tuple[str, str, int, bool]],
    colors: dict[str, int],
) -> None:
    for idx, (name, text, value, good_when_present) in enumerate(rows[:h]):
        yy = y + idx
        value_w = min(14, max(7, len(text)))
        safe_addstr(win, yy, x, name, colors["muted"])
        safe_addstr(
            win,
            yy,
            x + max(8, w - value_w),
            f"{text:>{value_w}}",
            colors["good"] if value and good_when_present else colors["warn"] if value else colors["muted"],
        )


def draw_bandwidth_rows(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    rows: list[tuple[str, str]],
    colors: dict[str, int],
) -> int:
    drawn = 0
    for idx, (name, text) in enumerate(rows[:h]):
        yy = y + idx
        safe_addstr(win, yy, x, name[: max(1, min(8, w // 2 - 1))], colors["muted"])
        safe_addstr(win, yy, x + max(9, w - min(10, max(7, len(text)))), text[: max(1, w - 9)], colors["fg"])
        drawn += 1
    return drawn


def draw_memory_pressure_meter(
    win: curses.window,
    y: int,
    x: int,
    w: int,
    value: float | None,
    colors: dict[str, int],
) -> None:
    if w <= 0:
        return
    if value is None:
        safe_addstr(win, y, x, "·" * w, colors["dim"])
        return
    filled = int(round(clamp(value, 0.0, 100.0) / 100.0 * w))
    for col in range(w):
        pct_at_col = (col + 1) / max(1, w) * 100.0
        if col < filled:
            char = "━"
            if pct_at_col >= 85:
                attr = colors["bad"]
            elif pct_at_col >= 65:
                attr = colors["warn"]
            else:
                attr = colors["good"]
        else:
            char = "·"
            attr = colors["dim"]
        safe_addstr(win, y, x + col, char, attr)


def memory_detail_value_rows(memory: MemoryStats) -> list[tuple[str, str, int, bool]]:
    swap_text = (
        f"{fmt_bytes_zero(memory.swap_used_bytes)}/{fmt_bytes_zero(memory.swap_total_bytes)}"
        if memory.swap_total_bytes > 0
        else "0 B"
    )
    return [
        ("Phys", fmt_bytes(memory.physical_used_bytes), memory.physical_used_bytes, True),
        ("Swap", swap_text, memory.swap_used_bytes, False),
        ("Free", fmt_bytes(memory.free_bytes), memory.free_bytes, True),
        ("Cache", fmt_bytes(memory.cached_bytes), memory.cached_bytes, True),
        ("Wired", fmt_bytes(memory.wired_bytes), memory.wired_bytes, True),
        ("Compr", fmt_bytes(memory.compressed_bytes), memory.compressed_bytes, True),
        ("Reclaim", fmt_bytes(memory.available_bytes), memory.available_bytes, True),
        ("Active", fmt_bytes(memory.active_bytes), memory.active_bytes, True),
    ]


def draw_memory_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    memory: MemoryStats,
    sample: MetricSample | None,
    colors: dict[str, int],
    detail: str = "detail",
) -> None:
    if h < 7 or w < 30:
        return
    safe_addstr(win, y, x, "RAM phys", colors["accent"] | curses.A_BOLD)
    total_text = fmt_bytes(memory.total_bytes)
    value_w = min(12, max(8, len(total_text)))
    safe_addstr(win, y, x + max(4, w - value_w), f"{total_text:>{value_w}}", colors["fg"])

    usage_y = y + 1
    used_pct = memory.used_pct
    pressure_pct = memory.pressure_pct
    pct_text = "n/a" if used_pct is None else f"{used_pct:.0f}%"
    safe_addstr(win, usage_y, x, "Used", colors["muted"])
    safe_addstr(win, usage_y, x + 5, pct_text, colors["good"] if used_pct is not None else colors["muted"])
    used_text = fmt_bytes(memory.used_bytes) if memory.total_bytes else "n/a"
    value_w = min(12, max(8, len(used_text)))
    bar_x = x + 10
    bar_w = max(4, w - 11 - value_w)
    draw_memory_bar(win, usage_y, bar_x, bar_w, used_pct, colors, pressure_pct)
    safe_addstr(win, usage_y, x + w - value_w, f"{used_text:>{value_w}}", colors["fg"])

    pressure_label = "Pressure" if memory.system_free_pct is not None else "Reclaim"
    pressure_text = "n/a" if pressure_pct is None else f"{pressure_pct:.0f}%"
    free_text = "" if memory.system_free_pct is None else f" free {memory.system_free_pct:.0f}%"
    if h >= 11:
        safe_addstr(win, y + 2, x, pressure_label, colors["muted"])
        safe_addstr(win, y + 2, x + 10, pressure_text, memory_pressure_attr(pressure_pct, colors))
        if free_text:
            safe_addstr(win, y + 2, x + 16, free_text, colors["fg"])
        draw_memory_pressure_meter(win, y + 3, x, w, pressure_pct, colors)
        rows_start = y + 5
    elif h >= 9:
        safe_addstr(win, y + 2, x, pressure_label[:8], colors["muted"])
        safe_addstr(win, y + 2, x + 10, pressure_text, memory_pressure_attr(pressure_pct, colors))
        draw_memory_pressure_meter(win, y + 3, x, w, pressure_pct, colors)
        rows_start = y + 5
    else:
        rows_start = y + 3

    swap_text = (
        f"{fmt_bytes_zero(memory.swap_used_bytes)}/{fmt_bytes_zero(memory.swap_total_bytes)}"
        if memory.swap_total_bytes > 0
        else "0 B"
    )
    if detail == "compact":
        if h >= 5:
            safe_addstr(win, y + h - 2, x, "Swap", colors["muted"])
            safe_addstr(win, y + h - 2, x + 10, swap_text[: max(1, w - 11)], colors["fg"])
        return
    rows = memory_detail_value_rows(memory)
    row_space = max(0, h - (rows_start - y))
    bw_rows = memory_bandwidth_rows(sample)
    if bw_rows and row_space > 0:
        if w >= 58:
            left_w = min(28, max(20, w // 2 - 2))
            right_x = x + left_w + 2
            draw_bandwidth_rows(win, rows_start, x, row_space, left_w, bw_rows, colors)
            draw_memory_detail_rows(win, rows_start, right_x, row_space, max(0, x + w - right_x), rows, colors)
            return
        drawn = draw_bandwidth_rows(win, rows_start, x, min(row_space, len(bw_rows), 4), w, bw_rows, colors)
        draw_memory_detail_rows(win, rows_start + drawn, x, max(0, row_space - drawn), w, rows, colors)
        return
    draw_memory_detail_rows(win, rows_start, x, row_space, w, rows, colors)


def power_row_temperature(sample: MetricSample, mode: str) -> float | None:
    if mode == "cpu":
        return sample.cpu_temp_avg_c
    if mode == "gpu":
        return sample.gpu_temp_avg_c
    return None


def draw_power_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    sample: MetricSample,
    history: History,
    battery: BatteryStats,
    interval_s: float,
    colors: dict[str, int],
    detail: str = "normal",
) -> None:
    draw_box(win, y, x, h, w, "POWER", colors["accent"])
    if h < 5 or w < 34:
        return
    candidate_rows = [
        (total_power_label(sample), "soc", effective_total_power_mw(sample)),
        ("CPU", "cpu", sample.cpu_power_mw),
        ("GPU", "gpu", sample.gpu_power_mw),
        ("ANE", "ane", sample.ane_power_mw),
    ]
    rows = [row for row in candidate_rows if power_row_supported(sample, history, row[1])]
    if not rows:
        safe_addstr(win, y + 1, x + 2, "no power telemetry", colors["muted"])
        return
    if detail == "compact" or h < 7:
        compact_cols = 2 if w >= 38 else 1
        col_w = max(12, (w - 4) // compact_cols)
        value_offset = max(5, max(len(name) for name, _, _ in rows) + 1)
        for idx, (name, _, current) in enumerate(rows[: max(0, (h - 2) * compact_cols)]):
            row = idx // compact_cols
            col = idx % compact_cols
            yy = y + 1 + row
            xx = x + 2 + col * col_w
            safe_addstr(win, yy, xx, name, colors["muted"])
            safe_addstr(win, yy, xx + value_offset, fmt_power(current)[: max(1, col_w - value_offset - 1)], colors["fg"])
        return
    value_w = 9 if w >= 48 else 8
    summary_gap = min(12, max(3, w // 10 - 2))
    value_x = x + 2 + max(6, max(len(name) for name, _, _ in rows) + 2)
    current_width = max(len(fmt_power(current)) for _, _, current in rows)
    temp_w = 8
    has_temperature = any(power_row_temperature(sample, mode) is not None for _, mode, _ in rows)
    content_right = x + w - 2
    max_x = content_right - value_w
    avg_x = max_x - value_w - summary_gap
    show_summaries = avg_x >= value_x + current_width + 1
    temp_x = content_right - temp_w
    temperature_max_x = temp_x - value_w - summary_gap
    temperature_avg_x = temperature_max_x - value_w - summary_gap
    show_temperature = (
        show_summaries
        and has_temperature
        and temperature_avg_x >= value_x + current_width + 1
    )
    if show_temperature:
        avg_x = temperature_avg_x
        max_x = temperature_max_x
    if show_summaries:
        safe_addstr(win, y + 1, avg_x, "30s avg"[:value_w], colors["muted"])
        safe_addstr(win, y + 1, max_x, "peak"[:value_w], colors["muted"])
        if show_temperature:
            safe_addstr(win, y + 1, temp_x, "temp avg"[:temp_w], colors["muted"])
    current_limit = (avg_x - value_x - 1) if show_summaries else (x + w - value_x - 1)
    for idx, (name, mode, current) in enumerate(rows[: max(0, h - 3)]):
        yy = y + 2 + idx
        values = power_history_for_row(history, mode)
        avg = avg_power_window(history, values)
        peak = peak_power(values)
        safe_addstr(win, yy, x + 2, name, colors["muted"])
        safe_addstr(win, yy, value_x, fmt_power(current)[: max(1, current_limit)], colors["fg"])
        if show_summaries:
            if show_temperature:
                temperature = fmt_temp(power_row_temperature(sample, mode))
                safe_addstr(win, yy, temp_x, f"{temperature:>{temp_w}}"[-temp_w:], colors["fg"])
            safe_addstr(win, yy, avg_x, f"{fmt_power(avg):>{value_w}}"[-value_w:], colors["fg"])
            safe_addstr(win, yy, max_x, f"{fmt_power(peak):>{value_w}}"[-value_w:], colors["warn"] if peak else colors["muted"])


def draw_battery_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryStats,
    colors: dict[str, int],
    detail: str = "normal",
) -> None:
    if h < 3 or w < 24:
        return
    if not battery_supported(battery):
        safe_addstr(win, y, x, "no battery telemetry", colors["muted"])
        return
    state = battery_state_text(battery)
    left_rows = [
        ("Charge", fmt_pct(battery.charge_pct).strip()),
        ("State", state),
        ("Power", fmt_power(battery.power_mw)),
        ("Time", fmt_minutes(battery.time_remaining_min)),
    ]
    right_rows = [
        ("Health", fmt_pct(battery.health_pct).strip()),
        ("Cycles", str(battery.cycle_count) if battery.cycle_count is not None else "n/a"),
        ("Temp", fmt_temp(battery.temperature_c)),
        (
            "Design",
            f"{battery.max_capacity or battery.raw_max_capacity}/{battery.design_capacity} mAh"
            if battery.design_capacity and (battery.max_capacity or battery.raw_max_capacity)
            else "n/a",
        ),
    ]
    if detail == "compact":
        left_rows = left_rows[:3]
        right_rows = right_rows[:3]

    def draw_rows(start_x: int, width: int, rows: list[tuple[str, str]]) -> None:
        value_offset = min(10, max(7, width // 3))
        for idx, (name, value) in enumerate(rows[:h]):
            safe_addstr(win, y + idx, start_x, name[: max(1, value_offset - 1)], colors["muted"])
            attr = colors["fg"]
            if name == "Power" and battery.power_mw is not None and battery.power_mw < -HIGH_BATTERY_DRAIN_MW:
                attr = colors["warn"]
            elif name == "Health" and battery.health_pct is not None:
                attr = colors["good"] if battery.health_pct >= 80 else colors["warn"]
            safe_addstr(win, y + idx, start_x + value_offset, value[: max(1, width - value_offset)], attr)

    if w >= 44:
        column_w = max(20, (w - 2) // 2)
        draw_rows(x, column_w, left_rows)
        right_x = x + column_w + 2
        draw_rows(right_x, max(1, x + w - right_x), right_rows)
        return

    narrow_rows = [
        left_rows[0],
        left_rows[1],
        left_rows[2],
        right_rows[2],
        *left_rows[3:],
        *right_rows[:2],
        *right_rows[3:],
    ]
    for idx, (name, value) in enumerate(narrow_rows[:h]):
        safe_addstr(win, y + idx, x, name, colors["muted"])
        attr = colors["fg"]
        if name == "Power" and battery.power_mw is not None and battery.power_mw < -HIGH_BATTERY_DRAIN_MW:
            attr = colors["warn"]
        elif name == "Health" and battery.health_pct is not None:
            attr = colors["good"] if battery.health_pct >= 80 else colors["warn"]
        value_offset = min(12, max(8, w // 3))
        safe_addstr(win, y + idx, x + value_offset, value[: max(1, w - value_offset)], attr)


def draw_usb_c_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    usb_c: UsbCStats,
    colors: dict[str, int],
    detail: str = "normal",
) -> None:
    if h < 3 or w < 24:
        return
    if not usb_c_supported(usb_c):
        safe_addstr(win, y, x, "no charge-input telemetry", colors["muted"])
        return
    active = usb_c.active_port
    state = usb_c_state_text(usb_c)
    input_active = charge_input_active(usb_c)
    input_label = active.label if active else charge_input_label(usb_c) if input_active else "n/a"
    voltage = first_non_none(usb_c.system_voltage_v, usb_c.adapter_voltage_v, active.voltage_v if active else None) if input_active else None
    current = first_non_none(usb_c.system_current_a, usb_c.adapter_current_a, active.current_a if active else None) if input_active else None
    contract_power = first_non_none(usb_c.system_power_w, usb_c.adapter_contract_power_w, active.power_w if active else None) if input_active else None
    rows: list[tuple[str, str, int]] = [
        ("State", state, colors["good"] if usb_c.external_connected or (active and active.connected) else colors["muted"]),
        ("Input", input_label, colors["fg"]),
        ("Voltage", fmt_voltage(voltage), colors["fg"]),
        ("Current", fmt_current(current), colors["fg"]),
        ("Power", fmt_watts(contract_power), colors["warn"] if contract_power and contract_power >= 60 else colors["fg"]),
        ("Adapter", fmt_watts(usb_c.adapter_power_w) if usb_c.adapter_power_w is not None else (usb_c.adapter_name or "n/a"), colors["fg"]),
    ]
    if active and active.max_power_w is not None:
        rows.append(("PD max", fmt_watts(active.max_power_w), colors["fg"]))
    if active and active.pdo_labels:
        rows.append(("PDOs", " | ".join(active.pdo_labels[:4]), colors["fg"]))
    elif usb_c.ports:
        rows.append(("Ports", str(len(usb_c.ports)), colors["fg"]))
    if detail == "compact":
        rows = rows[:4]
    elif detail == "normal":
        rows = rows[:7]
    for idx, (name, value, attr) in enumerate(rows[:h]):
        safe_addstr(win, y + idx, x, name, colors["muted"])
        safe_addstr(win, y + idx, x + min(12, max(8, w // 3)), value[: max(1, w - 14)], attr)


def draw_cable_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    usb_c: UsbCStats,
    colors: dict[str, int],
    detail: str = "normal",
) -> None:
    if h < 3 or w < 24:
        return
    port = selected_cable_port(usb_c)
    if port is None:
        safe_addstr(win, y, x, "no public cable telemetry", colors["muted"])
        return
    info = port.cable_info if port.connected else None
    data_link = (
        usb_data_link_label(port.active_transports, port.data_link_speed_bps)
        if port.connected
        else "unavailable"
    )
    dp_alt = (
        "unavailable"
        if not port.connected
        else "active"
        if "DisplayPort" in port.active_transports
        else "not active"
    )

    def draw_rows(start_x: int, width: int, rows: list[tuple[str, str]]) -> None:
        value_offset = min(10, max(7, width // 3))
        for idx, (name, value) in enumerate(rows[:h]):
            safe_addstr(win, y + idx, start_x, name[: max(1, value_offset - 1)], colors["muted"])
            safe_addstr(win, y + idx, start_x + value_offset, value[: max(1, width - value_offset)], colors["fg"])

    if info is None:
        rows = [
            ("Status", "no E-marker data"),
            ("Port", port.label),
            ("Link", "connected" if port.connected else "idle"),
            ("Data link", data_link),
            ("DP Alt", dp_alt),
        ]
        draw_rows(x, w, rows)
        return

    rating = fmt_watts(info.max_power_w)
    if info.max_voltage_v is not None:
        voltage = f"{info.max_voltage_v:.0f} V max"
        rating = f"{rating} · {voltage}" if info.max_power_w is not None else voltage
    left_rows = [
        ("Status", "E-marker"),
        ("Port", port.label),
        ("Type", info.cable_type or "n/a"),
        ("Current", info.current_label or "n/a"),
        ("DP Alt", dp_alt),
    ]
    right_rows = [
        ("Rating", rating),
        ("Speed", info.speed_label or "n/a"),
        ("Vendor", f"0x{info.vendor_id:04X}" if info.vendor_id is not None else "n/a"),
        ("Product", f"0x{info.product_id:04X}" if info.product_id is not None else "n/a"),
        ("PD", info.pd_revision or "n/a"),
    ]
    if detail == "compact":
        left_rows = left_rows[:3]
        right_rows = right_rows[:3]

    if w >= 58:
        column_w = max(26, (w - 2) // 2)
        draw_rows(x, column_w, left_rows)
        right_x = x + column_w + 2
        draw_rows(right_x, max(1, x + w - right_x), right_rows)
        link_row = max(len(left_rows), len(right_rows))
        if link_row < h:
            safe_addstr(win, y + link_row, x, "Data link", colors["muted"])
            safe_addstr(win, y + link_row, x + 10, data_link[: max(1, w - 10)], colors["fg"])
        return
    draw_rows(x, w, [*left_rows, ("Data link", data_link), *right_rows])


def draw_io_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    io_stats: IoStats,
    colors: dict[str, int],
) -> None:
    if h < 3 or w < 24:
        return
    rows = [
        ("Disk rd", fmt_rate(io_stats.disk_read_bps)),
        ("Disk wr", fmt_rate(io_stats.disk_write_bps)),
        ("Net in", fmt_rate(io_stats.net_in_bps)),
        ("Net out", fmt_rate(io_stats.net_out_bps)),
    ]
    for idx, (name, value) in enumerate(rows[:h]):
        safe_addstr(win, y + idx, x, name, colors["muted"])
        safe_addstr(win, y + idx, x + 10, value[: max(1, w - 11)], colors["fg"])


def draw_process_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    processes: list[ProcessInfo],
    sort_key: str,
    selected_index: int,
    pending_kill_pid: int | None,
    colors: dict[str, int],
) -> None:
    draw_box_hotkey_title(win, y, x, h, w, "PROCESSES", colors["accent"], colors, "p")
    hint = f" sort {sort_key}  arrows move/sort  k kill "
    if w > len(hint) + 16:
        safe_addstr(win, y, x + w - len(hint) - 2, hint, colors["muted"])
    ordered = sorted_processes(processes, sort_key)
    if not ordered:
        safe_addstr(win, y + 2, x + 2, "no process data", colors["muted"])
        return
    show_gpu = any(process.gpu_pct is not None for process in ordered)
    min_width = 54 if show_gpu else 47
    if h < 5 or w < min_width:
        return
    selected_index = int(clamp(selected_index, 0, len(ordered) - 1))
    details_h = 4 if h >= 12 else 0
    rows_h = h - 3 - details_h
    offset = max(0, min(selected_index - rows_h // 2, max(0, len(ordered) - rows_h)))
    header_y = y + 1
    sort_attr = colors["warn"] | curses.A_BOLD
    ram_x = 24 if show_gpu else 17
    rss_x = 31 if show_gpu else 24
    command_x = 42 if show_gpu else 35
    safe_addstr(win, header_y, x + 2, "PID", sort_attr if sort_key == "pid" else colors["muted"])
    safe_addstr(win, header_y, x + 10, "CPU%", sort_attr if sort_key == "cpu" else colors["muted"])
    if show_gpu:
        safe_addstr(win, header_y, x + 17, "GPU%", sort_attr if sort_key == "gpu" else colors["muted"])
    safe_addstr(win, header_y, x + ram_x, "RAM%", sort_attr if sort_key == "ram" else colors["muted"])
    safe_addstr(win, header_y, x + rss_x, "RSS", colors["muted"])
    safe_addstr(win, header_y, x + command_x, "COMMAND", sort_attr if sort_key == "name" else colors["muted"])
    for row, proc in enumerate(ordered[offset : offset + rows_h]):
        yy = y + 2 + row
        absolute_index = offset + row
        attr = colors["bold"] if absolute_index == selected_index else colors["fg"]
        if proc.pid == pending_kill_pid:
            attr = colors["bad"] | curses.A_BOLD
        if absolute_index == selected_index:
            safe_addstr(win, yy, x + 1, ">", colors["warn"] | curses.A_BOLD)
        safe_addstr(win, yy, x + 2, f"{proc.pid:>6}", attr)
        safe_addstr(win, yy, x + 10, f"{proc.cpu_pct:5.1f}", colors["good"] if proc.cpu_pct < 60 else colors["warn"])
        if show_gpu:
            gpu_text = f"{proc.gpu_pct:5.1f}" if proc.gpu_pct is not None else "  n/a"
            gpu_attr = colors["muted"] if proc.gpu_pct is None else (colors["good"] if proc.gpu_pct < 60 else colors["warn"])
            safe_addstr(win, yy, x + 17, gpu_text, gpu_attr)
        safe_addstr(win, yy, x + ram_x, f"{proc.mem_pct:5.1f}", colors["fg"])
        safe_addstr(win, yy, x + rss_x, f"{fmt_bytes_zero(proc.rss_kib * 1024):>9}"[-9:], colors["fg"])
        safe_addstr(win, yy, x + command_x, proc.command[: max(1, w - command_x - 2)], attr)
    if details_h:
        selected = ordered[selected_index]
        detail_y = y + h - details_h
        safe_addstr(win, detail_y, x + 2, "─" * max(1, w - 4), colors["dim"])
        detail = (
            f"PID {selected.pid}"
            + (f"  user {selected.user}" if selected.user else "")
            + (f"  PPID {selected.ppid}" if selected.ppid is not None else "")
            + (f"  time {selected.etime}" if selected.etime else "")
            + f"  CPU {selected.cpu_pct:.1f}%"
            + (f"  GPU {fmt_pct(selected.gpu_pct).strip()}" if show_gpu else "")
            + f"  RAM {selected.mem_pct:.1f}%"
        )
        safe_addstr(win, detail_y + 1, x + 2, detail[: max(1, w - 4)], colors["fg"])
        kill_hint = "press k again to TERM" if selected.pid == pending_kill_pid else "k marks for TERM"
        safe_addstr(win, detail_y + 2, x + 2, kill_hint[: max(1, w - 4)], colors["bad"] if selected.pid == pending_kill_pid else colors["muted"])
        full_command = selected.full_command or selected.command
        safe_addstr(win, detail_y + 3, x + 2, full_command[: max(1, w - 4)], colors["muted"])


def io_value(io_stats: IoStats, mode: str) -> float | None:
    if mode == "disk_read":
        return io_stats.disk_read_bps
    if mode == "disk_write":
        return io_stats.disk_write_bps
    if mode == "net_in":
        return io_stats.net_in_bps
    if mode == "net_out":
        return io_stats.net_out_bps
    return None


def io_history(history: History, mode: str) -> deque:
    if mode == "disk_read":
        return history.disk_read_io
    if mode == "disk_write":
        return history.disk_write_io
    if mode == "net_in":
        return history.net_in_io
    if mode == "net_out":
        return history.net_out_io
    return deque(maxlen=history.length)


def io_graph_attr(value: float | None, scale: float, colors: dict[str, int]) -> int:
    ratio = value / scale if value is not None and value > 0 and scale > 0 else None
    return graph_gradient_attr(ratio, colors, warn_at=0.55, bad_at=0.85)


def draw_io_columns(
    win: curses.window,
    baseline: int,
    x: int,
    h: int,
    values: Iterable[float | None],
    width: int,
    scale: float,
    colors: dict[str, int],
    *,
    direction: int,
) -> None:
    dense_w = width * 2
    tails = tail_values(values, dense_w)
    cols = scaled_columns(values, dense_w, h, scale)
    for col in range(width):
        left_idx = col * 2
        right_idx = left_idx + 1
        attr_value = max((tails[left_idx] or 0.0), (tails[right_idx] or 0.0))
        attr = io_graph_attr(attr_value, scale, colors)
        for step in range(h):
            mask = 0
            left_amount = cols[left_idx]
            right_amount = cols[right_idx]
            if left_amount is not None and left_amount > step:
                mask |= sum(BRAILLE_LEFT_DOTS)
            if right_amount is not None and right_amount > step:
                mask |= sum(BRAILLE_RIGHT_DOTS)
            if not mask:
                continue
            yy = baseline - 1 - step if direction < 0 else baseline + 1 + step
            safe_addstr(win, yy, x + col, chr(0x2800 + mask), attr)


def draw_io_mini_graph(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    io_stats: IoStats,
    history: History,
    colors: dict[str, int],
    upper_io_mode: str,
    lower_io_mode: str,
    *,
    heading: bool = True,
) -> None:
    if h < 5 or w < 28:
        draw_io_section(win, y, x, h, w, io_stats, colors)
        return
    upper_label = IO_MODE_LABELS.get(upper_io_mode, upper_io_mode)
    lower_label = IO_MODE_LABELS.get(lower_io_mode, lower_io_mode)
    labels = f"{upper_label} {fmt_rate(io_value(io_stats, upper_io_mode))}  {lower_label} {fmt_rate(io_value(io_stats, lower_io_mode))}"
    if heading:
        safe_addstr(win, y, x, "Disk / Net", colors["accent"] | curses.A_BOLD)
    if len(labels) < w:
        safe_addstr(win, y, x + w - len(labels), labels, colors["fg"])
    else:
        safe_addstr(win, y, x, labels[:w], colors["fg"])
    graph_y = y + 1
    graph_h = h - 1
    baseline = graph_y + graph_h // 2
    upper_h = max(1, baseline - graph_y)
    lower_h = max(1, graph_y + graph_h - baseline - 1)
    safe_addstr(win, baseline, x, "─" * w, colors["dim"])
    upper_history = io_history(history, upper_io_mode)
    lower_history = io_history(history, lower_io_mode)
    present = [value for value in (*upper_history, *lower_history) if value is not None]
    scale = max(present, default=1.0)
    draw_io_columns(win, baseline, x, upper_h, upper_history, w, scale, colors, direction=-1)
    draw_io_columns(win, baseline, x, lower_h, lower_history, w, scale, colors, direction=1)
    legend_width = len(f"I {upper_label} / O {lower_label}")
    if legend_width + 2 < w:
        xx = x + max(0, w // 2 - legend_width // 2)
        safe_addstr(win, baseline, xx, " ", colors["fg"])
        xx += 1
        xx = draw_label_hotkey(win, baseline, xx, f"I {upper_label}", "i", colors)
        safe_addstr(win, baseline, xx, " / ", colors["fg"])
        xx += 3
        xx = draw_label_hotkey(win, baseline, xx, f"O {lower_label}", "o", colors)
        safe_addstr(win, baseline, xx, " ", colors["fg"])


def alert_thresholds(args: argparse.Namespace | None = None) -> tuple[float, int, float]:
    temp_c = float(getattr(args, "alert_temp_c", HIGH_TEMP_C)) if args is not None else HIGH_TEMP_C
    swap_gib = float(getattr(args, "alert_swap_gib", DEFAULT_ALERT_SWAP_GIB)) if args is not None else DEFAULT_ALERT_SWAP_GIB
    battery_drain_w = (
        float(getattr(args, "alert_battery_drain_w", DEFAULT_ALERT_BATTERY_DRAIN_W))
        if args is not None
        else DEFAULT_ALERT_BATTERY_DRAIN_W
    )
    return (
        clamp(temp_c, 40.0, 125.0),
        int(clamp(swap_gib, 0.0, 1024.0) * 1024**3),
        clamp(battery_drain_w, 0.0, 250.0) * 1000.0,
    )


def build_alerts(sample: MetricSample, memory: MemoryStats, battery: BatteryStats, args: argparse.Namespace | None = None) -> list[str]:
    alerts: list[str] = []
    high_temp_c, high_swap_bytes, high_battery_drain_mw = alert_thresholds(args)
    if sample.throttled:
        alerts.append("THROTTLE")
    if sample.temp_max_c is not None and sample.temp_max_c >= high_temp_c:
        alerts.append(f"TEMP {sample.temp_max_c:.0f}°C")
    if high_swap_bytes > 0 and memory.swap_used_bytes >= high_swap_bytes:
        alerts.append(f"SWAP {fmt_bytes_zero(memory.swap_used_bytes)}")
    battery_power = first_non_none(battery.power_mw, sample.battery_power_mw)
    if battery_power is not None and battery_power < -high_battery_drain_mw:
        alerts.append(f"BAT {fmt_power(battery_power)}")
    return alerts


def draw_usage_matrix(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    sample: MetricSample,
    history: History,
    colors: dict[str, int],
    load_view: str,
    show_io: bool,
    io_stats: IoStats,
    upper_io_mode: str,
    lower_io_mode: str,
) -> None:
    draw_box(win, y, x, h, w, "CPU / GPU LOAD", colors["accent"])
    hint = "l avg rows" if load_view == "graph" else "l avg graph"
    if w > len(hint) + 8:
        xx = x + w - len(hint) - 2
        safe_addstr(win, y, xx - 1, " ", colors["muted"])
        draw_label_hotkey(win, y, xx, hint, "l", colors)
        safe_addstr(win, y, xx + len(hint), " ", colors["muted"])
    if h < 5 or w < 34:
        return
    cores = sample.cores
    if not cores:
        cores = [
            CoreMetric("P avg", sample.p_usage_pct),
            CoreMetric("E avg", sample.e_usage_pct),
        ]
    half = (len(cores) + 1) // 2
    left = cores[:half]
    right = cores[half:]
    col_gap = 3
    col_w = max(18, (w - 4 - col_gap) // 2)
    right_x = x + 2 + col_w + col_gap
    right_w = max(12, w - 4 - col_w - col_gap)
    io_h = 7 if show_io and h >= 21 else 0
    footer_min_h = 5 if load_view == "graph" else 2
    max_core_rows = max(0, h - footer_min_h - io_h - 4)
    visible_left = left[:max_core_rows]
    visible_right = right[:max_core_rows]
    for idx, core in enumerate(visible_left):
        draw_usage_row(
            win,
            y + 1 + idx,
            x + 2,
            col_w - 1,
            core.label,
            history.core_usage.get(core.label, deque(maxlen=history.length)),
            core.usage_pct,
            colors,
        )
    for idx, core in enumerate(visible_right):
        draw_usage_row(
            win,
            y + 1 + idx,
            right_x,
            right_w,
            core.label,
            history.core_usage.get(core.label, deque(maxlen=history.length)),
            core.usage_pct,
            colors,
        )

    core_rows = max(len(visible_left), len(visible_right))
    footer_y = y + 1 + core_rows + 1
    footer_limit = y + h - 1 - io_h
    if footer_y >= footer_limit:
        footer_y = max(y + 1, footer_limit - 2)
    footer_h = max(0, footer_limit - footer_y)
    cpu_avg = current_cpu_usage(sample)
    if load_view == "graph" and footer_h >= 5:
        draw_avg_load_graph(win, footer_y, x + 2, footer_h, w - 4, sample, history, colors)
    else:
        draw_usage_row(win, footer_y, x + 2, w - 4, "CPU avg", history.cpu_usage, cpu_avg, colors)
        if footer_y + 1 < footer_limit:
            draw_usage_row(win, footer_y + 1, x + 2, w - 4, "GPU avg", history.gpu_usage, sample.gpu_usage_pct, colors)
    if io_h:
        io_y = y + h - 1 - io_h
        draw_io_mini_graph(win, io_y, x + 2, io_h, w - 4, io_stats, history, colors, upper_io_mode, lower_io_mode)


def init_colors(theme_name: str) -> dict[str, int]:
    pairs = {"fg": 0, "muted": 0, "good": 0, "warn": 0, "bad": 0, "accent": 0, "dim": curses.A_DIM, "bold": curses.A_BOLD}
    if not curses.has_colors():
        return pairs
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return pairs
    theme = THEMES.get(theme_name, THEMES["classic"])
    for index, name in enumerate(("fg", "muted", "good", "warn", "bad", "accent"), start=1):
        try:
            curses.init_pair(index, theme[name], -1)
            pairs[name] = curses.color_pair(index)
        except curses.error:
            pairs[name] = 0
    try:
        curses.init_pair(7, curses.COLOR_WHITE, -1)
        pairs["dim"] = curses.color_pair(7) | curses.A_DIM
    except curses.error:
        pairs["dim"] = pairs["fg"] | curses.A_DIM
    supports_gradient = (
        theme_name != "mono"
        and getattr(curses, "COLORS", 0) >= 256
        and getattr(curses, "COLOR_PAIRS", 0) >= 8 + len(GRAPH_GRADIENT_COLORS)
    )
    if supports_gradient:
        gradient_pairs: dict[str, int] = {}
        try:
            for offset, color in enumerate(GRAPH_GRADIENT_COLORS):
                pair_index = 8 + offset
                curses.init_pair(pair_index, color, -1)
                gradient_pairs[f"graph_{offset}"] = curses.color_pair(pair_index)
        except curses.error:
            gradient_pairs.clear()
        pairs.update(gradient_pairs)
    pairs["bold"] = pairs["fg"] | curses.A_BOLD
    return pairs


def source_status(
    args: argparse.Namespace,
    sample: MetricSample,
    memory: MemoryStats,
    battery: BatteryStats,
    usb_c: UsbCStats,
    io_stats: IoStats,
    processes: list[ProcessInfo],
    process_panel: str,
    side_warnings: Iterable[str] = (),
) -> str:
    sources = ["mock" if args.mock else "pm"]
    missing: list[str] = []
    if sample.warning:
        missing.append("pm")
    if sample.soc_temp_c is not None or sample.temp_max_c is not None:
        sources.append("temp")
    else:
        missing.append("temp")
    if memory.total_bytes > 0:
        sources.append("vm")
    else:
        missing.append("vm")
    if battery_supported(battery) or usb_c_supported(usb_c):
        sources.append("ioreg")
    else:
        missing.append("ioreg")
    if layout_uses_io(args):
        if any(value is not None for value in (io_stats.disk_read_bps, io_stats.disk_write_bps, io_stats.net_in_bps, io_stats.net_out_bps)):
            sources.append("io")
        elif args.layout != "focus":
            missing.append("io")
    if layout_uses_process(args, process_panel):
        if processes:
            sources.append("ps")
        else:
            missing.append("ps")
    elif processes:
        sources.append("ps")
    text = "src " + ",".join(sources)
    if missing:
        text += "  miss " + ",".join(dict.fromkeys(missing))
    warnings = list(dict.fromkeys(str(warning) for warning in side_warnings if warning))
    if warnings:
        text += "  warn " + ",".join(warnings[-2:])
    return text


def draw_help_overlay(win: curses.window, colors: dict[str, int]) -> None:
    max_y, max_x = win.getmaxyx()
    rows = [
        ("q", "quit", f"close {APP_NAME}"),
        ("? / h", "help", "toggle this overlay"),
        ("m", "menu", "edit settings"),
        ("t", "theme", "cycle color themes"),
        ("T", "tailor", "edit numbered custom slots"),
        ("+ / -", "interval", "change sampler interval"),
        ("v", "layout", "cycle full/compact/focus/custom layouts"),
        ("d", "disk/net", "show or hide I/O graph"),
        ("i / o", "I/O source", "cycle upper/lower disk-net graph"),
        ("S/C/G/A", "upper power", "select SoC/package/CPU/GPU/ANE"),
        ("s/c/g/a", "lower power", "select SoC/package/CPU/GPU/ANE"),
        ("u / n", "power cycle", "cycle upper/lower power graph"),
        ("L", "load view", "toggle CPU/GPU avg rows/graph"),
        ("b", "charge panel", "cycle Battery/Power Input/Cable"),
        ("p", "process panel", "hidden -> left -> right"),
        ("Up/Down", "process select", "move process cursor"),
        ("Left/Right", "process sort", "cycle CPU/GPU/RAM/PID/name"),
        ("k, k", "process TERM", "second press confirms kill"),
        ("menu", "Root kill", "disabled by default when running as root"),
        ("r", "reset peaks", "clear power history and peaks"),
    ]
    key_w = max(len("Key"), max(len(row[0]) for row in rows))
    action_w = max(len("Action"), max(len(row[1]) for row in rows))
    note_w = max(len("Note"), max(len(row[2]) for row in rows))
    content_w = key_w + action_w + note_w + 8
    logo_h = len(LOGO_LINES) if max_y >= len(rows) + len(LOGO_LINES) + 7 and max_x >= LOGO_WIDTH + 10 else 0
    logo_gap = 1 if logo_h else 0
    w = min(max_x - 4, max(58, content_w + 4, LOGO_WIDTH + 6 if logo_h else 0))
    h = min(max_y - 4, len(rows) + 4 + logo_h + logo_gap)
    y = max(1, (max_y - h) // 2)
    x = max(1, (max_x - w) // 2)
    fill_rect(win, y, x, h, w, colors["fg"])
    draw_box(win, y, x, h, w, "HELP", colors["accent"])
    content_y = y + 1
    if logo_h:
        draw_logo(win, content_y, x + 2, w - 4, colors)
        content_y += logo_h + logo_gap
    header = f"{'Key':<{key_w}}  {'Action':<{action_w}}  Note"
    safe_addstr(win, content_y, x + 2, header[: max(1, w - 4)], colors["bold"])
    safe_addstr(win, content_y + 1, x + 2, "-" * max(1, min(w - 4, len(header))), colors["dim"])
    row_y = content_y + 2
    visible_rows = max(0, h - (row_y - y) - 1)
    for idx, (key, action, note) in enumerate(rows[:visible_rows]):
        yy = row_y + idx
        key_attr = colors["warn"] | curses.A_BOLD
        safe_addstr(win, yy, x + 2, f"{key:<{key_w}}", key_attr)
        safe_addstr(win, yy, x + 4 + key_w, f"{action:<{action_w}}", colors["fg"])
        note_x = x + 6 + key_w + action_w
        safe_addstr(win, yy, note_x, note[: max(1, x + w - 2 - note_x)], colors["muted"])


def menu_value_text(
    item_id: str,
    args: argparse.Namespace,
    upper_power_mode: str,
    lower_power_mode: str,
    upper_io_mode: str,
    lower_io_mode: str,
    load_view: str,
    process_panel: str,
    process_sort: str,
    charge_panel: str,
    sample: MetricSample | None = None,
) -> str:
    if item_id == "theme":
        return args.theme
    if item_id == "layout":
        return args.layout
    if item_id == "interval":
        return interval_text(args.interval)
    if item_id == "show_io":
        return "on" if args.show_io else "off"
    if item_id == "upper_power":
        return selected_power_label(upper_power_mode, sample)
    if item_id == "lower_power":
        return selected_power_label(lower_power_mode, sample)
    if item_id == "upper_io":
        return IO_MODE_LABELS.get(upper_io_mode, upper_io_mode)
    if item_id == "lower_io":
        return IO_MODE_LABELS.get(lower_io_mode, lower_io_mode)
    if item_id == "load_view":
        return load_view
    if item_id == "process_panel":
        return process_panel
    if item_id == "process_sort":
        return process_sort
    if item_id == "charge_panel":
        return "power input" if charge_panel == "usb" else charge_panel
    if item_id == "allow_root_kill":
        return "on" if bool(getattr(args, "allow_root_kill", False)) else "off"
    if item_id == "alert_temp":
        return fmt_temp(float(args.alert_temp_c))
    if item_id == "alert_swap":
        return f"{float(args.alert_swap_gib):.2f} GiB"
    if item_id == "alert_battery":
        return f"{float(args.alert_battery_drain_w):.1f} W"
    return ""


def draw_menu_overlay(
    win: curses.window,
    colors: dict[str, int],
    args: argparse.Namespace,
    selected: int,
    upper_power_mode: str,
    lower_power_mode: str,
    upper_io_mode: str,
    lower_io_mode: str,
    load_view: str,
    process_panel: str,
    process_sort: str,
    charge_panel: str,
    sample: MetricSample | None = None,
) -> None:
    max_y, max_x = win.getmaxyx()
    label_w = max(len("Setting"), max(len(label) for _, label, _ in MENU_ITEMS))
    value_w = 16
    desc_w = max(len("Description"), max(len(desc) for _, _, desc in MENU_ITEMS))
    content_w = label_w + value_w + desc_w + 10
    logo_h = len(LOGO_LINES) if max_y >= len(MENU_ITEMS) + len(LOGO_LINES) + 8 and max_x >= LOGO_WIDTH + 10 else 0
    logo_gap = 1 if logo_h else 0
    w = min(max_x - 4, max(72, content_w + 4, LOGO_WIDTH + 6 if logo_h else 0))
    h = min(max_y - 4, len(MENU_ITEMS) + 5 + logo_h + logo_gap)
    y = max(1, (max_y - h) // 2)
    x = max(1, (max_x - w) // 2)
    fill_rect(win, y, x, h, w, colors["fg"])
    draw_box(win, y, x, h, w, "MENU", colors["accent"])
    content_y = y + 1
    if logo_h:
        draw_logo(win, content_y, x + 2, w - 4, colors)
        content_y += logo_h + logo_gap
    header = f"{'Setting':<{label_w}}  {'Value':<{value_w}}  Description"
    safe_addstr(win, content_y, x + 2, header[: max(1, w - 4)], colors["bold"])
    safe_addstr(win, content_y + 1, x + 2, "-" * max(1, min(w - 4, len(header))), colors["dim"])
    row_y = content_y + 2
    visible_rows = max(0, h - (row_y - y) - 2)
    selected = int(clamp(selected, 0, len(MENU_ITEMS) - 1))
    offset = max(0, min(selected - visible_rows // 2, max(0, len(MENU_ITEMS) - visible_rows)))
    for row, (item_id, label, desc) in enumerate(MENU_ITEMS[offset : offset + visible_rows]):
        idx = offset + row
        yy = row_y + row
        active = idx == selected
        attr = colors["warn"] | curses.A_BOLD if active else colors["fg"]
        marker = ">" if active else " "
        value = menu_value_text(
            item_id,
            args,
            upper_power_mode,
            lower_power_mode,
            upper_io_mode,
            lower_io_mode,
            load_view,
            process_panel,
            process_sort,
            charge_panel,
            sample,
        )
        safe_addstr(win, yy, x + 2, marker, colors["warn"] | curses.A_BOLD if active else colors["muted"])
        safe_addstr(win, yy, x + 4, f"{label:<{label_w}}"[:label_w], attr)
        safe_addstr(win, yy, x + 6 + label_w, f"{value:<{value_w}}"[:value_w], attr)
        desc_x = x + 8 + label_w + value_w
        safe_addstr(win, yy, desc_x, desc[: max(1, x + w - 2 - desc_x)], colors["muted"])
    footer = "Up/Down select  Left/Right or Enter change  Tab next  s save  Esc close"
    safe_addstr(win, y + h - 1, x + max(2, w - len(footer) - 2), footer[: max(1, w - 4)], colors["muted"])


def tailor_slot_number(slot_id: str) -> int | None:
    try:
        return CUSTOM_SLOT_IDS.index(slot_id) + 1
    except ValueError:
        return None


def tailor_slot_for_key(key: int) -> str | None:
    if ord("1") <= key <= ord(str(min(9, len(CUSTOM_SLOT_IDS)))):
        index = key - ord("1")
        if 0 <= index < len(CUSTOM_SLOT_IDS):
            return CUSTOM_SLOT_IDS[index]
    return None


def tailor_menu_value_text(item_id: str, args: argparse.Namespace) -> str:
    slot_id = getattr(args, "custom_slot", CUSTOM_SLOT_IDS[0])
    config = custom_slot_config(getattr(args, "custom_layout", {}), slot_id)
    if item_id == "panel":
        return config["panel"]
    if item_id == "detail":
        return config["detail"]
    if item_id == "name":
        return clean_custom_name(getattr(args, "custom_name", "")) or "unnamed"
    return ""


def tailor_menu_rect(max_y: int, max_x: int, anchor: Rect | None) -> Rect:
    h = min(max_y - 4, 8)
    w = min(max_x - 4, 46)
    if h < 6 or w < 30:
        return Rect(1, 1, max(3, h), max(20, w))
    if anchor is None:
        return Rect(2, max(1, max_x - w - 2), h, w)
    if anchor.x + anchor.w // 2 < max_x // 2:
        x = max_x - w - 2
    else:
        x = 2
    return Rect(2, x, h, w)


def draw_tailor_overlay(
    win: curses.window,
    layout: DashboardLayout,
    colors: dict[str, int],
    args: argparse.Namespace,
    menu_visible: bool,
    menu_selected: int,
    name_buffer: str,
    name_editing: bool,
) -> None:
    for slot_id in CUSTOM_SLOT_IDS:
        placement = layout.slot(slot_id)
        number = tailor_slot_number(slot_id)
        if placement is None or number is None:
            continue
        selected = slot_id == getattr(args, "custom_slot", CUSTOM_SLOT_IDS[0])
        attr = (colors["warn"] if selected else colors["accent"]) | curses.A_BOLD
        safe_addstr(win, placement.rect.y, placement.rect.x + 1, str(number), attr)
    max_y, max_x = win.getmaxyx()
    hint = "Tailor: 1-7 select slot  Enter edit  T/Esc close"
    safe_addstr(win, max_y - 1, max(1, max_x - len(hint) - 2), hint[: max(1, max_x - 2)], colors["muted"])
    if not menu_visible:
        return
    selected_slot = getattr(args, "custom_slot", CUSTOM_SLOT_IDS[0])
    anchor = layout.slot(selected_slot).rect if layout.slot(selected_slot) is not None else None
    rect = tailor_menu_rect(max_y, max_x, anchor)
    fill_rect(win, rect.y, rect.x, rect.h, rect.w, colors["fg"])
    draw_box(win, rect.y, rect.x, rect.h, rect.w, "TAILOR", colors["accent"])
    rows = (
        ("panel", "Custom panel"),
        ("detail", "Custom detail"),
        ("name", "Custom name"),
    )
    label_w = max(len(label) for _, label in rows)
    for idx, (item_id, label) in enumerate(rows[: max(0, rect.h - 3)]):
        yy = rect.y + 1 + idx
        active = idx == menu_selected
        attr = colors["warn"] | curses.A_BOLD if active else colors["fg"]
        marker = ">" if active else " "
        value = name_buffer if item_id == "name" and name_editing else tailor_menu_value_text(item_id, args)
        if item_id == "name" and name_editing:
            value = f"{value}_"
        safe_addstr(win, yy, rect.x + 2, marker, colors["warn"] | curses.A_BOLD if active else colors["muted"])
        safe_addstr(win, yy, rect.x + 4, f"{label:<{label_w}}"[:label_w], attr)
        safe_addstr(win, yy, rect.x + 6 + label_w, value[: max(1, rect.x + rect.w - (rect.x + 8 + label_w))], attr)
    footer = "Left/Right change  Enter edit name  Esc close"
    safe_addstr(win, rect.y + rect.h - 1, rect.x + 2, footer[: max(1, rect.w - 4)], colors["muted"])


def draw_modal_overlays(
    win: curses.window,
    colors: dict[str, int],
    args: argparse.Namespace,
    help_visible: bool,
    menu_visible: bool,
    menu_selected: int,
    upper_power_mode: str,
    lower_power_mode: str,
    upper_io_mode: str,
    lower_io_mode: str,
    load_view: str,
    process_panel: str,
    process_sort: str,
    charge_panel: str,
    sample: MetricSample | None = None,
) -> None:
    if help_visible:
        draw_help_overlay(win, colors)
    if menu_visible:
        draw_menu_overlay(
            win,
            colors,
            args,
            menu_selected,
            upper_power_mode,
            lower_power_mode,
            upper_io_mode,
            lower_io_mode,
            load_view,
            process_panel,
            process_sort,
            charge_panel,
            sample,
        )


def power_graph_height(layout: str, max_y: int) -> int:
    if layout == "compact":
        return 6
    if layout == "focus":
        return max(7, min(12, max_y // 4))
    return max(7, min(11, max_y // 5))


def panel_detail(layout_name: str, panel_id: str, rect: Rect) -> str:
    spec = PANEL_SPECS[panel_id]
    if layout_name == "compact" and "compact" in spec.detail_levels:
        return "compact"
    if (rect.h <= spec.min_h or rect.w < spec.min_w) and "compact" in spec.detail_levels:
        return "compact"
    return spec.default_detail if spec.default_detail in spec.detail_levels else "normal"


def panel_placement(layout_name: str, panel_id: str, rect: Rect, slot: str = "", detail: str | None = None) -> PanelPlacement:
    selected_detail = detail if detail in detail_levels_for_panel(panel_id) else panel_detail(layout_name, panel_id, rect)
    return PanelPlacement(panel_id, rect, selected_detail, slot)


def layout_panel(layout: DashboardLayout, panel_id: str, slot: str = "") -> PanelPlacement | None:
    return layout.panel(panel_id, slot)


def panel_id_for_slot(slot: LayoutSlot, process_left: bool, process_right: bool) -> str:
    if slot.slot_id == "lower_left" and process_left:
        return "process"
    if slot.slot_id == "right_middle" and process_right:
        return "process"
    return slot.default_panel_id


def panel_placements_from_template(
    layout_name: str,
    template: LayoutTemplate,
    rects: dict[str, Rect],
    process_left: bool = False,
    process_right: bool = False,
    custom_layout: dict[str, dict[str, str]] | None = None,
) -> tuple[PanelPlacement, ...]:
    placements: list[PanelPlacement] = []
    custom_layout = sanitize_custom_layout(custom_layout or {})
    for slot in template.slots:
        rect = rects.get(slot.slot_id)
        if rect is None:
            continue
        detail = None
        if layout_name == "custom" and slot.slot_id in CUSTOM_SLOT_IDS:
            config = custom_slot_config(custom_layout, slot.slot_id)
            panel_id = config["panel"]
            detail = config["detail"]
        else:
            panel_id = panel_id_for_slot(slot, process_left, process_right)
            if panel_id == "process" and slot.slot_id == "right_middle" and rect.h < 8:
                panel_id = slot.default_panel_id
        placements.append(panel_placement(layout_name, panel_id, rect, slot.slot_id, detail))
    return tuple(placements)


def dashboard_layout(
    max_y: int,
    max_x: int,
    layout_name: str,
    show_io: bool,
    process_panel: str,
    custom_layout: dict[str, dict[str, str]] | None = None,
    charge_panel: str = "usb",
) -> DashboardLayout:
    layout_name = normalize_layout(layout_name)
    template = LAYOUT_TEMPLATES.get(layout_name, LAYOUT_TEMPLATES["full"])
    custom_layout = sanitize_custom_layout(custom_layout or {})
    graph_h = power_graph_height(layout_name, max_y)
    graph = Rect(2, 0, graph_h, max_x)
    info_y = graph_h + 2
    top_h = 6 if layout_name == "compact" else 7
    left_w = max_x // 2
    right_w = max_x - left_w
    power = Rect(info_y, 0, top_h, left_w)
    thermals = Rect(info_y, left_w, top_h, right_w)
    rects = {"top": graph, "upper_left": power, "upper_right": thermals}

    if layout_name == "focus":
        charge_y = info_y + top_h
        focus_charge_area = Rect(charge_y, 0, max_y - charge_y - 1, max_x)
        rects["main"] = focus_charge_area
        return DashboardLayout(
            panels=panel_placements_from_template(layout_name, template, rects),
        )

    y2 = info_y + top_h
    remaining_h = max_y - y2 - 1
    bottom_left = max_x // 2
    right_w = max_x - bottom_left
    left_io_panel = layout_name in {"full", "compact"} and show_io
    process_left = layout_name == "full" and process_panel == "left"
    process_right = layout_name == "full" and process_panel == "right"
    custom_wants_right_lower = layout_name == "custom"
    custom_wants_right_bottom = layout_name == "custom" and (show_io or "right_bottom" in custom_layout)
    requested_charge_h = BATTERY_PANEL_H if layout_name == "full" and charge_panel == "battery" else CHARGE_PANEL_H

    if layout_name == "compact":
        clocks_h = min(6, remaining_h)
        charge_h = 6 if remaining_h - clocks_h >= 11 else 0
        io_h = 0
    elif remaining_h >= 16:
        clocks_h = 6
        wants_charge = custom_wants_right_lower or remaining_h - clocks_h >= requested_charge_h + MIN_RAM_PANEL_H
        charge_h = requested_charge_h if wants_charge and remaining_h - clocks_h - requested_charge_h >= MIN_RAM_PANEL_H else 0
        need_right_bottom = custom_wants_right_bottom or (show_io and not left_io_panel)
        io_h = IO_PANEL_H if need_right_bottom and remaining_h - clocks_h - charge_h - IO_PANEL_H >= MIN_RAM_PANEL_H else 0
    elif remaining_h >= 10:
        clocks_h = 5
        charge_h = 0
        io_h = 0
    else:
        clocks_h = remaining_h
        charge_h = 0
        io_h = 0

    ram_h = max(0, remaining_h - clocks_h - charge_h - io_h)
    ram_y = y2 + clocks_h
    charge_y = ram_y + ram_h
    io_y = charge_y + charge_h
    usage = Rect(y2, 0, remaining_h, bottom_left)
    clocks = Rect(y2, bottom_left, clocks_h, right_w)
    ram = Rect(ram_y, bottom_left, ram_h, right_w)
    charge = Rect(charge_y, bottom_left, charge_h, right_w) if charge_h else None
    io = Rect(io_y, bottom_left, io_h, right_w) if io_h else None
    rects["lower_left"] = usage
    rects["right_top"] = clocks
    if ram.h >= 3:
        rects["right_middle"] = ram
    if charge is not None:
        rects["right_lower"] = charge
    if io is not None:
        rects["right_bottom"] = io
    return DashboardLayout(
        left_io_panel=left_io_panel,
        panels=panel_placements_from_template(layout_name, template, rects, process_left, process_right, custom_layout),
    )


def thermal_hotspot(sample: MetricSample) -> tuple[float | None, str | None]:
    candidates = physical_temperature_candidates(sample)
    if sample.temp_max_source:
        candidates.append((sample.temp_max_c, sample.temp_max_source))
    return hottest_named_temperature(candidates)


def draw_thermal_rows(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    rows: list[tuple[str, str]],
    sample: MetricSample,
    colors: dict[str, int],
) -> None:
    longest_label = max((len(name) for name, _value in rows), default=0)
    battery_style_gap = max(2, min(10, max(7, w // 3)) - 6)
    value_offset = min(max(1, w - 1), longest_label + battery_style_gap)
    for idx, (name, value) in enumerate(rows[: max(0, h)]):
        label_attr = colors["muted"]
        if name == "Throttle":
            value_attr = colors["bad"]
        elif name == "Perf reason":
            value_attr = colors["muted"]
        else:
            value_attr = color_for_thermal(sample, colors)
        safe_addstr(win, y + idx, x, name[: max(1, value_offset - 1)], label_attr)
        safe_addstr(win, y + idx, x + value_offset, value[: max(1, w - value_offset)], value_attr)


def draw_thermals_panel(
    win: curses.window,
    placement: PanelPlacement,
    sample: MetricSample,
    battery: BatteryStats,
    colors: dict[str, int],
) -> None:
    del battery
    rect = placement.rect
    draw_box(win, rect.y, rect.x, rect.h, rect.w, "THERMALS", color_for_thermal(sample, colors))
    throttle_text = "yes" if sample.throttled else "no" if sample.throttled is False else "unknown"
    state_rows: list[tuple[str, str]] = [
        ("Pressure", sample.thermal_pressure or "n/a"),
        ("Throttled", throttle_text),
    ]
    hot_value, hot_source = thermal_hotspot(sample)
    if hot_value is not None and hot_source is not None:
        if hot_source == "AppleSMC + IOHID":
            hot_source_text = "SMC/IOHID"
        elif rect.w < 57 and hot_source.startswith("CPU@"):
            hot_source_text = hot_source.removeprefix("CPU@")
        else:
            hot_source_text = hot_source
        state_rows.append(("Hot", f"{fmt_temp(hot_value)} · {hot_source_text}"))
    elif (
        is_intel_sample(sample)
        and sample.soc_temp_c is not None
        and sample.temp_max_c is not None
        and math.isclose(sample.soc_temp_c, sample.temp_max_c, abs_tol=0.05)
    ):
        state_rows.append(("CPU temp", fmt_temp(sample.soc_temp_c)))
    else:
        state_rows.extend((("Temp avg", fmt_temp(sample.soc_temp_c)), ("Temp max", fmt_temp(sample.temp_max_c))))
    if sample.fan_rpm is not None:
        state_rows.append(("Fan", f"{sample.fan_rpm:.0f} rpm"))
    if sample.throttle_reasons:
        state_rows.append(("Throttle", ", ".join(sample.throttle_reasons)))
    if sample.performance_limit_reasons:
        state_rows.append(("Perf reason", ", ".join(sample.performance_limit_reasons)))
    sensor_rows = [
        ("PMU avg", fmt_temp(sample.pmu_temp_avg_c)),
        ("Power supply", fmt_temp(sample.power_supply_temp_c)),
        ("Airport", fmt_temp(sample.airport_temp_c)),
        ("Trackpad", fmt_temp(sample.trackpad_temp_c)),
        ("Trackpad act", fmt_temp(sample.trackpad_actuator_temp_c)),
    ]
    sensor_rows = [row for row in sensor_rows if row[1] != "n/a"]
    content_h = max(0, rect.h - 2)
    if sensor_rows and rect.w >= 48:
        columns_w = rect.w - 6
        left_w = max((columns_w + 1) // 2, min(24, columns_w - 20))
        draw_thermal_rows(win, rect.y + 1, rect.x + 2, content_h, left_w, state_rows, sample, colors)
        right_x = rect.x + 4 + left_w
        right_w = max(1, rect.x + rect.w - 2 - right_x)
        draw_thermal_rows(win, rect.y + 1, right_x, content_h, right_w, sensor_rows, sample, colors)
    else:
        draw_thermal_rows(
            win,
            rect.y + 1,
            rect.x + 2,
            content_h,
            rect.w - 4,
            state_rows + sensor_rows,
            sample,
            colors,
        )


def draw_power_panel(
    win: curses.window,
    placement: PanelPlacement,
    sample: MetricSample,
    history: History,
    battery: BatteryStats,
    interval_s: float,
    colors: dict[str, int],
) -> None:
    rect = placement.rect
    draw_power_section(win, rect.y, rect.x, rect.h, rect.w, sample, history, battery, interval_s, colors, placement.detail)


def draw_clocks_panel(
    win: curses.window,
    placement: PanelPlacement,
    sample: MetricSample,
    colors: dict[str, int],
) -> None:
    rect = placement.rect
    draw_box(win, rect.y, rect.x, rect.h, rect.w, "CLOCKS", colors["accent"])
    right_rows = clock_panel_rows(sample)
    max_freq_rows = min(len(right_rows), max(0, rect.h - 2))
    for idx, (name, value) in enumerate(right_rows[:max_freq_rows]):
        yy = rect.y + 1 + idx
        safe_addstr(win, yy, rect.x + 2, name, colors["muted"])
        safe_addstr(win, yy, rect.x + 13, value, colors["fg"])


def draw_charge_panel(
    win: curses.window,
    placement: PanelPlacement,
    charge_panel: str,
    battery: BatteryStats,
    usb_c: UsbCStats,
    colors: dict[str, int],
) -> None:
    rect = placement.rect
    effective_panel = effective_charge_panel(charge_panel, battery, usb_c)
    if effective_panel == "usb":
        draw_hotkey_box(win, rect.y, rect.x, rect.h, rect.w, "POWER INPUT", colors["accent"], colors, "b")
        draw_usb_c_section(win, rect.y + 1, rect.x + 2, rect.h - 2, rect.w - 4, usb_c, colors, placement.detail)
    elif effective_panel == "cable":
        draw_hotkey_box(win, rect.y, rect.x, rect.h, rect.w, "CABLE", colors["accent"], colors, "b")
        draw_cable_section(win, rect.y + 1, rect.x + 2, rect.h - 2, rect.w - 4, usb_c, colors, placement.detail)
    else:
        draw_hotkey_box(win, rect.y, rect.x, rect.h, rect.w, "BATTERY", colors["accent"], colors, "b")
        draw_battery_section(win, rect.y + 1, rect.x + 2, rect.h - 2, rect.w - 4, battery, colors, placement.detail)


def draw_io_panel(
    win: curses.window,
    placement: PanelPlacement,
    io_stats: IoStats,
    history: History,
    colors: dict[str, int],
    upper_io_mode: str,
    lower_io_mode: str,
) -> None:
    rect = placement.rect
    draw_box(win, rect.y, rect.x, rect.h, rect.w, "DISK / NET", colors["accent"])
    if rect.w > 20:
        title_x = rect.x + 13
        safe_addstr(win, rect.y, title_x, "I", colors["warn"] | curses.A_BOLD)
        safe_addstr(win, rect.y, title_x + 1, "/", colors["muted"])
        safe_addstr(win, rect.y, title_x + 2, "O", colors["warn"] | curses.A_BOLD)
    draw_io_mini_graph(
        win,
        rect.y + 1,
        rect.x + 2,
        rect.h - 2,
        rect.w - 4,
        io_stats,
        history,
        colors,
        upper_io_mode,
        lower_io_mode,
        heading=False,
    )


def draw_load_panel(
    win: curses.window,
    placement: PanelPlacement,
    sample: MetricSample,
    history: History,
    colors: dict[str, int],
    load_view: str,
    left_io_panel: bool,
    io_stats: IoStats,
    upper_io_mode: str,
    lower_io_mode: str,
) -> None:
    rect = placement.rect
    draw_usage_matrix(
        win,
        rect.y,
        rect.x,
        rect.h,
        rect.w,
        sample,
        history,
        colors,
        load_view,
        left_io_panel,
        io_stats,
        upper_io_mode,
        lower_io_mode,
    )


def draw_ram_panel(
    win: curses.window,
    placement: PanelPlacement,
    memory: MemoryStats,
    sample: MetricSample,
    colors: dict[str, int],
) -> None:
    rect = placement.rect
    draw_box(win, rect.y, rect.x, rect.h, rect.w, "RAM", colors["accent"])
    draw_memory_section(win, rect.y + 1, rect.x + 2, rect.h - 2, rect.w - 4, memory, sample, colors, placement.detail)


def draw_process_panel(
    win: curses.window,
    placement: PanelPlacement,
    processes: list[ProcessInfo],
    process_sort: str,
    process_selected: int,
    pending_kill_pid: int | None,
    colors: dict[str, int],
) -> None:
    rect = placement.rect
    draw_process_section(win, rect.y, rect.x, rect.h, rect.w, processes, process_sort, process_selected, pending_kill_pid, colors)


def draw_dashboard_panel(
    win: curses.window,
    placement: PanelPlacement,
    sample: MetricSample,
    history: History,
    memory: MemoryStats,
    battery: BatteryStats,
    usb_c: UsbCStats,
    io_stats: IoStats,
    processes: list[ProcessInfo],
    colors: dict[str, int],
    args: argparse.Namespace,
    upper_power_mode: str,
    lower_power_mode: str,
    upper_io_mode: str,
    lower_io_mode: str,
    load_view: str,
    process_sort: str,
    charge_panel: str,
    process_selected: int,
    pending_kill_pid: int | None,
    left_io_panel: bool,
) -> None:
    if placement.rect.h < 3 or placement.rect.w < 8:
        return
    if placement.panel_id == "power_graph":
        draw_split_power_graph(
            win,
            placement.rect.y,
            placement.rect.x,
            placement.rect.h,
            placement.rect.w,
            history,
            sample,
            upper_power_mode,
            lower_power_mode,
            colors,
        )
    elif placement.panel_id == "power":
        draw_power_panel(win, placement, sample, history, battery, args.interval, colors)
    elif placement.panel_id == "thermals":
        draw_thermals_panel(win, placement, sample, battery, colors)
    elif placement.panel_id == "load":
        draw_load_panel(win, placement, sample, history, colors, load_view, left_io_panel, io_stats, upper_io_mode, lower_io_mode)
    elif placement.panel_id == "clocks":
        draw_clocks_panel(win, placement, sample, colors)
    elif placement.panel_id == "ram":
        draw_ram_panel(win, placement, memory, sample, colors)
    elif placement.panel_id == "charge":
        draw_charge_panel(win, placement, charge_panel, battery, usb_c, colors)
    elif placement.panel_id == "io":
        draw_io_panel(win, placement, io_stats, history, colors, upper_io_mode, lower_io_mode)
    elif placement.panel_id == "process":
        draw_process_panel(win, placement, processes, process_sort, process_selected, pending_kill_pid, colors)


def draw_dashboard(
    stdscr: curses.window,
    sample: MetricSample | None,
    history: History,
    memory: MemoryStats,
    battery: BatteryStats,
    usb_c: UsbCStats,
    io_stats: IoStats,
    processes: list[ProcessInfo],
    colors: dict[str, int],
    args: argparse.Namespace,
    status: str,
    upper_power_mode: str,
    lower_power_mode: str,
    upper_io_mode: str,
    lower_io_mode: str,
    load_view: str,
    process_panel: str,
    process_sort: str,
    charge_panel: str,
    process_selected: int,
    pending_kill_pid: int | None,
    side_warnings: Iterable[str],
    help_visible: bool,
    menu_visible: bool,
    menu_selected: int,
    tailor_visible: bool,
    tailor_menu_visible: bool,
    tailor_menu_selected: int,
    tailor_name_buffer: str,
    tailor_name_editing: bool,
) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    if max_y < 24 or max_x < 72:
        safe_addstr(stdscr, 0, 0, f"{APP_NAME}: terminal too small; try at least 72x24", colors["warn"])
        stdscr.refresh()
        return

    draw_header(stdscr, args, colors)
    if sample is None:
        logo_y = max(3, (max_y - len(LOGO_LINES)) // 2 - 2)
        drawn_h = draw_logo(stdscr, logo_y, 0, max_x, colors)
        wait_text = "waiting for first powermetrics sample"
        safe_addstr(stdscr, logo_y + drawn_h + 2, max(1, (max_x - len(wait_text)) // 2), wait_text, colors["muted"])
        draw_modal_overlays(
            stdscr,
            colors,
            args,
            help_visible,
            menu_visible,
            menu_selected,
            upper_power_mode,
            lower_power_mode,
            upper_io_mode,
            lower_io_mode,
            load_view,
            process_panel,
            process_sort,
            charge_panel,
            sample,
        )
        stdscr.refresh()
        return

    sample = sample or MetricSample(warning="waiting for powermetrics sample")
    alerts = build_alerts(sample, memory, battery, args)
    status_attr = colors["bad"] | curses.A_BOLD if any(alert in ("THROTTLE",) for alert in alerts) else colors["warn"] if alerts else colors["muted"]
    source_text = source_status(args, sample, memory, battery, usb_c, io_stats, processes, process_panel, side_warnings)
    status_text = f"{status}  {source_text}"
    if alerts:
        status_text += f"  ALERT {' | '.join(alerts)}"
    if status_text:
        safe_addstr(stdscr, 1, 1, status_text[: max_x - 2], status_attr)

    active_charge_panel = effective_charge_panel(charge_panel, battery, usb_c)
    layout = dashboard_layout(
        max_y,
        max_x,
        args.layout,
        args.show_io,
        process_panel,
        getattr(args, "custom_layout", {}),
        active_charge_panel,
    )
    for slot_id in ("top", "upper_left", "upper_right"):
        placement = layout.slot(slot_id)
        if placement is not None:
            draw_dashboard_panel(
                stdscr,
                placement,
                sample,
                history,
                memory,
                battery,
                usb_c,
                io_stats,
                processes,
                colors,
                args,
                upper_power_mode,
                lower_power_mode,
                upper_io_mode,
                lower_io_mode,
                load_view,
                process_sort,
                charge_panel,
                process_selected,
                pending_kill_pid,
                False,
            )

    if args.layout == "focus":
        focus_charge = layout.slot("main")
        assert focus_charge is not None
        battery_y = focus_charge.rect.y
        battery_h = focus_charge.rect.h
        if battery_h >= 5:
            if max_x >= 150:
                first_w = max_x // 3
                second_w = max_x // 3
                third_x = first_w + second_w
                draw_hotkey_box(stdscr, battery_y, 0, battery_h, first_w, "BATTERY", colors["accent"], colors, "b")
                draw_hotkey_box(stdscr, battery_y, first_w, battery_h, second_w, "POWER INPUT", colors["accent"], colors, "b")
                draw_hotkey_box(stdscr, battery_y, third_x, battery_h, max_x - third_x, "CABLE", colors["accent"], colors, "b")
                draw_battery_section(stdscr, battery_y + 1, 2, battery_h - 2, first_w - 4, battery, colors)
                draw_usb_c_section(stdscr, battery_y + 1, first_w + 2, battery_h - 2, second_w - 4, usb_c, colors)
                draw_cable_section(stdscr, battery_y + 1, third_x + 2, battery_h - 2, max_x - third_x - 4, usb_c, colors)
            elif max_x >= 100:
                half = max_x // 2
                draw_hotkey_box(stdscr, battery_y, 0, battery_h, half, "BATTERY", colors["accent"], colors, "b")
                draw_battery_section(stdscr, battery_y + 1, 2, battery_h - 2, half - 4, battery, colors)
                if active_charge_panel == "cable":
                    draw_hotkey_box(stdscr, battery_y, half, battery_h, max_x - half, "CABLE", colors["accent"], colors, "b")
                    draw_cable_section(stdscr, battery_y + 1, half + 2, battery_h - 2, max_x - half - 4, usb_c, colors)
                else:
                    draw_hotkey_box(stdscr, battery_y, half, battery_h, max_x - half, "POWER INPUT", colors["accent"], colors, "b")
                    draw_usb_c_section(stdscr, battery_y + 1, half + 2, battery_h - 2, max_x - half - 4, usb_c, colors)
            elif battery_h >= 10:
                upper_h = max(3, battery_h // 2)
                lower_h = battery_h - upper_h
                draw_hotkey_box(stdscr, battery_y, 0, upper_h, max_x, "BATTERY", colors["accent"], colors, "b")
                draw_battery_section(stdscr, battery_y + 1, 2, upper_h - 2, max_x - 4, battery, colors)
                if active_charge_panel == "cable":
                    draw_hotkey_box(stdscr, battery_y + upper_h, 0, lower_h, max_x, "CABLE", colors["accent"], colors, "b")
                    draw_cable_section(stdscr, battery_y + upper_h + 1, 2, lower_h - 2, max_x - 4, usb_c, colors)
                else:
                    draw_hotkey_box(stdscr, battery_y + upper_h, 0, lower_h, max_x, "POWER INPUT", colors["accent"], colors, "b")
                    draw_usb_c_section(stdscr, battery_y + upper_h + 1, 2, lower_h - 2, max_x - 4, usb_c, colors)
            else:
                if active_charge_panel == "usb":
                    draw_hotkey_box(stdscr, battery_y, 0, battery_h, max_x, "POWER INPUT", colors["accent"], colors, "b")
                    draw_usb_c_section(stdscr, battery_y + 1, 2, battery_h - 2, max_x - 4, usb_c, colors)
                elif active_charge_panel == "cable":
                    draw_hotkey_box(stdscr, battery_y, 0, battery_h, max_x, "CABLE", colors["accent"], colors, "b")
                    draw_cable_section(stdscr, battery_y + 1, 2, battery_h - 2, max_x - 4, usb_c, colors)
                else:
                    draw_hotkey_box(stdscr, battery_y, 0, battery_h, max_x, "BATTERY", colors["accent"], colors, "b")
                    draw_battery_section(stdscr, battery_y + 1, 2, battery_h - 2, max_x - 4, battery, colors)
        if sample.warning:
            safe_addstr(stdscr, max_y - 1, 1, sample.warning[: max_x - 2], colors["warn"])
        draw_modal_overlays(
            stdscr,
            colors,
            args,
            help_visible,
            menu_visible,
            menu_selected,
            upper_power_mode,
            lower_power_mode,
            upper_io_mode,
            lower_io_mode,
            load_view,
            process_panel,
            process_sort,
            charge_panel,
            sample,
        )
        stdscr.refresh()
        return

    for slot_id in ("lower_left", "right_top", "right_middle", "right_lower", "right_bottom"):
        placement = layout.slot(slot_id)
        if placement is None:
            continue
        draw_dashboard_panel(
            stdscr,
            placement,
            sample,
            history,
            memory,
            battery,
            usb_c,
            io_stats,
            processes,
            colors,
            args,
            upper_power_mode,
            lower_power_mode,
            upper_io_mode,
            lower_io_mode,
            load_view,
            process_sort,
            charge_panel,
            process_selected,
            pending_kill_pid,
            layout.left_io_panel and placement.slot == "lower_left" and placement.panel_id == "load",
        )

    if sample.warning:
        safe_addstr(stdscr, max_y - 1, 1, sample.warning[: max_x - 2], colors["warn"])
    if tailor_visible and args.layout == "custom":
        draw_tailor_overlay(
            stdscr,
            layout,
            colors,
            args,
            tailor_menu_visible,
            tailor_menu_selected,
            tailor_name_buffer,
            tailor_name_editing,
        )
    draw_modal_overlays(
        stdscr,
        colors,
        args,
        help_visible,
        menu_visible,
        menu_selected,
        upper_power_mode,
        lower_power_mode,
        upper_io_mode,
        lower_io_mode,
        load_view,
        process_panel,
        process_sort,
        charge_panel,
        sample,
    )
    stdscr.refresh()


def run_curses(stdscr: curses.window, args: argparse.Namespace) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    stdscr.nodelay(True)
    stdscr.timeout(200)
    colors = init_colors(args.theme)
    history = History(args.history)
    sample_queue: queue.Queue[MetricSample] = queue.Queue()
    stream_events: queue.Queue[tuple[int, str]] = queue.Queue()
    side_queue: queue.Queue[SideMetricsUpdate] = queue.Queue()
    side_stop = threading.Event()
    side_poll_state = SideMetricsPollState()
    sudo_keeper = SudoKeeper()
    stream: MockStream | PowerMetricsStream
    worker: threading.Thread
    latest: MetricSample | None = None
    freq_cache: dict[str, float] = {}
    memory_stats = MemoryStats()
    battery_stats = BatteryStats()
    usb_c_stats = UsbCStats()
    processes: list[ProcessInfo] = []
    process_gpu_pcts: dict[int, float] = {}
    io_stats = IoStats()
    system_cpu_usage_pct: float | None = None
    side_warnings: dict[str, str] = {}
    upper_power_mode = args.upper_power_mode if args.upper_power_mode in POWER_MODES else "soc"
    lower_power_mode = args.lower_power_mode if args.lower_power_mode in POWER_MODES else "cpu"
    upper_io_mode = args.upper_io_mode if args.upper_io_mode in IO_MODES else "disk_read"
    lower_io_mode = args.lower_io_mode if args.lower_io_mode in IO_MODES else "net_in"
    load_view = args.load_view if args.load_view in LOAD_VIEWS else "rows"
    process_panel = args.process_panel if args.process_panel in PROCESS_PANEL_MODES else "hidden"
    process_sort = args.process_sort if args.process_sort in PROCESS_SORTS else "cpu"
    charge_panel = args.charge_panel if args.charge_panel in CHARGE_PANEL_MODES else "battery"
    args.custom_slot = args.custom_slot if getattr(args, "custom_slot", None) in CUSTOM_SLOT_IDS else CUSTOM_SLOT_IDS[0]
    args.custom_layout = sanitize_custom_layout(getattr(args, "custom_layout", {}))
    args.custom_name = clean_custom_name(getattr(args, "custom_name", ""))
    process_selected = 0
    pending_kill: PendingKill | None = None
    help_visible = False
    menu_visible = False
    menu_selected = 0
    tailor_visible = False
    tailor_menu_visible = False
    tailor_menu_selected = 0
    tailor_name_editing = False
    tailor_name_buffer = ""
    status = "starting sampler"
    stream_generation = 0
    terminal_size = (0, 0)
    sampler_retry = SamplerRetryState()
    sampler_restart_pending = False
    last_good_sample_at = 0.0
    stream_started_at = time.monotonic()

    def save_ui_settings() -> bool:
        nonlocal status
        args.process_panel = process_panel
        args.process_sort = process_sort
        args.upper_power_mode = upper_power_mode
        args.lower_power_mode = lower_power_mode
        args.upper_io_mode = upper_io_mode
        args.lower_io_mode = lower_io_mode
        args.load_view = load_view
        args.charge_panel = charge_panel
        error = save_settings(args)
        if error:
            status = f"settings not saved: {error}"
            return False
        return True

    def io_poll_visible() -> bool:
        max_y, max_x = terminal_size
        if max_y <= 0 or max_x <= 0:
            return layout_uses_io(args)
        active_charge_panel = effective_charge_panel(charge_panel, battery_stats, usb_c_stats)
        layout = dashboard_layout(
            max_y,
            max_x,
            args.layout,
            args.show_io,
            process_panel,
            args.custom_layout,
            active_charge_panel,
        )
        return layout.left_io_panel or any(
            placement.panel_id == "io" and placement.rect.h >= 5 and placement.rect.w >= 20 for placement in layout.panels
        )

    def process_poll_visible() -> bool:
        max_y, max_x = terminal_size
        if max_y <= 0 or max_x <= 0:
            return layout_uses_process(args, process_panel)
        active_charge_panel = effective_charge_panel(charge_panel, battery_stats, usb_c_stats)
        layout = dashboard_layout(
            max_y,
            max_x,
            args.layout,
            args.show_io,
            process_panel,
            args.custom_layout,
            active_charge_panel,
        )
        return any(
            placement.panel_id == "process" and placement.rect.h >= 5 and placement.rect.w >= 32 for placement in layout.panels
        )

    def update_side_polling() -> None:
        side_poll_state.update(io_poll_visible(), process_poll_visible(), args.interval)

    def set_custom_slot_config(panel_id: str | None = None, detail: str | None = None) -> None:
        slot_id = args.custom_slot if args.custom_slot in CUSTOM_SLOT_IDS else CUSTOM_SLOT_IDS[0]
        custom_layout = sanitize_custom_layout(getattr(args, "custom_layout", {}))
        config = custom_slot_config(custom_layout, slot_id)
        if panel_id is not None:
            config["panel"] = panel_id
            config["detail"] = default_detail_for_panel(panel_id)
        if detail is not None:
            config["detail"] = detail if detail in detail_levels_for_panel(config["panel"]) else default_detail_for_panel(config["panel"])
        custom_layout[slot_id] = config
        args.custom_layout = custom_layout
        args.layout = "custom"

    def apply_tailor_change(delta: int) -> None:
        nonlocal io_stats, processes, tailor_menu_selected, status
        item_id = TAILOR_MENU_ITEMS[tailor_menu_selected]
        step = 1 if delta >= 0 else -1
        if item_id == "slot":
            args.custom_slot = cycle_value(CUSTOM_SLOT_IDS, args.custom_slot, step)
        elif item_id == "panel":
            current = custom_slot_config(args.custom_layout, args.custom_slot)["panel"]
            set_custom_slot_config(panel_id=cycle_value(CUSTOM_PANEL_IDS, current, step))
            if not process_poll_visible():
                processes = []
            if not io_poll_visible():
                io_stats = IoStats()
        elif item_id == "detail":
            config = custom_slot_config(args.custom_layout, args.custom_slot)
            set_custom_slot_config(detail=cycle_value(detail_levels_for_panel(config["panel"]), config["detail"], step))
        status = f"tailor {item_id} {tailor_menu_value_text(item_id, args)}"
        update_side_polling()
        save_ui_settings()

    def apply_menu_change(delta: int) -> None:
        nonlocal upper_power_mode, lower_power_mode, upper_io_mode, lower_io_mode, load_view
        nonlocal io_stats, processes
        nonlocal process_panel, process_sort, charge_panel, colors, stream, worker, status
        item_id = MENU_ITEMS[menu_selected][0]
        step = 1 if delta >= 0 else -1
        if item_id == "theme":
            names = tuple(THEMES)
            args.theme = cycle_value(names, args.theme, step)
            colors = init_colors(args.theme)
        elif item_id == "layout":
            args.layout = cycle_value(LAYOUTS, args.layout, step)
            if not process_poll_visible():
                processes = []
            if not io_poll_visible():
                io_stats = IoStats()
        elif item_id == "interval":
            if step > 0:
                args.interval = round(min(MAX_INTERVAL, args.interval + interval_step(args.interval)), 1)
            else:
                args.interval = round(max(MIN_INTERVAL, args.interval - interval_step(args.interval)), 1)
            restart_stream()
        elif item_id == "show_io":
            args.show_io = not args.show_io
            if not args.show_io:
                io_stats = IoStats()
        elif item_id == "upper_power":
            upper_power_mode = cycle_value(POWER_MODES, upper_power_mode, step)
        elif item_id == "lower_power":
            lower_power_mode = cycle_value(POWER_MODES, lower_power_mode, step)
        elif item_id == "upper_io":
            upper_io_mode = cycle_value(IO_MODES, upper_io_mode, step)
        elif item_id == "lower_io":
            lower_io_mode = cycle_value(IO_MODES, lower_io_mode, step)
        elif item_id == "load_view":
            load_view = cycle_value(LOAD_VIEWS, load_view, step)
        elif item_id == "process_panel":
            process_panel = cycle_value(PROCESS_PANEL_MODES, process_panel, step)
            if not process_poll_visible():
                processes = []
        elif item_id == "process_sort":
            process_sort = cycle_value(PROCESS_SORTS, process_sort, step)
        elif item_id == "charge_panel":
            charge_panel = cycle_value(CHARGE_PANEL_MODES, charge_panel, step)
        elif item_id == "allow_root_kill":
            args.allow_root_kill = not bool(getattr(args, "allow_root_kill", False))
        elif item_id == "alert_temp":
            args.alert_temp_c = round(clamp(float(args.alert_temp_c) + step * 1.0, 40.0, 125.0), 1)
        elif item_id == "alert_swap":
            args.alert_swap_gib = round(clamp(float(args.alert_swap_gib) + step * 0.25, 0.0, 1024.0), 2)
        elif item_id == "alert_battery":
            args.alert_battery_drain_w = round(clamp(float(args.alert_battery_drain_w) + step * 1.0, 0.0, 250.0), 1)
        status = (
            f"menu {MENU_ITEMS[menu_selected][1]} "
            f"{menu_value_text(item_id, args, upper_power_mode, lower_power_mode, upper_io_mode, lower_io_mode, load_view, process_panel, process_sort, charge_panel, latest)}"
        )
        update_side_polling()
        save_ui_settings()

    def make_stream() -> MockStream | PowerMetricsStream:
        return MockStream(int(args.interval * 1000)) if args.mock else PowerMetricsStream(int(args.interval * 1000))

    def pump(local_stream: MockStream | PowerMetricsStream, generation: int) -> None:
        try:
            for next_sample in local_stream.samples():
                if generation == stream_generation:
                    sample_queue.put(next_sample)
        except Exception as exc:
            if generation == stream_generation:
                sample_queue.put(MetricSample(warning=f"sampler stopped: {exc}"))
        finally:
            stream_events.put((generation, "stopped"))

    def start_stream() -> tuple[MockStream | PowerMetricsStream, threading.Thread]:
        local_stream = make_stream()
        local_worker = threading.Thread(target=pump, args=(local_stream, stream_generation), daemon=True)
        local_worker.start()
        return local_stream, local_worker

    def restart_stream(manual: bool = True) -> None:
        nonlocal sample_queue, stream, worker, stream_generation, sampler_restart_pending, stream_started_at
        stream_generation += 1
        stream.stop()
        if worker.is_alive():
            worker.join(timeout=2.0)
        sample_queue = queue.Queue()
        sampler_restart_pending = False
        if manual:
            sampler_retry.reset()
        stream, worker = start_stream()
        stream_started_at = time.monotonic()

    stream, worker = start_stream()
    update_side_polling()
    if not args.mock:
        sudo_keeper.start()
    side_worker = threading.Thread(target=side_metrics_worker, args=(side_queue, side_stop, side_poll_state, args.mock), daemon=True)
    side_worker.start()
    process_gpu_worker = threading.Thread(
        target=process_gpu_metrics_worker,
        args=(side_queue, side_stop, side_poll_state, args.mock),
        daemon=True,
    )
    process_gpu_worker.start()
    themes = list(THEMES)
    layouts = list(LAYOUTS)

    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            terminal_size = (max_y, max_x)
            update_side_polling()
            if sudo_keeper.last_error:
                status = f"sudo refresh failed: {sudo_keeper.last_error}"
            history.resize(max(args.history, max_x * 2, int(30.0 / MIN_INTERVAL) + 1))
            while True:
                try:
                    next_sample = sample_queue.get_nowait()
                    if not powermetrics_sample_is_usable(next_sample):
                        detail = next_sample.warning or "powermetrics sample contained no usable telemetry"
                        status = f"sampler warning: {detail.splitlines()[0]}"
                        continue
                    latest = next_sample
                    apply_system_cpu_usage(latest, system_cpu_usage_pct)
                    keep_last_nonzero_frequencies(latest, freq_cache)
                    last_good_sample_at = time.monotonic()
                    sampler_retry.reset()
                    history.add(latest, now=last_good_sample_at)
                    age = time.strftime("%H:%M:%S", time.localtime(latest.timestamp))
                    status = f"last sample {age}" + ("; partial data" if latest.warning else "")
                except queue.Empty:
                    break
            while True:
                try:
                    generation, event = stream_events.get_nowait()
                    if generation == stream_generation and event == "stopped" and not sampler_restart_pending:
                        delay = sampler_retry.schedule()
                        sampler_restart_pending = True
                        status = f"sampler stopped; restarting in {delay:.1f}s"
                except queue.Empty:
                    break
            now = time.monotonic()
            stale_after = max(5.0, args.interval * 3.0)
            stale_age = now - max(last_good_sample_at, stream_started_at)
            if not sampler_restart_pending and stale_age > stale_after:
                delay = sampler_retry.schedule(now)
                sampler_restart_pending = True
                status = f"sampler stale; restarting in {delay:.1f}s"
            if sampler_restart_pending and sampler_retry.ready(now):
                status = "restarting sampler"
                restart_stream(manual=False)
            elif stale_age > stale_after and not sampler_restart_pending:
                status = f"sample stale ({stale_age:.0f}s)"
            while True:
                try:
                    update = side_queue.get_nowait()
                    if update.memory is not None:
                        memory_stats = update.memory
                    if update.battery is not None:
                        battery_stats = update.battery
                    if update.usb_c is not None:
                        usb_c_stats = update.usb_c
                    if update.cpu_usage_pct is not None:
                        system_cpu_usage_pct = update.cpu_usage_pct
                        if latest is not None:
                            if apply_system_cpu_usage(latest, system_cpu_usage_pct):
                                history.update_latest_cpu_usage(current_cpu_usage(latest))
                    if update.io_stats is not None:
                        io_stats = update.io_stats
                        history.add_io(io_stats)
                    if update.processes is not None:
                        processes = update.processes
                        merge_process_gpu_pcts(processes, process_gpu_pcts)
                        process_selected = min(process_selected, max(0, len(processes) - 1))
                    if update.process_gpu_pcts is not None:
                        process_gpu_pcts = update.process_gpu_pcts
                        merge_process_gpu_pcts(processes, process_gpu_pcts)
                    for source in update.recovered:
                        side_warnings.pop(source, None)
                    for warning in update.warnings:
                        source = warning.split(":", 1)[0]
                        side_warnings[source] = warning
                except queue.Empty:
                    break

            draw_dashboard(
                stdscr,
                latest,
                history,
                memory_stats,
                battery_stats,
                usb_c_stats,
                io_stats,
                processes,
                colors,
                args,
                status,
                upper_power_mode,
                lower_power_mode,
                upper_io_mode,
                lower_io_mode,
                load_view,
                process_panel,
                process_sort,
                charge_panel,
                process_selected,
                pending_kill.pid if pending_kill else None,
                tuple(side_warnings.values()),
                help_visible,
                menu_visible,
                menu_selected,
                tailor_visible,
                tailor_menu_visible,
                tailor_menu_selected,
                tailor_name_buffer,
                tailor_name_editing,
            )
            key = stdscr.getch()
            if tailor_name_editing:
                if key == -1:
                    continue
                if key in (27,):
                    tailor_name_editing = False
                    tailor_name_buffer = ""
                    status = "tailor name cancelled"
                    continue
                if key in (curses.KEY_ENTER, 10, 13):
                    cleaned = clean_custom_name(tailor_name_buffer)
                    error = custom_name_error(cleaned)
                    if error is not None:
                        status = f"tailor name rejected: {error}"
                    else:
                        args.custom_name = cleaned
                        status = f"tailor name {args.custom_name}"
                        save_ui_settings()
                    tailor_name_editing = False
                    tailor_name_buffer = ""
                    continue
                if key in (curses.KEY_BACKSPACE, 8, 127):
                    tailor_name_buffer = tailor_name_buffer[:-1]
                    continue
                if 32 <= key <= 126 and len(tailor_name_buffer) < 32:
                    tailor_name_buffer += chr(key)
                continue
            if key in (ord("q"), ord("Q"), 27):
                if tailor_menu_visible or tailor_visible:
                    tailor_menu_visible = False
                    tailor_visible = False
                    continue
                if menu_visible or help_visible:
                    menu_visible = False
                    help_visible = False
                    continue
                break
            if tailor_visible:
                slot_id = tailor_slot_for_key(key)
                if slot_id is not None:
                    args.custom_slot = slot_id
                    tailor_menu_visible = True
                    tailor_menu_selected = 0
                    status = f"tailor slot {tailor_slot_number(slot_id)} {CUSTOM_SLOT_LABELS.get(slot_id, slot_id)}"
                    continue
                if key in (ord("T"),):
                    tailor_visible = False
                    tailor_menu_visible = False
                    status = "tailor closed"
                    continue
                if tailor_menu_visible:
                    if key in (curses.KEY_UP,):
                        tailor_menu_selected = max(0, tailor_menu_selected - 1)
                    elif key in (curses.KEY_DOWN, ord("\t")):
                        tailor_menu_selected = (tailor_menu_selected + 1) % len(TAILOR_MENU_ITEMS)
                    elif key == curses.KEY_LEFT:
                        apply_tailor_change(-1)
                    elif key == curses.KEY_RIGHT:
                        apply_tailor_change(1)
                    elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
                        if TAILOR_MENU_ITEMS[tailor_menu_selected] == "name":
                            tailor_name_buffer = clean_custom_name(getattr(args, "custom_name", ""))
                            tailor_name_editing = True
                            status = "tailor name editing"
                        else:
                            apply_tailor_change(1)
                    continue
                if key in (curses.KEY_ENTER, 10, 13, ord(" ")):
                    tailor_menu_visible = True
                    continue
            if menu_visible:
                if key in (curses.KEY_UP,):
                    menu_selected = max(0, menu_selected - 1)
                elif key in (curses.KEY_DOWN, ord("\t")):
                    menu_selected = (menu_selected + 1) % len(MENU_ITEMS)
                elif key == curses.KEY_LEFT:
                    apply_menu_change(-1)
                elif key in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13, ord(" ")):
                    apply_menu_change(1)
                elif key in (ord("s"), ord("S")):
                    if save_ui_settings():
                        status = "settings saved"
                elif key in (ord("m"), ord("M")):
                    menu_visible = False
                continue
            if pending_kill is not None and time.monotonic() > pending_kill.until:
                pending_kill = None
            if key in (ord("+"), ord("=")):
                args.interval = round(max(MIN_INTERVAL, args.interval - interval_step(args.interval)), 1)
                status = f"interval {interval_text(args.interval)} applied"
                restart_stream()
                save_ui_settings()
            elif key in (ord("?"), ord("h"), ord("H")):
                help_visible = not help_visible
            elif key in (ord("m"), ord("M")):
                menu_visible = not menu_visible
                help_visible = False
            elif key in (ord("-"), ord("_")):
                args.interval = round(min(MAX_INTERVAL, args.interval + interval_step(args.interval)), 1)
                status = f"interval {interval_text(args.interval)} applied"
                restart_stream()
                save_ui_settings()
            elif key in (ord("r"), ord("R")):
                history.clear_power()
                status = "power peaks/history reset"
            elif key == ord("t"):
                index = (themes.index(args.theme) + 1) % len(themes)
                args.theme = themes[index]
                colors = init_colors(args.theme)
                save_ui_settings()
            elif key == ord("T"):
                if args.layout == "custom":
                    tailor_visible = not tailor_visible
                    tailor_menu_visible = False
                    help_visible = False
                    menu_visible = False
                    status = "tailor open" if tailor_visible else "tailor closed"
                else:
                    status = "tailor needs custom layout"
            elif key in (ord("v"), ord("V")):
                index = (layouts.index(args.layout) + 1) % len(layouts)
                args.layout = layouts[index]
                if args.layout != "custom":
                    tailor_visible = False
                    tailor_menu_visible = False
                status = f"layout {args.layout}"
                if not process_poll_visible():
                    processes = []
                if not io_poll_visible():
                    io_stats = IoStats()
                update_side_polling()
                save_ui_settings()
            elif key in (ord("d"), ord("D")):
                args.show_io = not args.show_io
                status = "disk/net shown" if args.show_io else "disk/net hidden"
                io_stats = IoStats()
                update_side_polling()
                save_ui_settings()
            elif key in (ord("l"), ord("L")):
                load_view = "graph" if load_view == "rows" else "rows"
                save_ui_settings()
            elif key in (ord("b"), ord("B")):
                charge_panel = cycle_value(CHARGE_PANEL_MODES, charge_panel, 1)
                status = f"charge panel {'power input' if charge_panel == 'usb' else charge_panel}"
                save_ui_settings()
            elif key in (ord("i"), ord("I")):
                index = (IO_MODES.index(upper_io_mode) + 1) % len(IO_MODES)
                upper_io_mode = IO_MODES[index]
                status = f"io upper {IO_MODE_LABELS[upper_io_mode]}"
                save_ui_settings()
            elif key in (ord("o"), ord("O")):
                index = (IO_MODES.index(lower_io_mode) + 1) % len(IO_MODES)
                lower_io_mode = IO_MODES[index]
                status = f"io lower {IO_MODE_LABELS[lower_io_mode]}"
                save_ui_settings()
            elif key == curses.KEY_UP:
                if process_poll_visible():
                    process_selected = max(0, process_selected - 1)
                    pending_kill = None
            elif key == curses.KEY_DOWN:
                if process_poll_visible():
                    process_selected = min(max(0, len(processes) - 1), process_selected + 1)
                    pending_kill = None
            elif key == curses.KEY_LEFT:
                if process_poll_visible():
                    process_sort = cycle_value(PROCESS_SORTS, process_sort, -1)
                    process_selected = 0
                    pending_kill = None
                    status = f"process sort {process_sort}"
                    save_ui_settings()
            elif key == curses.KEY_RIGHT:
                if process_poll_visible():
                    process_sort = cycle_value(PROCESS_SORTS, process_sort, 1)
                    process_selected = 0
                    pending_kill = None
                    status = f"process sort {process_sort}"
                    save_ui_settings()
            elif key in (ord("k"), ord("K")):
                if process_poll_visible():
                    ordered = sorted_processes(processes, process_sort)
                    if 0 <= process_selected < len(ordered):
                        target = ordered[process_selected]
                        now = time.monotonic()
                        root_kill_blocked = is_root_process() and not bool(getattr(args, "allow_root_kill", False))
                        protected_pid = target.pid in {1, os.getpid()}
                        if protected_pid:
                            status = f"process {target.pid} is protected"
                            pending_kill = None
                        elif root_kill_blocked:
                            status = "root process kill disabled in settings"
                            pending_kill = None
                        elif pending_kill is not None and pending_kill.pid == target.pid and now <= pending_kill.until:
                            fresh = read_process_for_kill(target.pid)
                            if fresh is None:
                                status = f"process {target.pid} already exited"
                                pending_kill = None
                                continue
                            if not pending_kill.matches(fresh):
                                status = f"process {target.pid} changed; kill cancelled"
                                pending_kill = None
                                processes = read_processes()
                                continue
                            try:
                                os.kill(target.pid, signal.SIGTERM)
                                status = f"sent TERM to {target.pid} {target.command}"
                            except PermissionError:
                                status = f"cannot kill {target.pid}: permission denied"
                            except ProcessLookupError:
                                status = f"process {target.pid} already exited"
                            except Exception as exc:
                                status = f"kill failed: {exc}"
                            pending_kill = None
                        else:
                            fresh = read_process_for_kill(target.pid)
                            if fresh is None:
                                status = f"process {target.pid} already exited"
                                pending_kill = None
                                continue
                            if not process_identity_matches(target, fresh):
                                status = f"process {target.pid} changed; kill cancelled"
                                pending_kill = None
                                processes = read_processes()
                                continue
                            pending_kill = PendingKill.from_process(fresh, now + KILL_CONFIRM_SECONDS)
                            status = f"press k again to TERM {fresh.pid} {fresh.command}"
            elif key == ord("s"):
                lower_power_mode = "soc"
                save_ui_settings()
            elif key == ord("c"):
                lower_power_mode = "cpu"
                save_ui_settings()
            elif key == ord("g"):
                lower_power_mode = "gpu"
                save_ui_settings()
            elif key == ord("a"):
                lower_power_mode = "ane"
                save_ui_settings()
            elif key == ord("S"):
                upper_power_mode = "soc"
                save_ui_settings()
            elif key == ord("C"):
                upper_power_mode = "cpu"
                save_ui_settings()
            elif key == ord("G"):
                upper_power_mode = "gpu"
                save_ui_settings()
            elif key == ord("A"):
                upper_power_mode = "ane"
                save_ui_settings()
            elif key in (ord("p"), ord("P")):
                process_panel = cycle_value(PROCESS_PANEL_MODES, process_panel, 1)
                process_selected = min(process_selected, max(0, len(processes) - 1))
                pending_kill = None
                status = f"process panel {process_panel}"
                if not process_poll_visible():
                    processes = []
                update_side_polling()
                save_ui_settings()
            elif key == ord("n"):
                index = (POWER_MODES.index(lower_power_mode) + 1) % len(POWER_MODES)
                lower_power_mode = POWER_MODES[index]
                save_ui_settings()
            elif key == ord("u"):
                index = (POWER_MODES.index(upper_power_mode) + 1) % len(POWER_MODES)
                upper_power_mode = POWER_MODES[index]
                save_ui_settings()
    finally:
        save_ui_settings()
        side_stop.set()
        stream.stop()
        sudo_keeper.stop()
        if worker.is_alive():
            worker.join(timeout=2.0)
        side_worker.join(timeout=2.5)
        process_gpu_worker.join(timeout=2.5)


def run_probe(args: argparse.Namespace) -> int:
    ensure_powermetrics_access(args)
    if args.mock:
        sample = next(MockStream(int(args.interval * 1000)).samples())
        print_sample(sample)
        return 0

    previous_cpu: CpuLoadSnapshot | None = None
    try:
        previous_cpu = read_cpu_load_snapshot()
    except Exception:
        previous_cpu = None
    cmd = powermetrics_command(int(args.interval * 1000), "1")
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=max(5.0, args.interval + 4.0),
            env=system_environment(),
        )
    except subprocess.TimeoutExpired:
        print_console("powermetrics timed out while collecting a probe sample", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "ignore") or proc.stdout.decode("utf-8", "ignore")
        print_console(detail, file=sys.stderr)
        return proc.returncode
    raw = proc.stdout.split(b"\0", 1)[0].strip()
    if not raw:
        print_console("powermetrics returned an empty probe sample", file=sys.stderr)
        return 1
    try:
        obj = plistlib.loads(raw)
    except Exception as exc:
        print_console(f"powermetrics plist parse failed: {exc}", file=sys.stderr)
        return 1
    if not isinstance(obj, dict):
        print_console("powermetrics did not return a plist dictionary", file=sys.stderr)
        return 1
    sample = sample_from_plist(obj, interval_s=args.interval)
    if not powermetrics_sample_is_usable(sample):
        print_console(sample.warning or "powermetrics sample contained no usable telemetry", file=sys.stderr)
        return 1
    if is_intel_sample(sample):
        try:
            apply_system_cpu_usage(sample, cpu_usage_from_snapshots(previous_cpu, read_cpu_load_snapshot()))
        except Exception:
            pass
    if not is_intel_sample(sample) or sample.soc_temp_c is None or sample.temp_max_c is None:
        apply_hid_temperatures(sample)
    print_sample(sample)
    if args.raw:
        print_console("\nFlattened powermetrics keys and values (not anonymized):")
        for path, value in sorted(flatten(obj), key=lambda item: item[0].lower()):
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            print_console(f"{path}: {value}")
    return 0


def print_sample(sample: MetricSample) -> None:
    print_console(f"CPU power:    {fmt_power(sample.cpu_power_mw)}")
    print_console(f"GPU power:    {fmt_power(sample.gpu_power_mw)}")
    print_console(f"ANE/NPU power:{fmt_power(sample.ane_power_mw):>10}")
    print_console(f"Media power:  {fmt_power(sample.media_power_mw)}")
    if is_intel_sample(sample):
        print_console(f"Package:      {fmt_power(effective_total_power_mw(sample))}")
        print_console(f"CPU usage:    {fmt_pct(current_cpu_usage(sample))}  {fmt_freq(sample.p_freq_mhz)}")
    else:
        print_console(f"SoC/Total:    {fmt_power(effective_total_power_mw(sample))}")
        print_console(f"P usage:      {fmt_pct(sample.p_usage_pct)}  {fmt_freq(sample.p_freq_mhz)}")
        print_console(f"E usage:      {fmt_pct(sample.e_usage_pct)}  {fmt_freq(sample.e_freq_mhz)}")
    print_console(f"GPU usage:    {fmt_pct(sample.gpu_usage_pct)}  {fmt_freq(sample.gpu_freq_mhz)}")
    print_console(f"ANE usage:    {fmt_pct(sample.ane_usage_pct)}")
    print_console(f"Media usage:  {fmt_pct(sample.media_usage_pct)}")
    has_detailed_temperatures = any(
        value is not None
        for value in (
            sample.cpu_temp_avg_c,
            sample.cpu_temp_max_c,
            sample.gpu_temp_avg_c,
            sample.gpu_temp_max_c,
        )
    )
    if has_detailed_temperatures:
        print_console(f"CPU temp avg: {fmt_temp(sample.cpu_temp_avg_c)}")
        print_console(f"CPU temp max: {fmt_temp(sample.cpu_temp_max_c)}")
        print_console(f"GPU temp avg: {fmt_temp(sample.gpu_temp_avg_c)}")
        print_console(f"GPU temp max: {fmt_temp(sample.gpu_temp_max_c)}")
        print_console(f"Temp max:     {fmt_temp(sample.temp_max_c)}")
    else:
        print_console(f"Temp avg:     {fmt_temp(sample.soc_temp_c)}")
        print_console(f"Temp max:     {fmt_temp(sample.temp_max_c)}")
    if sample.temp_max_source:
        print_console(f"Max source:   {sample.temp_max_source}")
    if sample.temperature_source or has_detailed_temperatures:
        print_console(f"Temp source:  {sample.temperature_source or 'n/a'}")
    if sample.fan_rpm is not None:
        print_console(f"Fan:          {sample.fan_rpm:.0f} rpm")
    print_console(f"Battery:      {fmt_power(sample.battery_power_mw)}")
    if sample.memory_bandwidth_gbps:
        bandwidth = ", ".join(f"{name} {value}" for name, value in memory_bandwidth_rows(sample))
        print_console(f"Mem BW:       {bandwidth}")
    print_console(f"Thermal:      {sample.thermal_pressure or 'n/a'}")
    print_console(f"Throttled:    {sample.throttled if sample.throttled is not None else 'unknown'}")
    if sample.throttle_reasons:
        print_console(f"Throttle:     {', '.join(sample.throttle_reasons)}")
    if sample.performance_limit_reasons:
        print_console(f"Perf reasons: {', '.join(sample.performance_limit_reasons)}")
    print_console(f"Raw keys:     {sample.raw_keys or 'n/a'}")


def display_path(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve(strict=False)
        home = real_user_home().expanduser().resolve(strict=False)
        try:
            relative = resolved.relative_to(home)
        except ValueError:
            return str(path)
        return "~" if str(relative) == "." else str(Path("~") / relative)
    except Exception:
        return str(path)


def anonymize_text_paths(value: Any) -> str:
    text = str(value)
    try:
        home = str(real_user_home().expanduser().resolve(strict=False))
        if home:
            text = re.sub(rf"{re.escape(home)}(?=$|/)", "~", text)
    except Exception:
        pass
    return sanitize_terminal_text(text)


def nearest_existing_parent(path: Path) -> Path:
    current = path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def can_write_settings() -> bool:
    parent = nearest_existing_parent(SETTINGS_PATH)
    return os.access(parent, os.W_OK)


def check_command(name: str) -> tuple[str, str]:
    path = SYSTEM_COMMANDS.get(name)
    if path is None:
        return "fail", "no trusted path configured"
    return ("ok", path) if os.access(path, os.X_OK) else ("fail", f"missing at {path}")


def diagnostic_rows(args: argparse.Namespace | None = None) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    def add(name: str, status: str, note: str) -> None:
        rows.append((name, status, note))

    add("macOS", "ok" if sys.platform == "darwin" else "fail", platform.platform())
    process_machine = platform.machine() or "unknown"
    machine, translated = physical_machine(process_machine)
    if translated:
        add("platform", "warn", f"Apple Silicon {machine}; Python runs through Rosetta ({process_machine})")
    elif machine in {"arm64", "aarch64"}:
        add("platform", "ok", f"Apple Silicon {machine}")
    elif machine in {"x86_64", "amd64"}:
        add("platform", "warn", f"Intel {machine}; experimental support")
    else:
        add("platform", "fail", machine)
    py_ok = sys.version_info >= (3, 10)
    add("Python", "ok" if py_ok else "fail", platform.python_version())
    add("settings", "ok" if can_write_settings() else "warn", display_path(SETTINGS_PATH))

    for command in ("powermetrics", "ioreg", "vm_stat", "memory_pressure", "sysctl", "ps", "netstat"):
        status, note = check_command(command)
        add(command, status, note)

    if args is not None and getattr(args, "mock", False):
        add("sudo cached", "ok", "not needed in mock mode")
    elif powermetrics_needs_sudo():
        status, note = check_command("sudo")
        add("sudo", status, note)
        if status == "ok":
            ok, error = refresh_sudo_credentials(prompt=False)
            add("sudo cached", "ok" if ok else "warn", "ready" if ok else (error or "will prompt when dashboard starts"))
    else:
        add("sudo cached", "ok", "not needed; running as root")

    if args is not None and getattr(args, "live", False):
        ok, error = refresh_sudo_credentials(prompt=False)
        if ok or is_root_process():
            cmd = powermetrics_command(int(args.interval * 1000), "1")
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    timeout=max(5.0, args.interval + 4.0),
                    env=system_environment(),
                )
                raw = proc.stdout.split(b"\0", 1)[0].strip() if proc.stdout else b""
                valid_plist = False
                if proc.returncode == 0 and raw:
                    try:
                        parsed = plistlib.loads(raw)
                        valid_plist = (
                            isinstance(parsed, dict)
                            and powermetrics_sample_is_usable(
                                sample_from_plist(parsed, interval_s=args.interval)
                            )
                        )
                    except Exception:
                        valid_plist = False
                add("powermetrics sample", "ok" if valid_plist else "fail", f"exit {proc.returncode}; plist {'ok' if valid_plist else 'invalid'}")
            except Exception as exc:
                add("powermetrics sample", "fail", str(exc))
        else:
            add("powermetrics sample", "warn", "skip; sudo credential is not cached")
    return rows


def diagnostics_exit_code(rows: list[tuple[str, str, str]]) -> int:
    return 1 if any(status == "fail" for _, status, _ in rows) else 0


def run_doctor(args: argparse.Namespace) -> int:
    rows = diagnostic_rows(args)
    if args.json:
        print(json.dumps({"app": APP_NAME, "version": VERSION, "checks": [{"name": n, "status": s, "note": note} for n, s, note in rows]}, indent=2))
        return diagnostics_exit_code(rows)
    print_console(f"{APP_NAME} doctor {VERSION}")
    width = max(len(name) for name, _, _ in rows)
    for name, status, note in rows:
        marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}.get(status, status.upper())
        print_console(f"{marker:<4} {name:<{width}}  {note}")
    return diagnostics_exit_code(rows)


def sample_to_report_dict(sample: MetricSample | None) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "soc_power": fmt_power(effective_total_power_mw(sample)),
        "cpu_power": fmt_power(sample.cpu_power_mw),
        "gpu_power": fmt_power(sample.gpu_power_mw),
        "ane_power": fmt_power(sample.ane_power_mw),
        "cpu_usage": fmt_pct(current_cpu_usage(sample)).strip(),
        "cpu_usage_source": sample.cpu_usage_source or ("powermetrics" if current_cpu_usage(sample) is not None else "n/a"),
        "gpu_usage": fmt_pct(sample.gpu_usage_pct).strip(),
        "thermal_pressure": sample.thermal_pressure or "n/a",
        "throttled": sample.throttled if sample.throttled is not None else "unknown",
        "throttle_reasons": list(sample.throttle_reasons),
        "performance_limit_reasons": list(sample.performance_limit_reasons),
        "cpu_temp_avg": fmt_temp(sample.cpu_temp_avg_c),
        "cpu_temp_max": fmt_temp(sample.cpu_temp_max_c),
        "cpu_temp_max_source": sample.cpu_temp_max_label or "n/a",
        "gpu_temp_avg": fmt_temp(sample.gpu_temp_avg_c),
        "gpu_temp_max": fmt_temp(sample.gpu_temp_max_c),
        "pmu_temp_avg": fmt_temp(sample.pmu_temp_avg_c),
        "pmu_temp_max": fmt_temp(sample.pmu_temp_max_c),
        "airport_temp": fmt_temp(sample.airport_temp_c),
        "power_supply_temp": fmt_temp(sample.power_supply_temp_c),
        "trackpad_temp": fmt_temp(sample.trackpad_temp_c),
        "trackpad_actuator_temp": fmt_temp(sample.trackpad_actuator_temp_c),
        "temperature_source": sample.temperature_source or "n/a",
        "temp_avg": fmt_temp(sample.soc_temp_c),
        "temp_max": fmt_temp(sample.temp_max_c),
        "temp_max_source": sample.temp_max_source or "n/a",
        "fan": f"{sample.fan_rpm:.0f} rpm" if sample.fan_rpm is not None else "n/a",
        "raw_keys": sample.raw_keys,
        "memory_bandwidth": dict(memory_bandwidth_rows(sample)),
        "warning": anonymize_text_paths(sample.warning) if sample.warning else None,
    }


def memory_to_report_dict(memory: MemoryStats) -> dict[str, Any]:
    return {
        "total": fmt_bytes_zero(memory.total_bytes),
        "used": fmt_bytes_zero(memory.used_bytes),
        "used_pct": fmt_pct(memory.used_pct).strip(),
        "pressure": fmt_pct(memory.pressure_pct).strip(),
        "swap_used": fmt_bytes_zero(memory.swap_used_bytes),
    }


def battery_state_text(battery: BatteryStats) -> str:
    if not battery_supported(battery):
        return "unsupported"
    if battery.charging is True:
        return "charging"
    if battery.external_connected is True:
        return "plugged"
    if battery.external_connected is False or battery.charging is False:
        return "discharging"
    return "unknown"


def usb_c_state_text(usb_c: UsbCStats) -> str:
    if not usb_c_supported(usb_c):
        return "unsupported"
    active = usb_c.active_port
    if usb_c.charging is True:
        return "charging"
    if usb_c.external_connected is True:
        return "plugged"
    if active and active.connected:
        return "PD active"
    if usb_c.external_connected is False:
        return "no input"
    return "unknown"


def battery_to_report_dict(battery: BatteryStats) -> dict[str, Any]:
    return {
        "supported": battery_supported(battery),
        "charge": fmt_pct(battery.charge_pct).strip(),
        "state": battery_state_text(battery),
        "power": fmt_power(battery.power_mw),
        "temperature": fmt_temp(battery.temperature_c),
        "health": fmt_pct(battery.health_pct).strip(),
        "cycles": battery.cycle_count if battery.cycle_count is not None else "n/a",
    }


def usb_to_report_dict(usb_c: UsbCStats) -> dict[str, Any]:
    active = usb_c.active_port
    input_active = charge_input_active(usb_c)
    voltage = first_non_none(usb_c.system_voltage_v, usb_c.adapter_voltage_v, active.voltage_v if active else None) if input_active else None
    current = first_non_none(usb_c.system_current_a, usb_c.adapter_current_a, active.current_a if active else None) if input_active else None
    power = first_non_none(
        usb_c.system_power_w,
        usb_c.adapter_contract_power_w,
        active.power_w if active else None,
    ) if input_active else None
    ports: list[dict[str, Any]] = []
    for port in usb_c.ports:
        item: dict[str, Any] = {
            "label": port.label,
            "connected": port.connected,
            "role": port.role,
            "cable_telemetry": "supported" if port.cable_query_supported else "unavailable",
            "active_transports": list(port.active_transports) if port.connected else [],
            "data_link": (
                usb_data_link_label(port.active_transports, port.data_link_speed_bps)
                if port.connected
                else "unavailable"
            ),
            "data_link_speed_bps": port.data_link_speed_bps if port.data_link_speed_bps is not None else "n/a",
            "dp_alt_mode": (
                "unavailable"
                if not port.connected
                else "active"
                if "DisplayPort" in port.active_transports
                else "not active"
            ),
        }
        info = port.cable_info if port.connected else None
        if info is not None:
            item.update(
                {
                    "cable_type": info.cable_type or "n/a",
                    "cable_current": info.current_label or "n/a",
                    "cable_max_voltage": fmt_voltage(info.max_voltage_v),
                    "cable_max_power": fmt_watts(info.max_power_w),
                    "cable_speed": info.speed_label or "n/a",
                    "cable_vendor_id": f"0x{info.vendor_id:04X}" if info.vendor_id is not None else "n/a",
                    "cable_product_id": f"0x{info.product_id:04X}" if info.product_id is not None else "n/a",
                    "cable_pd_revision": info.pd_revision or "n/a",
                }
            )
        ports.append(item)
    return {
        "supported": usb_c_supported(usb_c),
        "state": usb_c_state_text(usb_c),
        "input_type": charge_input_label(usb_c) if input_active else "n/a",
        "active_port": active.label if active else charge_input_label(usb_c) if input_active else "n/a",
        "voltage": fmt_voltage(voltage),
        "current": fmt_current(current),
        "power": fmt_watts(power),
        "adapter_rating": fmt_watts(usb_c.adapter_power_w) if input_active else "n/a",
        "ports": ports,
    }


def collect_report_sample(args: argparse.Namespace) -> MetricSample | None:
    if args.mock:
        return next(MockStream(int(args.interval * 1000)).samples())
    if not args.live:
        return None
    ensure_powermetrics_access(args)
    previous_cpu: CpuLoadSnapshot | None = None
    try:
        previous_cpu = read_cpu_load_snapshot()
    except Exception:
        previous_cpu = None
    cmd = powermetrics_command(int(args.interval * 1000), "1")
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=max(5.0, args.interval + 4.0),
            env=system_environment(),
        )
    except subprocess.TimeoutExpired:
        return MetricSample(warning="powermetrics timed out while collecting a report sample")
    except Exception as exc:
        return MetricSample(warning=f"powermetrics sample failed: {exc}")
    if proc.returncode != 0 or not proc.stdout:
        return MetricSample(warning=(proc.stderr.decode("utf-8", "ignore").strip() or f"powermetrics exited {proc.returncode}"))
    raw = proc.stdout.split(b"\0", 1)[0].strip()
    try:
        obj = plistlib.loads(raw)
    except Exception as exc:
        return MetricSample(warning=f"powermetrics plist parse failed: {exc}")
    if not isinstance(obj, dict):
        return MetricSample(warning="powermetrics did not return a plist dictionary")
    sample = sample_from_plist(obj, interval_s=args.interval)
    if not powermetrics_sample_is_usable(sample):
        if sample.warning is None:
            sample.warning = "powermetrics sample contained no usable telemetry"
        return sample
    if is_intel_sample(sample):
        try:
            apply_system_cpu_usage(sample, cpu_usage_from_snapshots(previous_cpu, read_cpu_load_snapshot()))
        except Exception:
            pass
    if not is_intel_sample(sample) or sample.soc_temp_c is None or sample.temp_max_c is None:
        apply_hid_temperatures(sample)
    return sample


def diagnostic_rows_for_report(args: argparse.Namespace, sample: MetricSample | None) -> list[tuple[str, str, str]]:
    diag_args = argparse.Namespace(**vars(args))
    diag_args.live = False
    rows = anonymize_diagnostic_rows(diagnostic_rows(diag_args))
    if getattr(args, "live", False):
        if getattr(args, "mock", False):
            rows.append(("mock sample", "ok", "generated demo snapshot"))
        elif sample is None:
            rows.append(("powermetrics sample", "warn", "not requested"))
        elif sample.warning:
            status = "warn" if powermetrics_sample_is_usable(sample) else "fail"
            rows.append(("powermetrics sample", status, anonymize_text_paths(sample.warning)))
        else:
            rows.append(("powermetrics sample", "ok", "snapshot collected"))
    return rows


def anonymize_diagnostic_rows(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    anonymized: list[tuple[str, str, str]] = []
    for name, status, note in rows:
        note = anonymize_text_paths(note)
        if note.startswith("/"):
            note = display_path(Path(note))
        anonymized.append((name, status, note))
    return anonymized


def report_data(args: argparse.Namespace) -> dict[str, Any]:
    if args.mock:
        memory = mock_memory_stats(0.0)
        battery = mock_battery_stats(0.0)
        usb_c = mock_usb_c_stats(0.0)
    else:
        memory = read_memory_stats()
        battery, usb_c = read_charge_stats()
    sample = collect_report_sample(args)
    rows = diagnostic_rows_for_report(args, sample)
    process_machine = platform.machine() or "unknown"
    physical_arch, translated = physical_machine(process_machine)
    machine_text = f"{physical_arch} (Rosetta {process_machine})" if translated else physical_arch
    return {
        "app": APP_NAME,
        "version": VERSION,
        "system": {
            "macos": platform.mac_ver()[0] or platform.platform(),
            "machine": machine_text,
            "python": platform.python_version(),
        },
        "settings_path": display_path(SETTINGS_PATH),
        "diagnostics": [{"name": name, "status": status, "note": note} for name, status, note in rows],
        "snapshot": {
            "powermetrics": sample_to_report_dict(sample),
            "memory": memory_to_report_dict(memory),
            "battery": battery_to_report_dict(battery),
            "usb_c": usb_to_report_dict(usb_c),
        },
    }


def print_markdown_report(data: dict[str, Any]) -> None:
    def safe_print(value: Any = "") -> None:
        print_console(value)

    def print_value(key: str, value: Any, indent: int = 0) -> None:
        prefix = "  " * indent
        if isinstance(value, dict):
            safe_print(f"{prefix}- {key}:")
            for child_key, child_value in value.items():
                print_value(str(child_key), child_value, indent + 1)
        elif isinstance(value, list):
            safe_print(f"{prefix}- {key}:")
            if not value:
                safe_print(f"{prefix}  - n/a")
            for item in value:
                if isinstance(item, dict):
                    text = ", ".join(f"{child_key}={child_value}" for child_key, child_value in item.items())
                    safe_print(f"{prefix}  - {text}")
                else:
                    safe_print(f"{prefix}  - {item}")
        else:
            safe_print(f"{prefix}- {key}: {value}")

    safe_print(f"# {data['app']} report")
    safe_print()
    safe_print(f"- Version: {data['version']}")
    safe_print(f"- macOS: {data['system']['macos']}")
    safe_print(f"- Machine: {data['system']['machine']}")
    safe_print(f"- Python: {data['system']['python']}")
    safe_print(f"- Settings: {data['settings_path']}")
    safe_print()
    safe_print("## Diagnostics")
    for row in data["diagnostics"]:
        safe_print(f"- {row['status'].upper()}: {row['name']} - {row['note']}")
    safe_print()
    safe_print("## Snapshot")
    for section, values in data["snapshot"].items():
        safe_print(f"### {section}")
        if values is None:
            safe_print("- n/a")
            continue
        for key, value in values.items():
            print_value(str(key), value)
        safe_print()


def run_report(args: argparse.Namespace) -> int:
    data = report_data(args)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print_markdown_report(data)
    return 0


def build_parser() -> argparse.ArgumentParser:
    settings = load_settings()
    parser = argparse.ArgumentParser(
        description=(
            "Asmond monitors macOS power, thermal pressure, CPU/GPU load, memory, "
            "battery, USB-C/MagSafe charging, disk/network I/O and processes from a compact terminal UI."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("-i", "--interval", type=finite_float_arg, default=settings.get("interval", 1.0), help="sample interval in seconds")
    parser.add_argument("--history", type=int, default=240, help="number of samples kept for graphs")
    parser.add_argument("-t", "--theme", choices=sorted(THEMES), default=settings.get("theme", "classic"), help="color theme")
    parser.add_argument(
        "--layout",
        type=layout_arg,
        default=settings.get("layout", "full"),
        metavar="{full,compact,focus,custom}",
        help="dashboard layout preset",
    )
    parser.add_argument("--show-io", action="store_true", default=settings.get("show_io", False), help="show compact disk/network panel")
    parser.add_argument("--mock", action="store_true", help="run with generated demo data")
    parser.add_argument("--remove-settings", action="store_true", help="remove the saved user settings file and exit")
    parser.add_argument("--config-path", action="store_true", help="print the settings file path and exit")
    parser.add_argument("--allow-root-ui", action="store_true", help="allow the full terminal UI to run as root")
    parser.set_defaults(
        upper_power_mode=settings.get("upper_power_mode", "soc"),
        lower_power_mode=settings.get("lower_power_mode", "cpu"),
        upper_io_mode=settings.get("upper_io_mode", "disk_read"),
        lower_io_mode=settings.get("lower_io_mode", "net_in"),
        load_view=settings.get("load_view", "rows"),
        process_panel=settings.get("process_panel", "hidden"),
        process_sort=settings.get("process_sort", "cpu"),
        charge_panel=settings.get("charge_panel", "battery"),
        allow_root_kill=False if is_root_process() else settings.get("allow_root_kill", False),
        settings_warning=settings.get("__warning__", ""),
        alert_temp_c=settings.get("alert_temp_c", HIGH_TEMP_C),
        alert_swap_gib=settings.get("alert_swap_gib", DEFAULT_ALERT_SWAP_GIB),
        alert_battery_drain_w=settings.get("alert_battery_drain_w", DEFAULT_ALERT_BATTERY_DRAIN_W),
        custom_slot=settings.get("custom_slot", CUSTOM_SLOT_IDS[0]),
        custom_layout=settings.get("custom_layout", {}),
        custom_name=settings.get("custom_name", ""),
    )

    subparsers = parser.add_subparsers(dest="command")
    probe = subparsers.add_parser("probe", help="print one parsed powermetrics sample")
    probe.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="run with generated demo data")
    probe.add_argument("--raw", action="store_true", help="also print flattened plist keys and values; not anonymized")
    doctor = subparsers.add_parser("doctor", help="check local Asmond data sources")
    doctor.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="run with generated demo data")
    doctor.add_argument("--json", action="store_true", help="print machine-readable diagnostics")
    doctor.add_argument("--live", action="store_true", help="also try one live powermetrics sample without prompting for sudo")
    report = subparsers.add_parser("report", help="print an anonymized support report")
    report.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="run with generated demo data")
    report.add_argument("--json", action="store_true", help="print the report as JSON")
    report.add_argument("--live", action="store_true", help="include one live powermetrics sample; may prompt for sudo")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "settings_warning", ""):
        print_console(f"{APP_NAME}: {args.settings_warning}", file=sys.stderr)
    args.interval = round(clamp(args.interval, MIN_INTERVAL, MAX_INTERVAL), 1)
    args.history = int(clamp(args.history, 20, 1000))
    args.layout = normalize_layout(args.layout)

    if args.remove_settings:
        error = remove_settings()
        if error:
            print_console(f"Could not remove settings at {SETTINGS_PATH}: {error}", file=sys.stderr)
            return 1
        print_console(f"Removed settings at {SETTINGS_PATH}")
        return 0
    if args.config_path:
        print_console(SETTINGS_PATH)
        return 0

    if args.command == "probe":
        return run_probe(args)
    if args.command == "doctor":
        return run_doctor(args)
    if args.command == "report":
        return run_report(args)

    ensure_ui_not_root(args)
    ensure_powermetrics_access(args)
    curses.wrapper(run_curses, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
