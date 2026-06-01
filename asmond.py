#!/usr/bin/env python3
"""
Small Apple Silicon power/thermal monitor.

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
import plistlib
import pwd
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "Asmond"
VERSION = "0.1.1"
POWER_SAMPLERS = "cpu_power,gpu_power,ane_power,thermal,battery"
ANE_MAX_POWER_MW = 8000.0
IOHID_TEMP_TYPE = 15
IOHID_TEMP_FIELD = IOHID_TEMP_TYPE << 16
POWER_MODES = ("soc", "cpu", "gpu", "ane")
LAYOUTS = ("full", "compact", "power-only", "thermals-only")
LOAD_VIEWS = ("rows", "graph")
PROCESS_PANEL_MODES = ("hidden", "left", "right")
PROCESS_SORTS = ("cpu", "ram", "pid", "name")
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
    ("process_panel", "Processes", "Full layout process panel"),
    ("process_sort", "Proc sort", "Process sort key"),
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
KILL_CONFIRM_SECONDS = 3.0
MIN_INTERVAL = 0.1
MAX_INTERVAL = 10.0
MEMORY_BATTERY_INTERVAL = 2.0
IO_POLL_INTERVAL = 1.0
PROCESS_POLL_INTERVAL = 2.0


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


@dataclass
class CoreMetric:
    label: str
    usage_pct: float | None = None
    freq_mhz: float | None = None


def real_user_home() -> Path:
    if os.name != "posix":
        return Path.home()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and sudo_user != "root":
            try:
                return Path(pwd.getpwnam(sudo_user).pw_dir)
            except Exception:
                pass
    return Path.home()


def default_settings_path() -> Path:
    override = os.environ.get(SETTINGS_DIR_ENV)
    if override:
        return Path(override).expanduser() / SETTINGS_FILENAME
    return real_user_home() / "Library" / "Application Support" / APP_NAME / SETTINGS_FILENAME


SETTINGS_PATH = default_settings_path()


def settings_owner() -> tuple[int, int] | None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            return int(sudo_uid), int(sudo_gid)
        except ValueError:
            return None
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            info = pwd.getpwnam(sudo_user)
            return info.pw_uid, info.pw_gid
        except Exception:
            return None
    return None


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    settings: dict[str, Any] = {}
    theme = data.get("theme")
    if isinstance(theme, str) and theme in THEMES:
        settings["theme"] = theme
    interval = data.get("interval")
    if isinstance(interval, int | float):
        settings["interval"] = float(clamp(float(interval), MIN_INTERVAL, MAX_INTERVAL))
    layout = data.get("layout")
    if isinstance(layout, str) and layout in LAYOUTS:
        settings["layout"] = layout
    show_io = data.get("show_io")
    if isinstance(show_io, bool):
        settings["show_io"] = show_io
    upper_power_mode = data.get("upper_power_mode")
    if isinstance(upper_power_mode, str) and upper_power_mode in POWER_MODES:
        settings["upper_power_mode"] = upper_power_mode
    lower_power_mode = data.get("lower_power_mode")
    if isinstance(lower_power_mode, str) and lower_power_mode in POWER_MODES:
        settings["lower_power_mode"] = lower_power_mode
    upper_io_mode = data.get("upper_io_mode")
    if isinstance(upper_io_mode, str) and upper_io_mode in IO_MODES:
        settings["upper_io_mode"] = upper_io_mode
    lower_io_mode = data.get("lower_io_mode")
    if isinstance(lower_io_mode, str) and lower_io_mode in IO_MODES:
        settings["lower_io_mode"] = lower_io_mode
    load_view = data.get("load_view")
    if isinstance(load_view, str) and load_view in LOAD_VIEWS:
        settings["load_view"] = load_view
    process_panel = data.get("process_panel")
    if isinstance(process_panel, str) and process_panel in PROCESS_PANEL_MODES:
        settings["process_panel"] = process_panel
    process_sort = data.get("process_sort")
    if isinstance(process_sort, str) and process_sort in PROCESS_SORTS:
        settings["process_sort"] = process_sort
    allow_root_kill = data.get("allow_root_kill")
    if isinstance(allow_root_kill, bool):
        settings["allow_root_kill"] = allow_root_kill
    alert_temp_c = data.get("alert_temp_c")
    if isinstance(alert_temp_c, int | float):
        settings["alert_temp_c"] = float(clamp(float(alert_temp_c), 40.0, 125.0))
    alert_swap_gib = data.get("alert_swap_gib")
    if isinstance(alert_swap_gib, int | float):
        settings["alert_swap_gib"] = float(clamp(float(alert_swap_gib), 0.0, 1024.0))
    alert_battery_drain_w = data.get("alert_battery_drain_w")
    if isinstance(alert_battery_drain_w, int | float):
        settings["alert_battery_drain_w"] = float(clamp(float(alert_battery_drain_w), 0.0, 250.0))
    return settings


def setting_choice(args: argparse.Namespace, name: str, default: str, choices: tuple[str, ...]) -> str:
    value = getattr(args, name, default)
    return value if isinstance(value, str) and value in choices else default


def save_settings(args: argparse.Namespace) -> str | None:
    data = {
        "theme": args.theme if args.theme in THEMES else "classic",
        "interval": round(float(args.interval), 2),
        "layout": args.layout if args.layout in LAYOUTS else "full",
        "show_io": bool(args.show_io),
        "upper_power_mode": setting_choice(args, "upper_power_mode", "soc", POWER_MODES),
        "lower_power_mode": setting_choice(args, "lower_power_mode", "cpu", POWER_MODES),
        "upper_io_mode": setting_choice(args, "upper_io_mode", "disk_read", IO_MODES),
        "lower_io_mode": setting_choice(args, "lower_io_mode", "net_in", IO_MODES),
        "load_view": setting_choice(args, "load_view", "rows", LOAD_VIEWS),
        "process_panel": setting_choice(args, "process_panel", "hidden", PROCESS_PANEL_MODES),
        "process_sort": setting_choice(args, "process_sort", "cpu", PROCESS_SORTS),
        "allow_root_kill": bool(getattr(args, "allow_root_kill", False)),
        "alert_temp_c": round(float(getattr(args, "alert_temp_c", HIGH_TEMP_C)), 1),
        "alert_swap_gib": round(float(getattr(args, "alert_swap_gib", DEFAULT_ALERT_SWAP_GIB)), 2),
        "alert_battery_drain_w": round(float(getattr(args, "alert_battery_drain_w", DEFAULT_ALERT_BATTERY_DRAIN_W)), 1),
    }
    tmp_path = SETTINGS_PATH.with_suffix(f"{SETTINGS_PATH.suffix}.tmp")
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        owner = settings_owner()
        if owner is not None:
            try:
                os.chown(SETTINGS_PATH.parent, *owner)
            except Exception:
                pass
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if owner is not None:
            try:
                os.chown(tmp_path, *owner)
            except Exception:
                pass
        os.replace(tmp_path, SETTINGS_PATH)
        if owner is not None:
            try:
                os.chown(SETTINGS_PATH, *owner)
            except Exception:
                pass
        return None
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return str(exc)


def remove_settings() -> str | None:
    try:
        SETTINGS_PATH.unlink(missing_ok=True)
        try:
            SETTINGS_PATH.parent.rmdir()
        except OSError:
            pass
        return None
    except Exception as exc:
        return str(exc)


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
        return clamp(self.used_bytes / self.total_bytes * 100.0, 0.0, 100.0)

    @property
    def pressure_pct(self) -> float | None:
        if self.total_bytes <= 0:
            return None
        if self.system_free_pct is not None:
            return clamp(100.0 - self.system_free_pct, 0.0, 100.0)
        return clamp(100.0 - self.available_bytes / self.total_bytes * 100.0, 0.0, 100.0)

    @property
    def physical_used_bytes(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return max(0, self.total_bytes - min(self.total_bytes, self.free_bytes))

    @property
    def physical_used_pct(self) -> float | None:
        if self.total_bytes <= 0:
            return None
        return clamp(self.physical_used_bytes / self.total_bytes * 100.0, 0.0, 100.0)


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


@dataclass
class SideMetricsUpdate:
    memory: MemoryStats | None = None
    battery: BatteryStats | None = None
    io_stats: IoStats | None = None
    processes: list[ProcessInfo] | None = None


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


def bytes_from_pages(pages: int | float, page_size: int) -> int:
    return max(0, int(pages) * page_size)


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
    raw_keys: int = 0
    warning: str | None = None


def normalize_key(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", path.lower()).strip()


def flatten(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten(value, next_prefix)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            next_prefix = f"{prefix}[{index}]"
            yield from flatten(value, next_prefix)
    else:
        yield prefix, obj


def parse_value(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int | float):
        if math.isfinite(float(value)):
            return float(value), None
        return None, None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "ignore")
        except Exception:
            return None, None
    if isinstance(value, str):
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z%/]+)?", value)
        if match:
            return float(match.group(1)), (match.group(2) or "").lower() or None
    return None, None


def as_mw(path: str, value: Any) -> float | None:
    number, unit = parse_value(value)
    if number is None:
        return None
    key = normalize_key(path)
    if unit == "w" or " watts" in key:
        return number * 1000.0
    if unit == "uw":
        return number / 1000.0
    if unit == "mw" or " mw" in key or "milliwatt" in key:
        return number
    # powermetrics plist power fields are normally milliwatts.
    return number


def as_pct(value: Any) -> float | None:
    number, unit = parse_value(value)
    if number is None:
        return None
    if unit == "%" or number > 1.0:
        return clamp(number, 0.0, 100.0)
    return clamp(number * 100.0, 0.0, 100.0)


def as_mhz(path: str, value: Any) -> float | None:
    number, unit = parse_value(value)
    if number is None:
        return None
    key = normalize_key(path)
    if unit == "ghz":
        return number * 1000.0
    if unit == "khz":
        return number / 1000.0
    if unit == "hz":
        return number / 1_000_000.0
    if unit == "mhz" or "mhz" in key:
        return number
    # Very large plist values are often Hz.
    if number > 100_000:
        return number / 1_000_000.0
    return number


def as_temp_c(path: str, value: Any) -> float | None:
    number, unit = parse_value(value)
    if number is None:
        return None
    key = normalize_key(path)
    if unit in {"f", "fahrenheit"}:
        number = (number - 32.0) * 5.0 / 9.0
    elif unit in {"k", "kelvin"}:
        number = number - 273.15
    elif unit in {"mk", "mc"} or "millidegree" in key:
        number = number / 1000.0
    elif 2000.0 <= number <= 4200.0:
        kelvin_temp = number / 10.0 - 273.15
        centi_temp = number / 100.0
        if -20.0 <= kelvin_temp <= 140.0:
            number = kelvin_temp
        else:
            number = centi_temp
    elif 140.0 < number <= 1000.0:
        kelvin_temp = number - 273.15
        deci_temp = number / 10.0
        if -20.0 <= kelvin_temp <= 140.0:
            number = kelvin_temp
        else:
            number = deci_temp
    elif number > 1000.0:
        number = number / 1000.0
    if -20.0 <= number <= 140.0:
        return number
    return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def find_best(
    flat: list[tuple[str, Any]],
    include: tuple[str, ...],
    *,
    any_of: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    converter=parse_value,
) -> float | None:
    best_score = -1
    best_value: float | None = None
    for path, value in flat:
        key = normalize_key(path)
        if any(part not in key for part in include):
            continue
        if any_of and not any(part in key for part in any_of):
            continue
        if any(part in key for part in exclude):
            continue
        converted = converter(path, value) if converter in {as_mw, as_mhz, as_temp_c} else converter(value)
        if isinstance(converted, tuple):
            converted = converted[0]
        if converted is None:
            continue
        score = len(include) * 10
        score += sum(8 for part in any_of if part in key)
        score -= len(key) / 200.0
        if score > best_score:
            best_score = score
            best_value = float(converted)
    return best_value


def string_contains(flat: list[tuple[str, Any]], *needles: str) -> str | None:
    for path, value in flat:
        key = normalize_key(path)
        text = str(value).strip()
        haystack = f"{key} {text.lower()}"
        if all(needle in haystack for needle in needles) and text:
            return text
    return None


def dict_number(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return float(value)
    return None


def idle_to_active_pct(value: Any) -> float | None:
    number, _ = parse_value(value)
    if number is None:
        return None
    return clamp((1.0 - number) * 100.0, 0.0, 100.0)


def average(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def max_present(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)


def temp_stats_from_flat(flat: list[tuple[str, Any]]) -> tuple[float | None, float | None]:
    temps: list[float] = []
    include_words = ("temp", "temperature", "tdie")
    exclude_words = (
        "pressure",
        "target",
        "limit",
        "threshold",
        "battery",
        "adapter",
        "charger",
        "virtual",
        "minimum",
        "maximum",
        "lifetime",
    )
    for path, value in flat:
        key = normalize_key(path)
        if not any(word in key for word in include_words):
            continue
        if any(word in key for word in exclude_words):
            continue
        temp = as_temp_c(path, value)
        if temp is not None:
            temps.append(temp)
    if not temps:
        return None, None
    return average(temps), max(temps)


def first_non_none(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def energy_counter_to_mw(value: Any, interval_s: float) -> float | None:
    number, _ = parse_value(value)
    if number is None:
        return None
    # powermetrics plist energy fields are usually millijoules over the
    # sample window. mJ/s is numerically mW.
    return max(0.0, number / max(interval_s, 0.001))


def power_value_to_mw(value: Any) -> float | None:
    number, unit = parse_value(value)
    if number is None:
        return None
    if unit == "w":
        return max(0.0, number * 1000.0)
    if unit in {"mw", "mws"}:
        return max(0.0, number)
    # Some powermetrics versions store combined_power as milliwatts, others
    # expose a small watt value. Idle SoC totals such as 5.18 are watts.
    if 0.0 <= number < 100.0:
        return number * 1000.0
    return number


def cluster_type(name: str) -> str | None:
    lower = name.lower()
    if lower.startswith("e") or "efficiency" in lower:
        return "e"
    if lower.startswith("p") or "performance" in lower:
        return "p"
    return None


def core_sort_key(core: CoreMetric) -> tuple[int, int, str]:
    match = re.match(r"([A-Z]+)(\d+)", core.label)
    if not match:
        return (9, 999, core.label)
    prefix, index = match.groups()
    group = {"P": 0, "E": 1, "S": 2}.get(prefix, 8)
    return (group, int(index), core.label)


def active_from_cluster(cluster: dict[str, Any]) -> float | None:
    direct = idle_to_active_pct(cluster.get("idle_ratio"))
    if direct is not None:
        return direct
    cpus = cluster.get("cpus")
    if isinstance(cpus, list):
        return average(idle_to_active_pct(cpu.get("idle_ratio")) for cpu in cpus if isinstance(cpu, dict))
    return None


def freq_from_cluster(cluster: dict[str, Any]) -> float | None:
    freq = as_mhz("freq_hz", cluster.get("freq_hz"))
    if freq is not None:
        return freq
    cpus = cluster.get("cpus")
    if isinstance(cpus, list):
        return max_present(as_mhz("freq_hz", cpu.get("freq_hz")) for cpu in cpus if isinstance(cpu, dict))
    return None


def apply_structured_powermetrics(sample: MetricSample, obj: dict[str, Any], interval_s: float) -> None:
    pressure = obj.get("thermal_pressure")
    if isinstance(pressure, str) and pressure.strip():
        sample.thermal_pressure = pressure.strip()

    processor = obj.get("processor")
    if isinstance(processor, dict):
        sample.cpu_power_mw = first_non_none(energy_counter_to_mw(processor.get("cpu_energy"), interval_s), sample.cpu_power_mw)
        sample.gpu_power_mw = first_non_none(energy_counter_to_mw(processor.get("gpu_energy"), interval_s), sample.gpu_power_mw)
        sample.ane_power_mw = first_non_none(energy_counter_to_mw(processor.get("ane_energy"), interval_s), sample.ane_power_mw)
        sample.soc_power_mw = first_non_none(power_value_to_mw(processor.get("combined_power")), sample.soc_power_mw)

        e_active: list[float | None] = []
        p_active: list[float | None] = []
        e_freqs: list[float | None] = []
        p_freqs: list[float | None] = []
        all_core_active: list[float | None] = []
        clusters = processor.get("clusters")
        if isinstance(clusters, list):
            for cluster in clusters:
                if not isinstance(cluster, dict):
                    continue
                name = str(cluster.get("name", ""))
                active = active_from_cluster(cluster)
                freq = freq_from_cluster(cluster)
                kind = cluster_type(name)
                if kind == "e":
                    e_active.append(active)
                    e_freqs.append(freq)
                elif kind == "p":
                    p_active.append(active)
                    p_freqs.append(freq)

                cpus = cluster.get("cpus")
                if isinstance(cpus, list):
                    for cpu in cpus:
                        if isinstance(cpu, dict):
                            core_active = idle_to_active_pct(cpu.get("idle_ratio"))
                            all_core_active.append(core_active)
                            if kind in {"e", "p"}:
                                cpu_id = int(dict_number(cpu, "cpu") or len(sample.cores))
                                sample.cores.append(
                                    CoreMetric(
                                        label=f"{kind.upper()}{cpu_id}",
                                        usage_pct=core_active,
                                        freq_mhz=as_mhz("freq_hz", cpu.get("freq_hz")),
                                    )
                                )

        sample.e_usage_pct = first_non_none(average(e_active), sample.e_usage_pct)
        sample.p_usage_pct = first_non_none(average(p_active), sample.p_usage_pct)
        sample.e_freq_mhz = first_non_none(max_present(e_freqs), sample.e_freq_mhz)
        sample.p_freq_mhz = first_non_none(max_present(p_freqs), sample.p_freq_mhz)
        sample.cpu_usage_pct = first_non_none(average(all_core_active), average([sample.e_usage_pct, sample.p_usage_pct]), sample.cpu_usage_pct)
        sample.cores.sort(key=core_sort_key)

    gpu = obj.get("gpu")
    if isinstance(gpu, dict):
        sample.gpu_usage_pct = first_non_none(idle_to_active_pct(gpu.get("idle_ratio")), sample.gpu_usage_pct)
        sample.gpu_freq_mhz = first_non_none(as_mhz("freq_hz", gpu.get("freq_hz")), sample.gpu_freq_mhz)

    thermal_sensors = obj.get("thermal_sensors")
    if isinstance(thermal_sensors, list):
        temps = []
        for sensor in thermal_sensors:
            if not isinstance(sensor, dict):
                continue
            name = str(sensor.get("name", ""))
            for key in ("temperature", "current_value", "value", "temp"):
                temp = as_temp_c(f"{name} {key}", sensor.get(key))
                if temp is not None:
                    temps.append(temp)
        sample.soc_temp_c = first_non_none(average(temps), sample.soc_temp_c)
        sample.temp_max_c = first_non_none(max_present(temps), sample.temp_max_c)


def sample_from_plist(obj: dict[str, Any], interval_s: float = 1.0) -> MetricSample:
    flat = list(flatten(obj))
    sample = MetricSample(raw_keys=len(flat))
    apply_structured_powermetrics(sample, obj, interval_s)

    sample.cpu_power_mw = first_non_none(sample.cpu_power_mw, find_best(
        flat, ("cpu", "power"), exclude=("limit", "cap", "battery"), converter=as_mw
    ))
    sample.gpu_power_mw = first_non_none(sample.gpu_power_mw, find_best(
        flat, ("gpu", "power"), exclude=("limit", "cap", "battery"), converter=as_mw
    ))
    sample.ane_power_mw = first_non_none(sample.ane_power_mw, find_best(
        flat,
        ("power",),
        any_of=("ane", "neural"),
        exclude=("limit", "cap", "battery"),
        converter=as_mw,
    ))
    sample.media_power_mw = first_non_none(sample.media_power_mw, find_best(
        flat,
        ("power",),
        any_of=("media", "decoder", "encoder", "video"),
        exclude=("limit", "cap", "battery"),
        converter=as_mw,
    ))
    sample.soc_power_mw = first_non_none(sample.soc_power_mw, find_best(
        flat,
        ("power",),
        any_of=("soc", "processor", "package", "combined"),
        exclude=("limit", "cap", "battery"),
        converter=as_mw,
    ))
    sample.battery_power_mw = first_non_none(sample.battery_power_mw, find_best(
        flat, ("battery", "power"), exclude=("limit", "cap", "accumulated"), converter=as_mw
    ))

    sample.p_usage_pct = first_non_none(sample.p_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("p cluster", "performance"),
        exclude=("frequency", "freq"),
        converter=as_pct,
    ))
    sample.e_usage_pct = first_non_none(sample.e_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("e cluster", "efficiency"),
        exclude=("frequency", "freq"),
        converter=as_pct,
    ))
    sample.cpu_usage_pct = first_non_none(sample.cpu_usage_pct, find_best(
        flat,
        ("cpu", "active"),
        any_of=("residency", "duty", "usage", "utilization"),
        exclude=("frequency", "freq"),
        converter=as_pct,
    ))
    sample.gpu_usage_pct = first_non_none(sample.gpu_usage_pct, find_best(
        flat,
        ("gpu", "active"),
        any_of=("residency", "duty", "usage", "utilization"),
        exclude=("frequency", "freq"),
        converter=as_pct,
    ))
    sample.ane_usage_pct = first_non_none(sample.ane_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("ane", "neural"),
        exclude=("frequency", "freq"),
        converter=as_pct,
    ))
    sample.media_usage_pct = first_non_none(sample.media_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("media", "decoder", "encoder", "video"),
        exclude=("frequency", "freq"),
        converter=as_pct,
    ))
    # There is no stable public ANE utilization counter here. asitop-style
    # tools often display ANE power as a utilization proxy, so do that only
    # when powermetrics reports ANE energy and no better active-residency value.
    if sample.ane_usage_pct is None and sample.ane_power_mw is not None:
        sample.ane_usage_pct = clamp(sample.ane_power_mw / ANE_MAX_POWER_MW * 100.0, 0.0, 100.0)

    sample.p_freq_mhz = first_non_none(sample.p_freq_mhz, find_best(
        flat,
        ("frequency",),
        any_of=("p cluster", "performance"),
        converter=as_mhz,
    ))
    sample.e_freq_mhz = first_non_none(sample.e_freq_mhz, find_best(
        flat,
        ("frequency",),
        any_of=("e cluster", "efficiency"),
        converter=as_mhz,
    ))
    sample.gpu_freq_mhz = first_non_none(sample.gpu_freq_mhz, find_best(flat, ("gpu", "frequency"), converter=as_mhz))

    temp_avg, temp_max = temp_stats_from_flat(flat)
    sample.soc_temp_c = first_non_none(sample.soc_temp_c, temp_avg, find_best(
        flat,
        tuple(),
        any_of=("soc temp", "die temp", "temperature", "thermal temperature"),
        exclude=("target", "limit"),
        converter=as_temp_c,
    ))
    sample.temp_max_c = first_non_none(sample.temp_max_c, temp_max, sample.soc_temp_c)

    pressure = string_contains(flat, "thermal", "pressure")
    if pressure and sample.thermal_pressure is None:
        sample.thermal_pressure = pressure

    reasons: list[str] = []
    for path, value in flat:
        key = normalize_key(path)
        text = str(value).strip()
        low_text = text.lower()
        if any(word in key for word in ("limit", "throttle", "thermal")):
            number, _ = parse_value(value)
            active_bool = low_text in {"1", "true", "yes", "active"}
            active_num = number is not None and number > 0
            if ("limit" in key or "throttle" in key) and (active_bool or active_num):
                reason = re.sub(r"\s+", " ", key).strip()
                if reason and reason not in reasons:
                    reasons.append(reason[:42])
    sample.throttle_reasons = reasons[:4]
    if reasons:
        sample.throttled = True
    elif sample.thermal_pressure:
        pressure_key = sample.thermal_pressure.lower()
        sample.throttled = any(word in pressure_key for word in ("serious", "critical", "heavy"))
    else:
        sample.throttled = None

    return sample


def read_battery_items() -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["ioreg", "-rn", "AppleSmartBattery", "-a"],
            check=False,
            capture_output=True,
            timeout=1.0,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        items = plistlib.loads(proc.stdout)
    except Exception:
        return None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    return items[0]


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


def read_battery_power_mw() -> float | None:
    battery = read_battery_items()
    if battery is None:
        return None
    return battery_power_from_item(battery)


def read_battery_stats() -> BatteryStats:
    battery = read_battery_items()
    if battery is None:
        return BatteryStats()
    raw_max_capacity = dict_number(battery, "AppleRawMaxCapacity")
    raw_design = dict_number(battery, "DesignCapacity")
    max_capacity = raw_max_capacity or dict_number(battery, "MaxCapacity")
    design_capacity = raw_design
    health_pct = None
    if raw_max_capacity and raw_design:
        health_pct = clamp(raw_max_capacity / raw_design * 100.0, 0.0, 150.0)
    is_charging = bool(battery.get("IsCharging")) if "IsCharging" in battery else None
    time_remaining = dict_number(battery, "TimeRemaining")
    if time_remaining is None:
        time_remaining = dict_number(battery, "AvgTimeToFull" if is_charging else "AvgTimeToEmpty")
    if time_remaining is not None and time_remaining >= 65535:
        time_remaining = None
    return BatteryStats(
        power_mw=battery_power_from_item(battery),
        temperature_c=battery_temperature_from_item(battery),
        charge_pct=first_non_none(dict_number(battery, "CurrentCapacity"), dict_number(battery, "AppleRawCurrentCapacity")),
        health_pct=health_pct,
        cycle_count=int(dict_number(battery, "CycleCount") or 0) if dict_number(battery, "CycleCount") is not None else None,
        time_remaining_min=int(time_remaining) if time_remaining is not None else None,
        charging=is_charging,
        external_connected=bool(battery.get("ExternalConnected")) if "ExternalConnected" in battery else None,
        design_capacity=int(design_capacity) if design_capacity is not None else None,
        max_capacity=int(max_capacity) if max_capacity is not None else None,
        raw_max_capacity=int(raw_max_capacity) if raw_max_capacity is not None else None,
    )


def refresh_battery_power(sample: MetricSample) -> None:
    battery = read_battery_stats()
    if battery.power_mw is not None:
        sample.battery_power_mw = battery.power_mw


def parse_number_text(text: str) -> float | None:
    value = text.strip()
    if not value:
        return None
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def parse_sysctl_size(text: str) -> int:
    match = re.search(r"(-?[\d.,]+)\s*([KMGT]?)(?:i?B|B)?", text.strip(), re.IGNORECASE)
    if not match:
        return 0
    amount = parse_number_text(match.group(1))
    if amount is None:
        return 0
    unit = match.group(2).upper()
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
    return max(0, int(amount * factor))


def parse_swapusage(text: str) -> tuple[int, int, int]:
    values = {"total": 0, "used": 0, "free": 0}
    for name, raw_value in re.findall(r"\b(total|used|free)\s*=\s*([^\s)]+)", text, flags=re.IGNORECASE):
        values[name.lower()] = parse_sysctl_size(raw_value)
    return values["total"], values["used"], values["free"]


def read_swap_stats(stats: MemoryStats) -> None:
    try:
        proc = subprocess.run(["sysctl", "-n", "vm.swapusage"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        return
    if proc.returncode != 0:
        return
    text = proc.stdout.decode("utf-8", "ignore")
    stats.swap_total_bytes, stats.swap_used_bytes, stats.swap_free_bytes = parse_swapusage(text)


def read_memory_stats() -> MemoryStats:
    stats = MemoryStats()
    try:
        total_proc = subprocess.run(["sysctl", "-n", "hw.memsize"], check=False, capture_output=True, timeout=1.0)
        if total_proc.returncode == 0:
            stats.total_bytes = int(total_proc.stdout.decode("utf-8", "ignore").strip())
    except Exception:
        pass
    try:
        proc = subprocess.run(["vm_stat"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        read_swap_stats(stats)
        return stats
    if proc.returncode != 0:
        read_swap_stats(stats)
        return stats
    text = proc.stdout.decode("utf-8", "ignore")
    page_size = 4096
    page_match = re.search(r"page size of (\d+) bytes", text)
    if page_match:
        page_size = int(page_match.group(1))
    pages: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+([\d,.]+)", line.strip())
        if not match:
            continue
        key = match.group(1).strip().lower()
        pages[key] = int(match.group(2).replace(",", "").rstrip("."))
    free_pages = pages.get("pages free", 0) + pages.get("pages speculative", 0)
    active_pages = pages.get("pages active", 0)
    purgeable_pages = pages.get("pages purgeable", 0)
    wired_pages = pages.get("pages wired down", 0)
    compressed_pages = pages.get("pages occupied by compressor", 0)
    file_backed_pages = pages.get("file-backed pages", 0)
    cached_pages = file_backed_pages + purgeable_pages

    stats.free_bytes = bytes_from_pages(free_pages, page_size)
    stats.cached_bytes = bytes_from_pages(cached_pages, page_size)
    stats.available_bytes = stats.free_bytes + stats.cached_bytes
    stats.active_bytes = bytes_from_pages(active_pages, page_size)
    stats.wired_bytes = bytes_from_pages(wired_pages, page_size)
    stats.compressed_bytes = bytes_from_pages(compressed_pages, page_size)

    if stats.total_bytes > 0:
        stats.available_bytes = min(stats.total_bytes, stats.available_bytes)
    try:
        pressure_proc = subprocess.run(["memory_pressure"], check=False, capture_output=True, timeout=1.0)
        if pressure_proc.returncode == 0:
            pressure_text = pressure_proc.stdout.decode("utf-8", "ignore")
            match = re.search(r"System-wide memory free percentage:\s+(\d+(?:\.\d+)?)%", pressure_text)
            if match:
                stats.system_free_pct = clamp(float(match.group(1)), 0.0, 100.0)
    except Exception:
        pass
    read_swap_stats(stats)
    return stats


def read_disk_total_bytes_from_iostat() -> int:
    try:
        proc = subprocess.run(["iostat", "-Id", "-c", "1"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        return 0
    if proc.returncode != 0:
        return 0
    total_mb = 0.0
    for line in proc.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            total_mb += float(parts[2])
        except ValueError:
            continue
    return int(total_mb * 1024 * 1024)


def read_disk_counters() -> tuple[int, int]:
    try:
        proc = subprocess.run(
            ["ioreg", "-r", "-c", "IOBlockStorageDriver", "-k", "Statistics", "-a"],
            check=False,
            capture_output=True,
            timeout=1.0,
        )
    except Exception:
        total = read_disk_total_bytes_from_iostat()
        return total, 0
    if proc.returncode != 0 or not proc.stdout:
        total = read_disk_total_bytes_from_iostat()
        return total, 0
    try:
        items = plistlib.loads(proc.stdout)
    except Exception:
        total = read_disk_total_bytes_from_iostat()
        return total, 0

    read_bytes = 0
    write_bytes = 0

    def walk(obj: Any) -> None:
        nonlocal read_bytes, write_bytes
        if isinstance(obj, dict):
            if obj.get("IOObjectClass") == "IOBlockStorageDriver":
                stats = obj.get("Statistics")
                if isinstance(stats, dict):
                    read_bytes += int(dict_number(stats, "Bytes (Read)") or 0)
                    write_bytes += int(dict_number(stats, "Bytes (Write)") or 0)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(items)
    if read_bytes == 0 and write_bytes == 0:
        total = read_disk_total_bytes_from_iostat()
        return total, 0
    return read_bytes, write_bytes


def read_network_bytes() -> tuple[int, int]:
    try:
        proc = subprocess.run(["netstat", "-ibn"], check=False, capture_output=True, timeout=1.0)
    except Exception:
        return 0, 0
    if proc.returncode != 0:
        return 0, 0
    seen: set[str] = set()
    in_bytes = 0
    out_bytes = 0
    ignored_prefixes = ("lo", "gif", "stf", "awdl", "llw", "utun")
    for line in proc.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 11 or not parts[2].startswith("<Link#"):
            continue
        name = parts[0].rstrip("*")
        if name in seen or name.startswith(ignored_prefixes):
            continue
        seen.add(name)
        try:
            in_bytes += int(parts[6])
            out_bytes += int(parts[9])
        except ValueError:
            continue
    return in_bytes, out_bytes


def read_io_snapshot() -> IoSnapshot:
    net_in, net_out = read_network_bytes()
    disk_read, disk_write = read_disk_counters()
    return IoSnapshot(disk_read_bytes=disk_read, disk_write_bytes=disk_write, net_in_bytes=net_in, net_out_bytes=net_out)


def io_stats_from_snapshots(previous: IoSnapshot | None, current: IoSnapshot) -> IoStats:
    if previous is None:
        return IoStats()
    elapsed = max(0.001, current.timestamp - previous.timestamp)
    return IoStats(
        disk_read_bps=max(0, current.disk_read_bytes - previous.disk_read_bytes) / elapsed,
        disk_write_bps=max(0, current.disk_write_bytes - previous.disk_write_bytes) / elapsed,
        net_in_bps=max(0, current.net_in_bytes - previous.net_in_bytes) / elapsed,
        net_out_bps=max(0, current.net_out_bytes - previous.net_out_bytes) / elapsed,
    )


def parse_process_line(line: str) -> ProcessInfo | None:
    parts_with_user = line.strip().split(None, 7)
    has_user_columns = (
        len(parts_with_user) >= 8
        and parts_with_user[2].isdigit()
        and re.match(r"^(?:\d+-)?\d{1,2}:\d{2}(?::\d{2})?$", parts_with_user[3])
    )
    user = ""
    if has_user_columns:
        pid_text, user, ppid_text, etime, cpu_text, mem_text, rss_text, command_text = parts_with_user
    else:
        parts = line.strip().split(None, 6)
        has_extended_columns = len(parts) >= 7 and parts[1].isdigit() and re.match(r"^(?:\d+-)?\d{1,2}:\d{2}(?::\d{2})?$", parts[2])
        if has_extended_columns:
            pid_text, ppid_text, etime, cpu_text, mem_text, rss_text, command_text = parts
        elif len(parts) >= 5:
            pid_text, cpu_text, mem_text, rss_text, command_text = line.strip().split(None, 4)
            ppid_text = ""
            etime = ""
        else:
            return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    try:
        ppid = int(ppid_text) if ppid_text else None
    except ValueError:
        ppid = None
    cpu_pct = parse_number_text(cpu_text)
    mem_pct = parse_number_text(mem_text)
    rss_kib = parse_number_text(rss_text.replace(",", ""))
    if cpu_pct is None or mem_pct is None or rss_kib is None:
        return None
    command = os.path.basename(command_text) or command_text
    return ProcessInfo(pid, cpu_pct, mem_pct, int(rss_kib), command, ppid, etime, command_text, user)


def read_processes() -> list[ProcessInfo]:
    commands = (
        ["/bin/ps", "-axo", "pid=,user=,ppid=,etime=,pcpu=,pmem=,rss=,comm="],
        ["/bin/ps", "-axo", "pid=,user=,ppid=,etime=,pcpu=,pmem=,rss=,command="],
        ["ps", "-axo", "pid=,user=,ppid=,etime=,pcpu=,pmem=,rss=,comm="],
    )
    processes: list[ProcessInfo] = []
    for command in commands:
        try:
            proc = subprocess.run(command, check=False, capture_output=True, timeout=2.0)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        processes = [
            process
            for line in proc.stdout.decode("utf-8", "ignore").splitlines()
            if (process := parse_process_line(line)) is not None
        ]
        if processes:
            break
    return processes


def side_metrics_worker(updates: queue.Queue[SideMetricsUpdate], stop_event: threading.Event) -> None:
    previous_io: IoSnapshot | None = None
    next_memory = 0.0
    next_io = 0.0
    next_process = 0.0
    while not stop_event.is_set():
        now = time.monotonic()
        slept = False
        if now >= next_memory:
            try:
                updates.put(SideMetricsUpdate(memory=read_memory_stats(), battery=read_battery_stats()))
            except Exception:
                pass
            next_memory = time.monotonic() + MEMORY_BATTERY_INTERVAL
        now = time.monotonic()
        if now >= next_io:
            try:
                current_io = read_io_snapshot()
                updates.put(SideMetricsUpdate(io_stats=io_stats_from_snapshots(previous_io, current_io)))
                previous_io = current_io
            except Exception:
                pass
            next_io = time.monotonic() + IO_POLL_INTERVAL
        now = time.monotonic()
        if now >= next_process:
            try:
                updates.put(SideMetricsUpdate(processes=read_processes()))
            except Exception:
                pass
            next_process = time.monotonic() + PROCESS_POLL_INTERVAL
        next_due = min(next_memory, next_io, next_process)
        timeout = max(0.05, min(0.5, next_due - time.monotonic()))
        slept = stop_event.wait(timeout)
        if slept:
            break


def sorted_processes(processes: list[ProcessInfo], sort_key: str) -> list[ProcessInfo]:
    if sort_key == "ram":
        return sorted(processes, key=lambda item: (item.mem_pct, item.rss_kib, item.cpu_pct), reverse=True)
    if sort_key == "pid":
        return sorted(processes, key=lambda item: item.pid)
    if sort_key == "name":
        return sorted(processes, key=lambda item: item.command.lower())
    return sorted(processes, key=lambda item: (item.cpu_pct, item.mem_pct, item.rss_kib), reverse=True)


def cycle_value(values: tuple[str, ...], current: str, delta: int) -> str:
    if current not in values:
        return values[0]
    return values[(values.index(current) + delta) % len(values)]


class HIDTemperatureReader:
    def __init__(self) -> None:
        self.available = False
        self.client: ctypes.c_void_p | None = None
        self.match: ctypes.c_void_p | None = None
        self.product_key: ctypes.c_void_p | None = None
        self._keepalive: list[ctypes.c_void_p] = []
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
        if not self.available:
            return None, None
        services = None
        acc_temps: list[float] = []
        die_temps: list[float] = []
        soc_temps: list[float] = []
        try:
            if not self.client or not self.product_key:
                return None, None
            services = self.iokit.IOHIDEventSystemClientCopyServices(self.client)
            if not services:
                return None, None
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
                if name.startswith(("eACC", "pACC")):
                    acc_temps.append(temp)
                elif name.startswith("PMU tdie") or name.startswith("PMU2 tdie"):
                    die_temps.append(temp)
                elif name.startswith("SOC MTR Temp Sensor"):
                    soc_temps.append(temp)
            temps = acc_temps or die_temps or soc_temps
            if not temps:
                return None, None
            return average(temps), max(temps)
        except Exception:
            return None, None
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


def is_root_process() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def powermetrics_command(interval_ms: int, sample_count: int | str) -> list[str]:
    command = [
        "powermetrics",
        "--samplers",
        POWER_SAMPLERS,
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
    if os.name == "posix" and not is_root_process():
        return ["sudo", "-n", *command]
    return command


def ensure_powermetrics_access(args: argparse.Namespace) -> None:
    if args.mock or os.name != "posix" or is_root_process():
        return
    if shutil.which("sudo") is None:
        print(f"{APP_NAME} needs sudo for powermetrics, but sudo was not found.", file=sys.stderr)
        sys.exit(1)
    print(f"{APP_NAME} keeps the UI unprivileged and asks sudo only for powermetrics.")
    proc = subprocess.run(["sudo", "-v"], check=False)
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def apply_hid_temperatures(sample: MetricSample) -> None:
    avg_temp, max_temp = HID_TEMPS.read()
    sample.soc_temp_c = first_non_none(sample.soc_temp_c, avg_temp)
    sample.temp_max_c = first_non_none(sample.temp_max_c, max_temp, sample.soc_temp_c)


class PowerMetricsStream:
    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self.proc: subprocess.Popen[bytes] | None = None
        self.stop_event = threading.Event()
        self.stderr_chunks: deque[str] = deque(maxlen=8)
        self.stderr_thread: threading.Thread | None = None

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

    def samples(self) -> Iterable[MetricSample]:
        self.proc = subprocess.Popen(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
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
                        refresh_battery_power(sample)
                        if sample.soc_temp_c is None or sample.temp_max_c is None:
                            apply_hid_temperatures(sample)
                        yield sample
                except Exception as exc:
                    yield MetricSample(warning=f"plist parse failed: {exc}")
        if self.stderr_chunks:
            yield MetricSample(warning="\n".join(self.stderr_chunks))

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
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
                raw_keys=42,
            )
            time.sleep(self.interval_ms / 1000.0)

    def stop(self) -> None:
        self.stop_event.set()


class History:
    def __init__(self, length: int) -> None:
        self.length = length
        self.soc_power = deque(maxlen=length)
        self.cpu_power = deque(maxlen=length)
        self.gpu_power = deque(maxlen=length)
        self.ane_power = deque(maxlen=length)
        self.cpu_usage = deque(maxlen=length)
        self.gpu_usage = deque(maxlen=length)
        self.ane_usage = deque(maxlen=length)
        self.core_usage: dict[str, deque] = {}
        self.temp = deque(maxlen=length)
        self.disk_read_io = deque(maxlen=length)
        self.disk_write_io = deque(maxlen=length)
        self.net_in_io = deque(maxlen=length)
        self.net_out_io = deque(maxlen=length)

    def add(self, sample: MetricSample) -> None:
        cpu_usage = sample.cpu_usage_pct
        if cpu_usage is None and sample.p_usage_pct is not None and sample.e_usage_pct is not None:
            cpu_usage = (sample.p_usage_pct + sample.e_usage_pct) / 2.0
        self.soc_power.append(effective_total_power_mw(sample))
        self.cpu_power.append(sample.cpu_power_mw)
        self.gpu_power.append(sample.gpu_power_mw)
        self.ane_power.append(sample.ane_power_mw)
        self.cpu_usage.append(cpu_usage)
        self.gpu_usage.append(sample.gpu_usage_pct)
        self.ane_usage.append(sample.ane_usage_pct)
        for core in sample.cores:
            if core.label not in self.core_usage:
                self.core_usage[core.label] = deque(maxlen=self.length)
            self.core_usage[core.label].append(core.usage_pct)
        self.temp.append(sample.soc_temp_c)

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

    def resize(self, length: int) -> None:
        if length <= self.length:
            return
        self.length = length
        self.soc_power = deque(self.soc_power, maxlen=length)
        self.cpu_power = deque(self.cpu_power, maxlen=length)
        self.gpu_power = deque(self.gpu_power, maxlen=length)
        self.ane_power = deque(self.ane_power, maxlen=length)
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


def selected_power_label(mode: str) -> str:
    return {"soc": "SoC", "cpu": "CPU", "gpu": "GPU", "ane": "ANE/NPU"}.get(mode, "CPU")


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


def peak_power(values: Iterable[float | None]) -> float | None:
    present = finite_tail(values)
    if not present:
        return None
    return max(present)


def power_history_for_row(history: History, mode: str) -> deque:
    return selected_power_history(history, mode)


def keep_last_nonzero_frequencies(sample: MetricSample, cache: dict[str, float]) -> None:
    for attr in ("p_freq_mhz", "e_freq_mhz", "gpu_freq_mhz"):
        value = getattr(sample, attr)
        if value is not None and value > 0:
            cache[attr] = value
        elif attr in cache:
            setattr(sample, attr, cache[attr])


def fmt_power(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f} W"
    return f"{value:.0f} mW"


def fmt_bytes(value: int | None) -> str:
    if value is None or value <= 0:
        return "n/a"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{amount:.0f} {unit}"
    if amount >= 10:
        return f"{amount:.1f} {unit}"
    return f"{amount:.2f} {unit}"


def fmt_bytes_zero(value: int | None) -> str:
    if value is None:
        return "n/a"
    if value <= 0:
        return "0 B"
    return fmt_bytes(value)


def fmt_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{fmt_bytes_zero(int(value))}/s"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:4.0f}%"


def fmt_freq(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value <= 0:
        return "idle"
    if value >= 1000:
        return f"{value / 1000:.2f} GHz"
    return f"{value:.0f} MHz"


def fmt_temp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} C"


def fmt_minutes(value: int | None) -> str:
    if value is None or value < 0:
        return "n/a"
    hours, minutes = divmod(value, 60)
    if hours:
        return f"{hours}h{minutes:02d}"
    return f"{minutes}m"


def interval_step(value: float) -> float:
    if value <= 1.0:
        return 0.1
    return 0.5


def interval_text(value: float) -> str:
    return f"{value:.1f}s"


def safe_addstr(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y or x >= max_x:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: max(0, max_x - x - 1)]
        if text:
            win.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_box(win: curses.window, y: int, x: int, h: int, w: int, title: str, attr: int) -> None:
    if h < 3 or w < 8:
        return
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
    safe_addstr(win, y, x + 2, f" {title} ", attr)


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


def draw_bar(win: curses.window, y: int, x: int, w: int, value: float | None, attr: int) -> None:
    if w <= 0:
        return
    if value is None:
        safe_addstr(win, y, x, "." * w, attr)
        return
    filled = int(round(clamp(value, 0, 100) / 100.0 * w))
    safe_addstr(win, y, x, "#" * filled + "-" * (w - filled), attr)


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
        if strongest >= 90:
            attr = colors["bad"]
        elif strongest >= 60:
            attr = colors["warn"]
        elif strongest > 0:
            attr = colors["good"]
        else:
            attr = colors["dim"]
        safe_addstr(win, y, x + idx, chr(0x2800 + mask), attr)


def graph_points(values: Iterable[float | None], width: int, height: int, max_value: float | None) -> list[int | None]:
    vals = list(values)[-width:]
    if len(vals) < width:
        vals = [None] * (width - len(vals)) + vals
    present = [v for v in vals if v is not None]
    if not present:
        return [None] * width
    scale = max_value or max(present) or 1.0
    scale = max(scale, 1.0)
    points: list[int | None] = []
    for value in vals:
        if value is None:
            points.append(None)
        else:
            points.append(int(round(clamp(value, 0, scale) / scale * (height - 1))))
    return points


def draw_graph(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    values: Iterable[float | None],
    *,
    max_value: float | None,
    attr: int,
) -> None:
    if h <= 0 or w <= 0:
        return
    points = graph_points(values, w, h, max_value)
    for row in range(h):
        line = []
        threshold = h - 1 - row
        for point in points:
            if point is None:
                line.append(" ")
            elif point >= threshold:
                line.append("#")
            else:
                line.append(" ")
        safe_addstr(win, y + row, x, "".join(line), attr)


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
    colors: dict[str, int],
) -> None:
    parts = [
        ("upper ", None, None),
        ("[S]", "upper", "soc"),
        ("oC ", "upper", "soc"),
        ("[C]", "upper", "cpu"),
        ("PU ", "upper", "cpu"),
        ("[G]", "upper", "gpu"),
        ("PU ", "upper", "gpu"),
        ("[A]", "upper", "ane"),
        ("NE ", "upper", "ane"),
        ("u cycle  /  lower ", None, None),
        ("s", "lower", "soc"),
        ("/", None, None),
        ("c", "lower", "cpu"),
        ("/", None, None),
        ("g", "lower", "gpu"),
        ("/", None, None),
        ("a", "lower", "ane"),
        (" n cycle", None, None),
    ]
    total_width = sum(len(text) for text, _, _ in parts)
    x = max(1, center_x - total_width // 2)
    for text, side, mode in parts:
        attr = colors["fg"]
        if side == "upper" and mode == upper_mode:
            attr = colors["good"] | curses.A_BOLD
        elif side == "lower" and mode == lower_mode:
            attr = colors["accent"] | curses.A_BOLD
        elif side:
            attr = colors["warn"] | curses.A_BOLD
        safe_addstr(win, y, x, text, attr)
        x += len(text)


def power_graph_attr(value: float | None, scale: float, colors: dict[str, int]) -> int:
    if value is None or value <= 0 or scale <= 0:
        return colors["dim"]
    ratio = clamp(value / scale * 100.0, 0.0, 100.0)
    if ratio >= 85:
        return colors["bad"]
    if ratio >= 55:
        return colors["warn"]
    return colors["good"]


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

    total = effective_total_power_mw(sample)
    upper_value = selected_power_value(sample, upper_mode)
    lower_value = selected_power_value(sample, lower_mode)
    live_label = (
        f"SoC {fmt_power(total)}  CPU {fmt_power(sample.cpu_power_mw)}  "
        f"GPU {fmt_power(sample.gpu_power_mw)}  ANE {fmt_power(sample.ane_power_mw)}"
    )
    if len(live_label) < w - 18:
        safe_addstr(win, y, x + w - len(live_label) - 3, f" {live_label} ", colors["fg"])
    else:
        top_label = f"Upper {selected_power_label(upper_mode)} {fmt_power(upper_value)}"
        bottom_label = f"Lower {selected_power_label(lower_mode)} {fmt_power(lower_value)}"
        safe_addstr(win, y, x + w - len(top_label) - len(bottom_label) - 8, f" {top_label} ", colors["good"])
        safe_addstr(win, y, x + w - len(bottom_label) - 3, f" {bottom_label} ", colors["accent"])
    scale_label = f"scale {fmt_power(shared_scale)}"
    safe_addstr(win, y + h - 1, x + max(2, w - len(scale_label) - 3), f" {scale_label} ", colors["muted"])
    draw_power_mode_legend(win, baseline, x + w // 2, upper_mode, lower_mode, colors)


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


def load_graph_attr(value: float | None, colors: dict[str, int]) -> int:
    if value is None or value <= 0:
        return colors["dim"]
    if value >= 90:
        return colors["bad"]
    if value >= 60:
        return colors["warn"]
    return colors["good"]


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


def draw_memory_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    memory: MemoryStats,
    colors: dict[str, int],
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
    rows: list[tuple[str, str, int, bool]] = [
        ("Phys", fmt_bytes(memory.physical_used_bytes), memory.physical_used_bytes, True),
        ("Free", fmt_bytes(memory.free_bytes), memory.free_bytes, True),
        ("Cache", fmt_bytes(memory.cached_bytes), memory.cached_bytes, True),
        ("Reclaim", fmt_bytes(memory.available_bytes), memory.available_bytes, True),
        ("Active", fmt_bytes(memory.active_bytes), memory.active_bytes, True),
        ("Wired", fmt_bytes(memory.wired_bytes), memory.wired_bytes, True),
        ("Swap", swap_text, memory.swap_used_bytes, False),
        ("Compr", fmt_bytes(memory.compressed_bytes), memory.compressed_bytes, True),
    ]
    row_space = max(0, h - (rows_start - y))
    for idx, (name, text, value, good_when_present) in enumerate(rows[:row_space]):
        yy = rows_start + idx
        value_w = min(18, max(8, len(text)))
        safe_addstr(win, yy, x, name, colors["muted"])
        safe_addstr(
            win,
            yy,
            x + w - value_w,
            f"{text:>{value_w}}",
            colors["good"] if value and good_when_present else colors["warn"] if value else colors["muted"],
        )


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
) -> None:
    draw_box(win, y, x, h, w, "POWER", colors["accent"])
    if h < 5 or w < 34:
        return
    avg_count = max(1, int(round(30.0 / max(interval_s, MIN_INTERVAL))))
    value_w = 9 if w >= 48 else 8
    avg_x = max(x + 18, x + w - value_w * 2 - 3)
    max_x = x + w - value_w - 2
    if avg_x > x + 16 and max_x > avg_x:
        safe_addstr(win, y + 1, avg_x, "30s avg"[:value_w], colors["muted"])
        safe_addstr(win, y + 1, max_x, "peak"[:value_w], colors["muted"])
    rows = [
        ("SoC", "soc", effective_total_power_mw(sample)),
        ("CPU", "cpu", sample.cpu_power_mw),
        ("GPU", "gpu", sample.gpu_power_mw),
        ("ANE", "ane", sample.ane_power_mw),
    ]
    for idx, (name, mode, current) in enumerate(rows[: max(0, h - 4)]):
        yy = y + 2 + idx
        values = power_history_for_row(history, mode)
        avg = avg_power(values, avg_count)
        peak = peak_power(values)
        safe_addstr(win, yy, x + 2, name, colors["muted"])
        safe_addstr(win, yy, x + 8, fmt_power(current), colors["fg"])
        if max_x > avg_x:
            safe_addstr(win, yy, avg_x, f"{fmt_power(avg):>{value_w}}"[-value_w:], colors["fg"])
            safe_addstr(win, yy, max_x, f"{fmt_power(peak):>{value_w}}"[-value_w:], colors["warn"] if peak else colors["muted"])
    extra_rows = [
        ("Media", fmt_power(sample.media_power_mw)),
        ("Battery", fmt_power(first_non_none(battery.power_mw, sample.battery_power_mw))),
    ]
    start = y + 2 + min(len(rows), max(0, h - 4))
    for idx, (name, value) in enumerate(extra_rows[: max(0, y + h - 1 - start)]):
        yy = start + idx
        safe_addstr(win, yy, x + 2, name, colors["muted"])
        safe_addstr(win, yy, x + 8, value, colors["fg"])


def draw_battery_section(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryStats,
    colors: dict[str, int],
) -> None:
    if h < 3 or w < 24:
        return
    state = "charging" if battery.charging else "plugged" if battery.external_connected else "discharging"
    rows = [
        ("Charge", fmt_pct(battery.charge_pct).strip()),
        ("State", state),
        ("Power", fmt_power(battery.power_mw)),
        ("Time", fmt_minutes(battery.time_remaining_min)),
        ("Health", fmt_pct(battery.health_pct).strip()),
        ("Cycles", str(battery.cycle_count) if battery.cycle_count is not None else "n/a"),
        (
            "Design",
            f"{battery.raw_max_capacity or battery.max_capacity}/{battery.design_capacity} mAh"
            if battery.design_capacity and (battery.raw_max_capacity or battery.max_capacity)
            else "n/a",
        ),
    ]
    for idx, (name, value) in enumerate(rows[:h]):
        safe_addstr(win, y + idx, x, name, colors["muted"])
        attr = colors["fg"]
        if name == "Power" and battery.power_mw is not None and battery.power_mw < -HIGH_BATTERY_DRAIN_MW:
            attr = colors["warn"]
        elif name == "Health" and battery.health_pct is not None:
            attr = colors["good"] if battery.health_pct >= 80 else colors["warn"]
        safe_addstr(win, y + idx, x + min(12, max(8, w // 3)), value[: max(1, w - 14)], attr)


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
    draw_box(win, y, x, h, w, "PROCESSES", colors["accent"])
    safe_addstr(win, y, x + 12, "p", colors["warn"] | curses.A_BOLD)
    hint = f" sort {sort_key}  arrows move/sort  k kill "
    if w > len(hint) + 16:
        safe_addstr(win, y, x + w - len(hint) - 2, hint, colors["muted"])
    if h < 5 or w < 42:
        return
    ordered = sorted_processes(processes, sort_key)
    if not ordered:
        safe_addstr(win, y + 2, x + 2, "no process data", colors["muted"])
        return
    selected_index = int(clamp(selected_index, 0, len(ordered) - 1))
    details_h = 4 if h >= 12 else 0
    rows_h = h - 3 - details_h
    offset = max(0, min(selected_index - rows_h // 2, max(0, len(ordered) - rows_h)))
    header_y = y + 1
    sort_attr = colors["warn"] | curses.A_BOLD
    safe_addstr(win, header_y, x + 2, "PID", sort_attr if sort_key == "pid" else colors["muted"])
    safe_addstr(win, header_y, x + 10, "CPU%", sort_attr if sort_key == "cpu" else colors["muted"])
    safe_addstr(win, header_y, x + 17, "RAM%", sort_attr if sort_key == "ram" else colors["muted"])
    safe_addstr(win, header_y, x + 24, "RSS", colors["muted"])
    safe_addstr(win, header_y, x + 35, "COMMAND", sort_attr if sort_key == "name" else colors["muted"])
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
        safe_addstr(win, yy, x + 17, f"{proc.mem_pct:5.1f}", colors["fg"])
        safe_addstr(win, yy, x + 24, f"{fmt_bytes_zero(proc.rss_kib * 1024):>9}"[-9:], colors["fg"])
        safe_addstr(win, yy, x + 35, proc.command[: max(1, w - 37)], attr)
    if details_h:
        selected = ordered[selected_index]
        detail_y = y + h - details_h
        safe_addstr(win, detail_y, x + 2, "─" * max(1, w - 4), colors["dim"])
        detail = (
            f"PID {selected.pid}"
            + (f"  user {selected.user}" if selected.user else "")
            + (f"  PPID {selected.ppid}" if selected.ppid is not None else "")
            + (f"  time {selected.etime}" if selected.etime else "")
            + f"  CPU {selected.cpu_pct:.1f}%  RAM {selected.mem_pct:.1f}%"
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
    if value is None or value <= 0 or scale <= 0:
        return colors["dim"]
    ratio = clamp(value / scale * 100.0, 0.0, 100.0)
    if ratio >= 85:
        return colors["bad"]
    if ratio >= 55:
        return colors["warn"]
    return colors["good"]


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
    legend = f"i {upper_label} / o {lower_label}"
    if len(legend) < w:
        safe_addstr(win, baseline, x + max(0, w // 2 - len(legend) // 2), f" {legend} ", colors["fg"])


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
        alerts.append(f"TEMP {sample.temp_max_c:.0f}C")
    if memory.swap_used_bytes >= high_swap_bytes:
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
    safe_addstr(win, y, x + 13, "L", colors["warn"] | curses.A_BOLD)
    hint = "l avg rows" if load_view == "graph" else "l avg graph"
    if w > len(hint) + 8:
        safe_addstr(win, y, x + w - len(hint) - 3, f" {hint} ", colors["muted"])
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
    pairs["bold"] = pairs["fg"] | curses.A_BOLD
    return pairs


def source_status(args: argparse.Namespace, sample: MetricSample, battery: BatteryStats, processes: list[ProcessInfo]) -> str:
    sources = ["mock" if args.mock else "pm"]
    if sample.soc_temp_c is not None or sample.temp_max_c is not None:
        sources.append("temp")
    sources.append("vm")
    if battery.power_mw is not None or battery.temperature_c is not None:
        sources.append("ioreg")
    if args.show_io:
        sources.append("io")
    if processes:
        sources.append("ps")
    return "src " + ",".join(sources)


def draw_help_overlay(win: curses.window, colors: dict[str, int]) -> None:
    max_y, max_x = win.getmaxyx()
    rows = [
        ("q", "quit", f"close {APP_NAME}"),
        ("?", "help", "toggle this overlay"),
        ("m", "menu", "edit settings"),
        ("t", "theme", "cycle color themes"),
        ("+ / -", "interval", "change sampler interval"),
        ("v", "layout", "cycle full/compact/focused layouts"),
        ("d", "disk/net", "show or hide I/O graph"),
        ("i / o", "I/O source", "cycle upper/lower disk-net graph"),
        ("S/C/G/A", "upper power", "select SoC/CPU/GPU/ANE"),
        ("s/c/g/a", "lower power", "select SoC/CPU/GPU/ANE"),
        ("u / n", "power cycle", "cycle upper/lower power graph"),
        ("L", "load view", "toggle CPU/GPU avg rows/graph"),
        ("p", "process panel", "hidden -> left -> right"),
        ("Up/Down", "process select", "move process cursor"),
        ("Left/Right", "process sort", "cycle CPU/RAM/PID/name"),
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
        return selected_power_label(upper_power_mode)
    if item_id == "lower_power":
        return selected_power_label(lower_power_mode)
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
    if item_id == "allow_root_kill":
        return "on" if bool(getattr(args, "allow_root_kill", False)) else "off"
    if item_id == "alert_temp":
        return f"{float(args.alert_temp_c):.1f} C"
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
        value = menu_value_text(item_id, args, upper_power_mode, lower_power_mode, upper_io_mode, lower_io_mode, load_view, process_panel, process_sort)
        safe_addstr(win, yy, x + 2, marker, colors["warn"] | curses.A_BOLD if active else colors["muted"])
        safe_addstr(win, yy, x + 4, f"{label:<{label_w}}"[:label_w], attr)
        safe_addstr(win, yy, x + 6 + label_w, f"{value:<{value_w}}"[:value_w], attr)
        desc_x = x + 8 + label_w + value_w
        safe_addstr(win, yy, desc_x, desc[: max(1, x + w - 2 - desc_x)], colors["muted"])
    footer = "Up/Down select  Left/Right or Enter change  Tab next  s save  Esc close"
    safe_addstr(win, y + h - 1, x + max(2, w - len(footer) - 2), footer[: max(1, w - 4)], colors["muted"])


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
        )


def draw_dashboard(
    stdscr: curses.window,
    sample: MetricSample | None,
    history: History,
    memory: MemoryStats,
    battery: BatteryStats,
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
    process_selected: int,
    pending_kill_pid: int | None,
    help_visible: bool,
    menu_visible: bool,
    menu_selected: int,
) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    if max_y < 24 or max_x < 72:
        safe_addstr(stdscr, 0, 0, f"{APP_NAME}: terminal too small; try at least 72x24", colors["warn"])
        stdscr.refresh()
        return

    if sample is None:
        title = f" {APP_NAME} {VERSION}  {interval_text(args.interval)}  {args.theme}  {args.layout}  q quit  m menu  ? help "
        safe_addstr(stdscr, 0, 1, title[: max_x - 2], colors["bold"])
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
        )
        stdscr.refresh()
        return

    sample = sample or MetricSample(warning="waiting for powermetrics sample")
    title = (
        f" {APP_NAME} {VERSION}  {interval_text(args.interval)}  {args.theme}  {args.layout}  "
        "q quit  m menu  ? help  +/- interval  p proc  u/n power  L avg "
    )
    safe_addstr(stdscr, 0, 1, title[: max_x - 2], colors["bold"])
    alerts = build_alerts(sample, memory, battery, args)
    status_attr = colors["bad"] | curses.A_BOLD if any(alert in ("THROTTLE",) for alert in alerts) else colors["warn"] if alerts else colors["muted"]
    source_text = source_status(args, sample, battery, processes)
    status_text = f"{status}  {source_text}"
    if alerts:
        status_text += f"  ALERT {' | '.join(alerts)}"
    if status_text:
        safe_addstr(stdscr, 1, 1, status_text[: max_x - 2], status_attr)

    if args.layout == "compact":
        graph_h = 7
    elif args.layout in {"power-only", "thermals-only"}:
        graph_h = max(9, min(14, max_y // 3))
    else:
        graph_h = max(7, min(11, max_y // 5))
    draw_split_power_graph(stdscr, 2, 0, graph_h, max_x, history, sample, upper_power_mode, lower_power_mode, colors)

    info_y = graph_h + 2
    top_h = 7 if args.layout == "compact" else 9
    left_w = max_x // 2
    right_w = max_x - left_w
    draw_box(stdscr, info_y, left_w, top_h, right_w, "THERMALS", color_for_thermal(sample, colors))
    draw_power_section(stdscr, info_y, 0, top_h, left_w, sample, history, battery, args.interval, colors)

    throttle_text = "yes" if sample.throttled else "no" if sample.throttled is False else "unknown"
    thermal_rows = [
        ("Pressure", sample.thermal_pressure or "n/a"),
        ("Throttled", throttle_text),
        ("Temp avg", fmt_temp(sample.soc_temp_c)),
        ("Temp max", fmt_temp(sample.temp_max_c)),
        ("Batt temp", fmt_temp(battery.temperature_c)),
    ]
    for idx, (name, value) in enumerate(thermal_rows[: max(0, top_h - 2)]):
        safe_addstr(stdscr, info_y + 1 + idx, left_w + 2, name, colors["muted"])
        safe_addstr(stdscr, info_y + 1 + idx, left_w + 14, value, color_for_thermal(sample, colors))
    if sample.throttle_reasons:
        safe_addstr(stdscr, info_y + 6, left_w + 2, "Limit", colors["muted"])
        safe_addstr(stdscr, info_y + 6, left_w + 14, ", ".join(sample.throttle_reasons), colors["bad"])

    if args.layout == "power-only":
        battery_y = info_y + top_h
        battery_h = max_y - battery_y - 1
        if battery_h >= 5:
            draw_box(stdscr, battery_y, 0, battery_h, max_x, "BATTERY", colors["accent"])
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
        )
        stdscr.refresh()
        return

    if args.layout == "thermals-only":
        detail_y = info_y + top_h
        detail_h = max_y - detail_y - 1
        if detail_h >= 5:
            half = max_x // 2
            draw_box(stdscr, detail_y, 0, detail_h, half, "BATTERY", colors["accent"])
            draw_box(stdscr, detail_y, half, detail_h, max_x - half, "RAM", colors["accent"])
            draw_battery_section(stdscr, detail_y + 1, 2, detail_h - 2, half - 4, battery, colors)
            draw_memory_section(stdscr, detail_y + 1, half + 2, detail_h - 2, max_x - half - 4, memory, colors)
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
        )
        stdscr.refresh()
        return

    y2 = info_y + top_h
    remaining_h = max_y - y2 - 1
    bottom_left = max_x // 2
    left_io_panel = args.layout == "full" and args.show_io
    process_left = args.layout == "full" and process_panel == "left"
    process_right = args.layout == "full" and process_panel == "right"
    if process_left:
        draw_process_section(stdscr, y2, 0, remaining_h, bottom_left, processes, process_sort, process_selected, pending_kill_pid, colors)
    else:
        draw_usage_matrix(
            stdscr,
            y2,
            0,
            remaining_h,
            bottom_left,
            sample,
            history,
            colors,
            load_view,
            left_io_panel,
            io_stats,
            upper_io_mode,
            lower_io_mode,
        )

    right_w = max_x - bottom_left
    show_battery_panel = args.layout == "full"
    if remaining_h >= 16:
        clocks_h = 8
    elif remaining_h >= 10:
        clocks_h = 5
    else:
        clocks_h = remaining_h
    battery_h = 8 if show_battery_panel and remaining_h - clocks_h >= 14 else 0
    io_h = 7 if args.show_io and not left_io_panel and remaining_h - clocks_h - battery_h >= 12 else 0
    ram_h = max(0, remaining_h - clocks_h - battery_h - io_h)
    draw_box(stdscr, y2, bottom_left, clocks_h, right_w, "CLOCKS", colors["accent"])
    right_rows = [
        ("P cores", fmt_freq(sample.p_freq_mhz)),
        ("E cores", fmt_freq(sample.e_freq_mhz)),
        ("GPU", fmt_freq(sample.gpu_freq_mhz)),
        ("Temp avg", fmt_temp(sample.soc_temp_c)),
        ("Temp max", fmt_temp(sample.temp_max_c)),
        ("Raw keys", str(sample.raw_keys or "n/a")),
    ]
    max_freq_rows = min(len(right_rows), max(0, clocks_h - 2))
    for idx, (name, value) in enumerate(right_rows[:max_freq_rows]):
        yy = y2 + 1 + idx
        safe_addstr(stdscr, yy, bottom_left + 2, name, colors["muted"])
        safe_addstr(stdscr, yy, bottom_left + 13, value, colors["fg"])
    if process_right and ram_h < 8:
        process_right = False
    if ram_h >= 3 and process_right:
        ram_y = y2 + clocks_h
        draw_process_section(stdscr, ram_y, bottom_left, ram_h, right_w, processes, process_sort, process_selected, pending_kill_pid, colors)
    elif ram_h >= 3:
        ram_y = y2 + clocks_h
        draw_box(stdscr, ram_y, bottom_left, ram_h, right_w, "RAM", colors["accent"])
        draw_memory_section(stdscr, ram_y + 1, bottom_left + 2, ram_h - 2, right_w - 4, memory, colors)
    if battery_h >= 3:
        battery_y = y2 + clocks_h + ram_h
        draw_box(stdscr, battery_y, bottom_left, battery_h, right_w, "BATTERY", colors["accent"])
        draw_battery_section(stdscr, battery_y + 1, bottom_left + 2, battery_h - 2, right_w - 4, battery, colors)
    if io_h >= 3:
        io_y = y2 + clocks_h + ram_h + battery_h
        draw_box(stdscr, io_y, bottom_left, io_h, right_w, "DISK / NET", colors["accent"])
        draw_io_mini_graph(
            stdscr,
            io_y + 1,
            bottom_left + 2,
            io_h - 2,
            right_w - 4,
            io_stats,
            history,
            colors,
            upper_io_mode,
            lower_io_mode,
            heading=False,
        )

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
    )
    stdscr.refresh()


def run_curses(stdscr: curses.window, args: argparse.Namespace) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.timeout(200)
    colors = init_colors(args.theme)
    history = History(args.history)
    sample_queue: queue.Queue[MetricSample] = queue.Queue()
    side_queue: queue.Queue[SideMetricsUpdate] = queue.Queue()
    side_stop = threading.Event()
    stream: MockStream | PowerMetricsStream
    worker: threading.Thread
    latest: MetricSample | None = None
    freq_cache: dict[str, float] = {}
    memory_stats = MemoryStats()
    battery_stats = BatteryStats()
    processes: list[ProcessInfo] = []
    io_stats = IoStats()
    upper_power_mode = args.upper_power_mode if args.upper_power_mode in POWER_MODES else "soc"
    lower_power_mode = args.lower_power_mode if args.lower_power_mode in POWER_MODES else "cpu"
    upper_io_mode = args.upper_io_mode if args.upper_io_mode in IO_MODES else "disk_read"
    lower_io_mode = args.lower_io_mode if args.lower_io_mode in IO_MODES else "net_in"
    load_view = args.load_view if args.load_view in LOAD_VIEWS else "rows"
    process_panel = args.process_panel if args.process_panel in PROCESS_PANEL_MODES else "hidden"
    process_sort = args.process_sort if args.process_sort in PROCESS_SORTS else "cpu"
    process_selected = 0
    pending_kill: PendingKill | None = None
    help_visible = False
    menu_visible = False
    menu_selected = 0
    status = "starting sampler"
    stream_generation = 0

    def save_ui_settings() -> bool:
        nonlocal status
        args.process_panel = process_panel
        args.process_sort = process_sort
        args.upper_power_mode = upper_power_mode
        args.lower_power_mode = lower_power_mode
        args.upper_io_mode = upper_io_mode
        args.lower_io_mode = lower_io_mode
        args.load_view = load_view
        error = save_settings(args)
        if error:
            status = f"settings not saved: {error}"
            return False
        return True

    def apply_menu_change(delta: int) -> None:
        nonlocal upper_power_mode, lower_power_mode, upper_io_mode, lower_io_mode, load_view
        nonlocal process_panel, process_sort, colors, stream, worker, status
        item_id = MENU_ITEMS[menu_selected][0]
        step = 1 if delta >= 0 else -1
        if item_id == "theme":
            names = tuple(THEMES)
            args.theme = cycle_value(names, args.theme, step)
            colors = init_colors(args.theme)
        elif item_id == "layout":
            args.layout = cycle_value(LAYOUTS, args.layout, step)
        elif item_id == "interval":
            if step > 0:
                args.interval = round(min(MAX_INTERVAL, args.interval + interval_step(args.interval)), 1)
            else:
                args.interval = round(max(MIN_INTERVAL, args.interval - interval_step(args.interval)), 1)
            restart_stream()
        elif item_id == "show_io":
            args.show_io = not args.show_io
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
        elif item_id == "process_sort":
            process_sort = cycle_value(PROCESS_SORTS, process_sort, step)
        elif item_id == "allow_root_kill":
            args.allow_root_kill = not bool(getattr(args, "allow_root_kill", False))
        elif item_id == "alert_temp":
            args.alert_temp_c = round(clamp(float(args.alert_temp_c) + step * 1.0, 40.0, 125.0), 1)
        elif item_id == "alert_swap":
            args.alert_swap_gib = round(clamp(float(args.alert_swap_gib) + step * 0.25, 0.0, 1024.0), 2)
        elif item_id == "alert_battery":
            args.alert_battery_drain_w = round(clamp(float(args.alert_battery_drain_w) + step * 1.0, 0.0, 250.0), 1)
        status = f"menu {MENU_ITEMS[menu_selected][1]} {menu_value_text(item_id, args, upper_power_mode, lower_power_mode, upper_io_mode, lower_io_mode, load_view, process_panel, process_sort)}"
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

    def start_stream() -> tuple[MockStream | PowerMetricsStream, threading.Thread]:
        local_stream = make_stream()
        local_worker = threading.Thread(target=pump, args=(local_stream, stream_generation), daemon=True)
        local_worker.start()
        return local_stream, local_worker

    def restart_stream() -> None:
        nonlocal sample_queue, stream, worker, stream_generation
        stream_generation += 1
        stream.stop()
        sample_queue = queue.Queue()
        stream, worker = start_stream()

    stream, worker = start_stream()
    side_worker = threading.Thread(target=side_metrics_worker, args=(side_queue, side_stop), daemon=True)
    side_worker.start()
    themes = list(THEMES)
    layouts = list(LAYOUTS)

    try:
        while True:
            _, max_x = stdscr.getmaxyx()
            history.resize(max(args.history, max_x * 2))
            while True:
                try:
                    latest = sample_queue.get_nowait()
                    keep_last_nonzero_frequencies(latest, freq_cache)
                    history.add(latest)
                    age = time.strftime("%H:%M:%S", time.localtime(latest.timestamp))
                    status = f"last sample {age}"
                except queue.Empty:
                    break
            while True:
                try:
                    update = side_queue.get_nowait()
                    if update.memory is not None:
                        memory_stats = update.memory
                    if update.battery is not None:
                        battery_stats = update.battery
                    if update.io_stats is not None:
                        io_stats = update.io_stats
                        history.add_io(io_stats)
                    if update.processes is not None:
                        processes = update.processes
                        process_selected = min(process_selected, max(0, len(processes) - 1))
                except queue.Empty:
                    break

            draw_dashboard(
                stdscr,
                latest,
                history,
                memory_stats,
                battery_stats,
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
                process_selected,
                pending_kill.pid if pending_kill else None,
                help_visible,
                menu_visible,
                menu_selected,
            )
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                if menu_visible or help_visible:
                    menu_visible = False
                    help_visible = False
                    continue
                break
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
            elif key == ord("?"):
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
            elif key in (ord("t"), ord("T")):
                index = (themes.index(args.theme) + 1) % len(themes)
                args.theme = themes[index]
                colors = init_colors(args.theme)
                save_ui_settings()
            elif key in (ord("v"), ord("V")):
                index = (layouts.index(args.layout) + 1) % len(layouts)
                args.layout = layouts[index]
                status = f"layout {args.layout}"
                save_ui_settings()
            elif key in (ord("d"), ord("D")):
                args.show_io = not args.show_io
                status = "disk/net shown" if args.show_io else "disk/net hidden"
                io_stats = IoStats()
                save_ui_settings()
            elif key in (ord("l"), ord("L")):
                load_view = "graph" if load_view == "rows" else "rows"
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
                if process_panel != "hidden":
                    process_selected = max(0, process_selected - 1)
                    pending_kill = None
            elif key == curses.KEY_DOWN:
                if process_panel != "hidden":
                    process_selected = min(max(0, len(processes) - 1), process_selected + 1)
                    pending_kill = None
            elif key == curses.KEY_LEFT:
                if process_panel != "hidden":
                    process_sort = cycle_value(PROCESS_SORTS, process_sort, -1)
                    process_selected = 0
                    pending_kill = None
                    status = f"process sort {process_sort}"
                    save_ui_settings()
            elif key == curses.KEY_RIGHT:
                if process_panel != "hidden":
                    process_sort = cycle_value(PROCESS_SORTS, process_sort, 1)
                    process_selected = 0
                    pending_kill = None
                    status = f"process sort {process_sort}"
                    save_ui_settings()
            elif key in (ord("k"), ord("K")):
                if process_panel != "hidden":
                    ordered = sorted_processes(processes, process_sort)
                    if 0 <= process_selected < len(ordered):
                        target = ordered[process_selected]
                        now = time.monotonic()
                        root_kill_blocked = is_root_process() and not bool(getattr(args, "allow_root_kill", False))
                        if root_kill_blocked:
                            status = "root process kill disabled in settings"
                            pending_kill = None
                        elif pending_kill is not None and pending_kill.pid == target.pid and now <= pending_kill.until:
                            fresh = next((process for process in read_processes() if process.pid == target.pid), None)
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
                            pending_kill = PendingKill.from_process(target, now + KILL_CONFIRM_SECONDS)
                            status = f"press k again to TERM {target.pid} {target.command}"
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
        side_worker.join(timeout=0.5)


def run_probe(args: argparse.Namespace) -> int:
    ensure_powermetrics_access(args)
    if args.mock:
        sample = next(MockStream(int(args.interval * 1000)).samples())
        print_sample(sample)
        return 0

    cmd = powermetrics_command(int(args.interval * 1000), "1")
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", "ignore") or proc.stdout.decode("utf-8", "ignore"), file=sys.stderr)
        return proc.returncode
    raw = proc.stdout.split(b"\0", 1)[0].strip()
    obj = plistlib.loads(raw)
    sample = sample_from_plist(obj, interval_s=args.interval)
    refresh_battery_power(sample)
    if sample.soc_temp_c is None or sample.temp_max_c is None:
        apply_hid_temperatures(sample)
    print_sample(sample)
    if args.raw:
        print("\nFlattened powermetrics keys:")
        for path, value in sorted(flatten(obj), key=lambda item: item[0].lower()):
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            print(f"{path}: {value}")
    return 0


def print_sample(sample: MetricSample) -> None:
    print(f"CPU power:    {fmt_power(sample.cpu_power_mw)}")
    print(f"GPU power:    {fmt_power(sample.gpu_power_mw)}")
    print(f"ANE/NPU power:{fmt_power(sample.ane_power_mw):>10}")
    print(f"Media power:  {fmt_power(sample.media_power_mw)}")
    print(f"SoC/Total:    {fmt_power(sample.soc_power_mw)}")
    print(f"P usage:      {fmt_pct(sample.p_usage_pct)}  {fmt_freq(sample.p_freq_mhz)}")
    print(f"E usage:      {fmt_pct(sample.e_usage_pct)}  {fmt_freq(sample.e_freq_mhz)}")
    print(f"GPU usage:    {fmt_pct(sample.gpu_usage_pct)}  {fmt_freq(sample.gpu_freq_mhz)}")
    print(f"ANE usage:    {fmt_pct(sample.ane_usage_pct)}")
    print(f"Media usage:  {fmt_pct(sample.media_usage_pct)}")
    print(f"Temp avg:     {fmt_temp(sample.soc_temp_c)}")
    print(f"Temp max:     {fmt_temp(sample.temp_max_c)}")
    print(f"Battery:      {fmt_power(sample.battery_power_mw)}")
    print(f"Thermal:      {sample.thermal_pressure or 'n/a'}")
    print(f"Throttled:    {sample.throttled if sample.throttled is not None else 'unknown'}")
    if sample.throttle_reasons:
        print(f"Limits:       {', '.join(sample.throttle_reasons)}")
    print(f"Raw keys:     {sample.raw_keys or 'n/a'}")


def build_parser() -> argparse.ArgumentParser:
    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="macOS power, thermal and activity monitor for Apple Silicon.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("-i", "--interval", type=float, default=settings.get("interval", 1.0), help="sample interval in seconds")
    parser.add_argument("--history", type=int, default=240, help="number of samples kept for graphs")
    parser.add_argument("-t", "--theme", choices=sorted(THEMES), default=settings.get("theme", "classic"), help="color theme")
    parser.add_argument("--layout", choices=LAYOUTS, default=settings.get("layout", "full"), help="dashboard layout preset")
    parser.add_argument("--show-io", action="store_true", default=settings.get("show_io", False), help="show compact disk/network panel")
    parser.add_argument("--mock", action="store_true", help="run with generated demo data")
    parser.add_argument("--remove-settings", action="store_true", help="remove the saved user settings file and exit")
    parser.set_defaults(
        upper_power_mode=settings.get("upper_power_mode", "soc"),
        lower_power_mode=settings.get("lower_power_mode", "cpu"),
        upper_io_mode=settings.get("upper_io_mode", "disk_read"),
        lower_io_mode=settings.get("lower_io_mode", "net_in"),
        load_view=settings.get("load_view", "rows"),
        process_panel=settings.get("process_panel", "hidden"),
        process_sort=settings.get("process_sort", "cpu"),
        allow_root_kill=settings.get("allow_root_kill", False),
        alert_temp_c=settings.get("alert_temp_c", HIGH_TEMP_C),
        alert_swap_gib=settings.get("alert_swap_gib", DEFAULT_ALERT_SWAP_GIB),
        alert_battery_drain_w=settings.get("alert_battery_drain_w", DEFAULT_ALERT_BATTERY_DRAIN_W),
    )

    subparsers = parser.add_subparsers(dest="command")
    probe = subparsers.add_parser("probe", help="print one parsed powermetrics sample")
    probe.add_argument("--raw", action="store_true", help="also print flattened plist keys")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.interval = round(clamp(args.interval, MIN_INTERVAL, MAX_INTERVAL), 1)
    args.history = int(clamp(args.history, 20, 1000))

    if args.remove_settings:
        error = remove_settings()
        if error:
            print(f"Could not remove settings at {SETTINGS_PATH}: {error}", file=sys.stderr)
            return 1
        print(f"Removed settings at {SETTINGS_PATH}")
        return 0

    if args.command == "probe":
        return run_probe(args)

    ensure_powermetrics_access(args)
    curses.wrapper(run_curses, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
