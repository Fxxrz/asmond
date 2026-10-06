from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SanitizeLayout = Callable[[Any], dict[str, dict[str, str]]]
NormalizeLayout = Callable[[str], str]


@dataclass(frozen=True)
class SettingsSchema:
    app_name: str
    settings_dir_env: str
    settings_filename: str
    themes: tuple[str, ...]
    layouts: tuple[str, ...]
    power_modes: tuple[str, ...]
    io_modes: tuple[str, ...]
    load_views: tuple[str, ...]
    process_panel_modes: tuple[str, ...]
    process_sorts: tuple[str, ...]
    charge_panel_modes: tuple[str, ...]
    custom_slot_ids: tuple[str, ...]
    reserved_custom_names: frozenset[str]
    min_interval: float
    max_interval: float
    high_temp_c: float
    default_alert_swap_gib: float
    default_alert_battery_drain_w: float
    normalize_layout: NormalizeLayout
    sanitize_custom_layout: SanitizeLayout


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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


def default_settings_path_for(app_name: str, settings_dir_env: str, settings_filename: str) -> Path:
    is_root = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0
    override = None if is_root else os.environ.get(settings_dir_env)
    if override:
        return Path(override).expanduser() / settings_filename
    return real_user_home() / "Library" / "Application Support" / app_name / settings_filename


def default_settings_path(schema: SettingsSchema) -> Path:
    return default_settings_path_for(schema.app_name, schema.settings_dir_env, schema.settings_filename)


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


def clean_custom_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned[:32]


def custom_name_error(value: str, reserved_names: frozenset[str]) -> str | None:
    name = clean_custom_name(value)
    if not name:
        return "name is empty"
    if name.casefold() in {reserved.casefold() for reserved in reserved_names}:
        return "name is reserved"
    return None


def setting_choice(args: argparse.Namespace, name: str, default: str, choices: tuple[str, ...]) -> str:
    value = getattr(args, name, default)
    return value if isinstance(value, str) and value in choices else default


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_settings(path: Path, schema: SettingsSchema) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        return {"__warning__": f"settings ignored: {type(exc).__name__}"}
    if not isinstance(data, dict):
        return {"__warning__": "settings ignored: expected a JSON object"}
    settings: dict[str, Any] = {}
    invalid_fields: list[str] = []
    theme = data.get("theme")
    if isinstance(theme, str) and theme in schema.themes:
        settings["theme"] = theme
    interval = data.get("interval")
    if "interval" in data:
        number = finite_number(interval)
        if number is None:
            invalid_fields.append("interval")
        else:
            settings["interval"] = clamp(number, schema.min_interval, schema.max_interval)
    layout = data.get("layout")
    if isinstance(layout, str):
        layout = schema.normalize_layout(layout)
        if layout in schema.layouts:
            settings["layout"] = layout
    show_io = data.get("show_io")
    if isinstance(show_io, bool):
        settings["show_io"] = show_io
    upper_power_mode = data.get("upper_power_mode")
    if isinstance(upper_power_mode, str) and upper_power_mode in schema.power_modes:
        settings["upper_power_mode"] = upper_power_mode
    lower_power_mode = data.get("lower_power_mode")
    if isinstance(lower_power_mode, str) and lower_power_mode in schema.power_modes:
        settings["lower_power_mode"] = lower_power_mode
    upper_io_mode = data.get("upper_io_mode")
    if isinstance(upper_io_mode, str) and upper_io_mode in schema.io_modes:
        settings["upper_io_mode"] = upper_io_mode
    lower_io_mode = data.get("lower_io_mode")
    if isinstance(lower_io_mode, str) and lower_io_mode in schema.io_modes:
        settings["lower_io_mode"] = lower_io_mode
    load_view = data.get("load_view")
    if isinstance(load_view, str) and load_view in schema.load_views:
        settings["load_view"] = load_view
    process_panel = data.get("process_panel")
    if isinstance(process_panel, str) and process_panel in schema.process_panel_modes:
        settings["process_panel"] = process_panel
    process_sort = data.get("process_sort")
    if isinstance(process_sort, str) and process_sort in schema.process_sorts:
        settings["process_sort"] = process_sort
    charge_panel = data.get("charge_panel")
    if isinstance(charge_panel, str) and charge_panel in schema.charge_panel_modes:
        settings["charge_panel"] = charge_panel
    allow_root_kill = data.get("allow_root_kill")
    if isinstance(allow_root_kill, bool):
        settings["allow_root_kill"] = allow_root_kill
    alert_temp_c = data.get("alert_temp_c")
    if "alert_temp_c" in data:
        number = finite_number(alert_temp_c)
        if number is None:
            invalid_fields.append("alert_temp_c")
        else:
            settings["alert_temp_c"] = clamp(number, 40.0, 125.0)
    alert_swap_gib = data.get("alert_swap_gib")
    if "alert_swap_gib" in data:
        number = finite_number(alert_swap_gib)
        if number is None:
            invalid_fields.append("alert_swap_gib")
        else:
            settings["alert_swap_gib"] = clamp(number, 0.0, 1024.0)
    alert_battery_drain_w = data.get("alert_battery_drain_w")
    if "alert_battery_drain_w" in data:
        number = finite_number(alert_battery_drain_w)
        if number is None:
            invalid_fields.append("alert_battery_drain_w")
        else:
            settings["alert_battery_drain_w"] = clamp(number, 0.0, 250.0)
    custom_slot = data.get("custom_slot")
    if isinstance(custom_slot, str) and custom_slot in schema.custom_slot_ids:
        settings["custom_slot"] = custom_slot
    custom_layout = schema.sanitize_custom_layout(data.get("custom_layout"))
    if custom_layout:
        settings["custom_layout"] = custom_layout
    custom_name = clean_custom_name(data.get("custom_name"))
    if custom_name and custom_name_error(custom_name, schema.reserved_custom_names) is None:
        settings["custom_name"] = custom_name
    if invalid_fields:
        settings["__warning__"] = f"ignored invalid settings: {', '.join(invalid_fields)}"
    return settings


def save_settings(path: Path, args: argparse.Namespace, schema: SettingsSchema) -> str | None:
    layout = schema.normalize_layout(args.layout) if isinstance(args.layout, str) else "full"
    custom_name = clean_custom_name(getattr(args, "custom_name", ""))
    if custom_name_error(custom_name, schema.reserved_custom_names) is not None:
        custom_name = ""
    data = {
        "theme": args.theme if args.theme in schema.themes else "classic",
        "interval": round(float(args.interval), 2),
        "layout": layout if layout in schema.layouts else "full",
        "show_io": bool(args.show_io),
        "upper_power_mode": setting_choice(args, "upper_power_mode", "soc", schema.power_modes),
        "lower_power_mode": setting_choice(args, "lower_power_mode", "cpu", schema.power_modes),
        "upper_io_mode": setting_choice(args, "upper_io_mode", "disk_read", schema.io_modes),
        "lower_io_mode": setting_choice(args, "lower_io_mode", "net_in", schema.io_modes),
        "load_view": setting_choice(args, "load_view", "rows", schema.load_views),
        "process_panel": setting_choice(args, "process_panel", "hidden", schema.process_panel_modes),
        "process_sort": setting_choice(args, "process_sort", "cpu", schema.process_sorts),
        "charge_panel": setting_choice(args, "charge_panel", "battery", schema.charge_panel_modes),
        "allow_root_kill": bool(getattr(args, "allow_root_kill", False))
        if not (os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0)
        else False,
        "alert_temp_c": round(float(getattr(args, "alert_temp_c", schema.high_temp_c)), 1),
        "alert_swap_gib": round(float(getattr(args, "alert_swap_gib", schema.default_alert_swap_gib)), 2),
        "alert_battery_drain_w": round(float(getattr(args, "alert_battery_drain_w", schema.default_alert_battery_drain_w)), 1),
        "custom_slot": getattr(args, "custom_slot", schema.custom_slot_ids[0])
        if getattr(args, "custom_slot", schema.custom_slot_ids[0]) in schema.custom_slot_ids
        else schema.custom_slot_ids[0],
        "custom_layout": schema.sanitize_custom_layout(getattr(args, "custom_layout", {})),
        "custom_name": custom_name,
    }
    tmp_path: Path | None = None
    try:
        created_parent = False
        try:
            parent_stat = path.parent.lstat()
            if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                raise OSError("settings directory is not a regular directory")
        except FileNotFoundError:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
            created_parent = True
        try:
            path_stat = path.lstat()
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                raise OSError("settings file must be a regular file")
        except FileNotFoundError:
            pass
        owner = settings_owner()
        if owner is not None and created_parent:
            try:
                os.chown(path.parent, *owner)
            except Exception:
                pass
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp_path = Path(tmp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if owner is not None:
            try:
                os.chown(tmp_path, *owner)
            except Exception:
                pass
        os.replace(tmp_path, path)
        if owner is not None:
            try:
                os.chown(path, *owner)
            except Exception:
                pass
        return None
    except Exception as exc:
        try:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return str(exc)


def remove_settings(path: Path) -> str | None:
    try:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return None
    except Exception as exc:
        return str(exc)
