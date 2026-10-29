"""Pure dashboard geometry and Tailor configuration; no terminal or sensor I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LAYOUTS = ("full", "compact", "focus", "custom")
LEGACY_LAYOUTS = {"power-only": "focus", "thermals-only": "focus"}
DETAIL_LEVELS = ("compact", "normal", "detail")
CUSTOM_SLOT_IDS = (
    "upper_left",
    "upper_right",
    "lower_left",
    "right_top",
    "right_middle",
    "right_lower",
    "right_bottom",
)
CUSTOM_SLOT_LABELS = {
    "upper_left": "upper left",
    "upper_right": "upper right",
    "lower_left": "lower left",
    "right_top": "right top",
    "right_middle": "right middle",
    "right_lower": "right lower",
    "right_bottom": "right bottom",
}
TAILOR_MENU_ITEMS = (
    "profile", "grid", "graph", "slot", "panel", "detail", "name",
    "duplicate", "delete", "swap", "copy", "reset",
)
MAX_CUSTOM_PROFILES = 16
RESERVED_CUSTOM_NAMES = frozenset((*LAYOUTS, *LEGACY_LAYOUTS))
MIN_RAM_PANEL_H = 9
BATTERY_PANEL_H = 6
CHARGE_PANEL_H = 8
IO_PANEL_H = 7


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
    custom_selectable: bool = True


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


@dataclass(frozen=True)
class CustomGrid:
    title: str
    columns: int
    rows: int
    slots: tuple[LayoutSlot, ...]


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


PANEL_SPECS: dict[str, PanelSpec] = {
    "power_graph": PanelSpec(
        "power_graph", "Power Graph", 72, 7, "wide", "detail", ("detail",), False
    ),
    "power": PanelSpec(
        "power", "Power", 30, 5, "normal", "normal", ("compact", "normal")
    ),
    "thermals": PanelSpec(
        "thermals", "Thermals", 30, 5, "normal", "normal", ("normal",)
    ),
    "load": PanelSpec("load", "CPU / GPU Load", 48, 8, "wide", "detail", ("detail",)),
    "clocks": PanelSpec("clocks", "Clocks", 30, 5, "normal", "normal", ("normal",)),
    "ram": PanelSpec("ram", "RAM", 38, 9, "tall", "detail", ("compact", "detail")),
    # Six outer rows leave four content rows, which is the complete two-column
    # battery view. Only shorter charge panels should auto-select compact detail.
    "charge": PanelSpec(
        "charge", "Charge (switchable)", 30, 5, "normal", "normal", DETAIL_LEVELS
    ),
    "battery": PanelSpec(
        "battery", "Battery", 30, 5, "normal", "normal", ("compact", "normal")
    ),
    "power_input": PanelSpec(
        "power_input", "Power Input", 30, 5, "normal", "normal", DETAIL_LEVELS
    ),
    "cable": PanelSpec(
        "cable", "Cable", 30, 5, "normal", "normal", ("compact", "normal")
    ),
    "port_diagnostics": PanelSpec(
        "port_diagnostics", "Port diagnostics", 30, 5, "normal", "normal", ("normal",)
    ),
    "io": PanelSpec("io", "Disk / Net", 38, 5, "wide", "normal", ("normal",)),
    "process": PanelSpec("process", "Processes", 48, 8, "tall", "detail", ("detail",)),
}


CUSTOM_PANEL_IDS = tuple(
    panel_id for panel_id, spec in PANEL_SPECS.items() if spec.custom_selectable
)


FIXED_CHARGE_PANEL_MODES = {
    "battery": "battery",
    "power_input": "usb",
    "cable": "cable",
    "port_diagnostics": "diagnostics",
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


CUSTOM_GRID_SPECS = {
    "classic": CustomGrid("Classic", 0, 0, LAYOUT_TEMPLATES["custom"].slots[1:]),
    "balanced": CustomGrid(
        "Balanced",
        2,
        2,
        (
            LayoutSlot("cell_1", "power"),
            LayoutSlot("cell_2", "thermals"),
            LayoutSlot("cell_3", "load"),
            LayoutSlot("cell_4", "ram"),
        ),
    ),
    "columns": CustomGrid(
        "Columns",
        3,
        2,
        (
            LayoutSlot("cell_1", "power"),
            LayoutSlot("cell_2", "thermals"),
            LayoutSlot("cell_3", "ram"),
            LayoutSlot("cell_4", "load"),
            LayoutSlot("cell_5", "clocks"),
            LayoutSlot("cell_6", "charge"),
        ),
    ),
}
CUSTOM_GRIDS = tuple(CUSTOM_GRID_SPECS)


def normalize_custom_grid(value: Any) -> str:
    return value if isinstance(value, str) and value in CUSTOM_GRIDS else "classic"


def custom_grid_slot_ids(grid: str = "classic") -> tuple[str, ...]:
    return tuple(
        slot.slot_id for slot in CUSTOM_GRID_SPECS[normalize_custom_grid(grid)].slots
    )


def custom_grid_dimensions(grid: str, width: int) -> tuple[int, int]:
    grid = normalize_custom_grid(grid)
    spec = CUSTOM_GRID_SPECS[grid]
    if grid == "columns" and width < 144:
        return 2, 3
    return spec.columns, spec.rows


def custom_slot_label(slot_id: str, grid: str = "classic") -> str:
    if normalize_custom_grid(grid) == "classic":
        return CUSTOM_SLOT_LABELS.get(slot_id, slot_id)
    number = tailor_slot_number(slot_id, grid)
    return f"area {number}" if number is not None else slot_id


def default_panel_for_slot(slot_id: str, grid: str = "classic") -> str:
    if slot_id == "top":
        return "power_graph"
    for slot in CUSTOM_GRID_SPECS[normalize_custom_grid(grid)].slots:
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


def panel_size_hint(panel_id: str, detail: str, width: int) -> tuple[int, int]:
    """Minimum slot width and height for the selected content, including borders."""
    spec = PANEL_SPECS[panel_id]
    height = spec.min_h
    if panel_id == "battery":
        rows = (
            (3 if detail == "compact" else 4)
            if width >= 48
            else (6 if detail == "compact" else 8)
        )
        height = rows + 2
    elif panel_id == "power_input":
        height = {"compact": 6, "normal": 9, "detail": 10}.get(detail, 9)
    elif panel_id == "cable":
        rows = (
            (4 if detail == "compact" else 6)
            if width >= 62
            else (7 if detail == "compact" else 11)
        )
        height = rows + 2
    return spec.min_w, height


def sanitize_custom_layout(
    value: Any, grid: str = "classic"
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for slot_id, raw_config in value.items():
        if slot_id not in custom_grid_slot_ids(grid) or not isinstance(
            raw_config, dict
        ):
            continue
        panel_id = raw_config.get("panel")
        detail = raw_config.get("detail")
        if panel_id not in CUSTOM_PANEL_IDS:
            continue
        if detail not in detail_levels_for_panel(panel_id):
            detail = default_detail_for_panel(panel_id)
        cleaned[slot_id] = {"panel": panel_id, "detail": detail}
    return cleaned


def sanitize_custom_layouts(
    value: Any, legacy_layout: Any = None
) -> dict[str, dict[str, dict[str, str]]]:
    cleaned = {}
    if isinstance(value, dict):
        for grid in CUSTOM_GRIDS:
            if grid in value and isinstance(value[grid], dict):
                cleaned[grid] = sanitize_custom_layout(value[grid], grid)
    if "classic" not in cleaned:
        legacy = sanitize_custom_layout(legacy_layout)
        if legacy:
            cleaned["classic"] = legacy
    return cleaned


def switch_custom_grid(
    current_grid: str,
    current_layout: Any,
    layouts: Any,
    next_grid: str,
) -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, dict[str, str]]]:
    cleaned = sanitize_custom_layouts(layouts)
    current_grid = normalize_custom_grid(current_grid)
    next_grid = normalize_custom_grid(next_grid)
    cleaned[current_grid] = sanitize_custom_layout(current_layout, current_grid)
    return cleaned, sanitize_custom_layout(cleaned.get(next_grid, {}), next_grid)


def profile_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())[:32]


def sanitize_custom_profiles(value: Any) -> dict[str, dict[str, Any]]:
    """Bounded, independent snapshots; never retain references to settings data."""
    cleaned = {}
    if not isinstance(value, dict):
        return cleaned
    for key, record in value.items():
        if len(cleaned) >= MAX_CUSTOM_PROFILES:
            break
        if (
            not isinstance(key, str) or not 1 <= len(key) <= 32
            or not all(ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in key)
            or not isinstance(record, dict)
        ):
            continue
        name = profile_name(record.get("name"))
        if not name or name.casefold() in RESERVED_CUSTOM_NAMES:
            continue
        cleaned[key] = {
            "name": name,
            "grid": normalize_custom_grid(record.get("grid")),
            "show_graph": record.get("show_graph") if isinstance(record.get("show_graph"), bool) else True,
            "layouts": sanitize_custom_layouts(record.get("layouts")),
        }
    return cleaned


def sync_custom_profile(args: Any) -> None:
    """Snapshot the editing view while preserving every inactive profile/grid."""
    args.custom_grid = normalize_custom_grid(getattr(args, "custom_grid", "classic"))
    args.custom_layouts = sanitize_custom_layouts(getattr(args, "custom_layouts", {}))
    args.custom_layout = sanitize_custom_layout(getattr(args, "custom_layout", {}), args.custom_grid)
    args.custom_layouts[args.custom_grid] = args.custom_layout
    show_graph = getattr(args, "custom_show_graph", True)
    args.custom_show_graph = show_graph if isinstance(show_graph, bool) else True
    profiles = sanitize_custom_profiles(getattr(args, "custom_profiles", {}))
    active = getattr(args, "custom_profile", "default")
    if not isinstance(active, str) or active not in profiles:
        active = next(iter(profiles), "default")
    name = profile_name(getattr(args, "custom_name", ""))
    if not name or name.casefold() in RESERVED_CUSTOM_NAMES:
        name = profiles.get(active, {}).get("name", "Default")
    profiles[active] = {
        "name": name, "grid": args.custom_grid, "show_graph": args.custom_show_graph,
        "layouts": sanitize_custom_layouts(args.custom_layouts),
    }
    args.custom_profiles = profiles
    args.custom_profile = active
    args.custom_name = name


def activate_custom_profile(args: Any, profile_id: str) -> None:
    sync_custom_profile(args)
    if profile_id not in args.custom_profiles:
        return
    record = args.custom_profiles[profile_id]
    args.custom_profile = profile_id
    args.custom_name = record["name"]
    args.custom_grid = record["grid"]
    args.custom_show_graph = record["show_graph"]
    args.custom_layouts = sanitize_custom_layouts(record["layouts"])
    args.custom_layout = sanitize_custom_layout(args.custom_layouts.get(args.custom_grid, {}), args.custom_grid)
    args.custom_slot = custom_grid_slot_ids(args.custom_grid)[0]


def duplicate_custom_profile(args: Any) -> bool:
    sync_custom_profile(args)
    if len(args.custom_profiles) >= MAX_CUSTOM_PROFILES:
        return False
    index = 1
    while f"profile_{index:02d}" in args.custom_profiles:
        index += 1
    key = f"profile_{index:02d}"
    names = {record["name"].casefold() for record in args.custom_profiles.values()}
    base = args.custom_name[:23]
    suffix = 1
    name = f"{base} copy"
    while name.casefold() in names:
        suffix += 1
        name = f"{base} copy {suffix}"
    args.custom_profiles[key] = {
        "name": name, "grid": args.custom_grid,
        "show_graph": args.custom_show_graph,
        "layouts": sanitize_custom_layouts(args.custom_layouts),
    }
    activate_custom_profile(args, key)
    return True


def delete_custom_profile(args: Any) -> bool:
    sync_custom_profile(args)
    keys = tuple(args.custom_profiles)
    if len(keys) <= 1:
        return False
    old = args.custom_profile
    next_id = keys[(keys.index(old) + 1) % len(keys)]
    activate_custom_profile(args, next_id)
    del args.custom_profiles[old]
    return True


def transfer_custom_slot(layout: Any, source: str, target: str, grid: str, *, swap: bool = False) -> dict[str, dict[str, str]]:
    cleaned = sanitize_custom_layout(layout, grid)
    slots = custom_grid_slot_ids(grid)
    if source not in slots or target not in slots or source == target:
        return cleaned
    source_config = custom_slot_config(cleaned, source, grid)
    target_config = custom_slot_config(cleaned, target, grid)
    cleaned[target] = source_config
    if swap:
        cleaned[source] = target_config
    return cleaned


def custom_slot_config(
    custom_layout: dict[str, dict[str, str]], slot_id: str, grid: str = "classic"
) -> dict[str, str]:
    config = sanitize_custom_layout(custom_layout, grid).get(slot_id, {})
    panel_id = config.get("panel", default_panel_for_slot(slot_id, grid))
    detail = config.get("detail", default_detail_for_panel(panel_id))
    return {"panel": panel_id, "detail": detail}


def update_custom_slot(
    custom_layout: dict[str, dict[str, str]],
    slot_id: str,
    panel_id: str | None = None,
    detail: str | None = None,
    grid: str = "classic",
) -> dict[str, dict[str, str]]:
    cleaned = sanitize_custom_layout(custom_layout, grid)
    if slot_id not in custom_grid_slot_ids(grid):
        return cleaned
    config = custom_slot_config(cleaned, slot_id, grid)
    if panel_id in CUSTOM_PANEL_IDS:
        config = {"panel": panel_id, "detail": default_detail_for_panel(panel_id)}
    if detail in detail_levels_for_panel(config["panel"]):
        config["detail"] = detail
    cleaned[slot_id] = config
    return cleaned


def custom_layout_effective_has_panel(
    custom_layout: dict[str, dict[str, str]], panel_id: str, grid: str = "classic"
) -> bool:
    cleaned = sanitize_custom_layout(custom_layout, grid)
    for slot_id in custom_grid_slot_ids(grid):
        if slot_id == "right_bottom" and slot_id not in cleaned:
            continue
        if custom_slot_config(cleaned, slot_id, grid)["panel"] == panel_id:
            return True
    return False


def normalize_layout(value: str) -> str:
    return LEGACY_LAYOUTS.get(value, value)


def tailor_slot_number(slot_id: str, grid: str = "classic") -> int | None:
    try:
        return custom_grid_slot_ids(grid).index(slot_id) + 1
    except ValueError:
        return None


def tailor_slot_for_key(key: int, grid: str = "classic") -> str | None:
    slot_ids = custom_grid_slot_ids(grid)
    if ord("1") <= key <= ord(str(min(9, len(slot_ids)))):
        index = key - ord("1")
        if 0 <= index < len(slot_ids):
            return slot_ids[index]
    return None


def tailor_menu_rect(max_y: int, max_x: int, anchor: Rect | None) -> Rect:
    h = min(max_y - 4, len(TAILOR_MENU_ITEMS) + 7)
    w = min(max_x - 4, 52)
    if h < 6 or w < 30:
        return Rect(1, 1, max(3, h), max(20, w))
    if anchor is None:
        return Rect(2, max(1, max_x - w - 2), h, w)
    if anchor.x + anchor.w // 2 < max_x // 2:
        x = max_x - w - 2
    else:
        x = 2
    return Rect(2, x, h, w)


def power_graph_height(layout: str, max_y: int) -> int:
    if layout == "compact":
        return 7
    if layout == "focus":
        return max(7, min(12, max_y // 4))
    return max(7, min(11, max_y // 5))


def panel_detail(layout_name: str, panel_id: str, rect: Rect) -> str:
    spec = PANEL_SPECS[panel_id]
    if layout_name == "compact" and "compact" in spec.detail_levels:
        return "compact"
    if (
        rect.h <= spec.min_h or rect.w < spec.min_w
    ) and "compact" in spec.detail_levels:
        return "compact"
    return (
        spec.default_detail if spec.default_detail in spec.detail_levels else "normal"
    )


def panel_placement(
    layout_name: str,
    panel_id: str,
    rect: Rect,
    slot: str = "",
    detail: str | None = None,
) -> PanelPlacement:
    selected_detail = (
        detail
        if detail in detail_levels_for_panel(panel_id)
        else panel_detail(layout_name, panel_id, rect)
    )
    return PanelPlacement(panel_id, rect, selected_detail, slot)


def layout_panel(
    layout: DashboardLayout, panel_id: str, slot: str = ""
) -> PanelPlacement | None:
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
    custom_grid: str = "classic",
) -> tuple[PanelPlacement, ...]:
    placements: list[PanelPlacement] = []
    custom_layout = sanitize_custom_layout(custom_layout or {}, custom_grid)
    for slot in template.slots:
        rect = rects.get(slot.slot_id)
        if rect is None:
            continue
        detail = None
        if layout_name == "custom" and slot.slot_id in custom_grid_slot_ids(
            custom_grid
        ):
            config = custom_slot_config(custom_layout, slot.slot_id, custom_grid)
            panel_id = config["panel"]
            detail = config["detail"]
        else:
            panel_id = panel_id_for_slot(slot, process_left, process_right)
            if panel_id == "process" and slot.slot_id == "right_middle" and rect.h < 8:
                panel_id = slot.default_panel_id
        placements.append(
            panel_placement(layout_name, panel_id, rect, slot.slot_id, detail)
        )
    return tuple(placements)


def custom_grid_layout(
    max_y: int,
    max_x: int,
    custom_layout: Any,
    grid: str,
    show_power_graph: bool = True,
) -> DashboardLayout:
    spec = CUSTOM_GRID_SPECS[grid]
    columns, rows = custom_grid_dimensions(grid, max_x)
    graph_h = power_graph_height("custom", max_y) if show_power_graph else 0
    content_y = 2 + graph_h
    content_h = max(0, max_y - content_y - 1)
    rects = {"top": Rect(2, 0, graph_h, max_x)} if show_power_graph else {}
    for index, slot in enumerate(spec.slots):
        row, column = divmod(index, columns)
        x = max_x * column // columns
        y = content_y + content_h * row // rows
        width = max_x * (column + 1) // columns - x
        height = content_y + content_h * (row + 1) // rows - y
        if height >= 3 and width >= 8:
            rects[slot.slot_id] = Rect(y, x, height, width)
    template = LayoutTemplate(
        "custom", spec.title, (LayoutSlot("top", "power_graph"), *spec.slots)
    )
    return DashboardLayout(
        panels=panel_placements_from_template(
            "custom",
            template,
            rects,
            custom_layout=custom_layout,
            custom_grid=grid,
        )
    )


def diagnostic_dashboard_layout(
    max_y: int, max_x: int, layout_name: str, show_graph: bool = True, *,
    show_io: bool = False, process_panel: str = "hidden",
    custom_layout: dict[str, dict[str, str]] | None = None, custom_grid: str = "classic",
) -> DashboardLayout:
    """Use the normal charge box, without changing saved Tailor assignments."""
    base = dashboard_layout(max_y, max_x, layout_name, show_io, process_panel,
                            custom_layout, "usb", custom_grid, show_graph)
    if layout_name == "focus":
        main = base.slot("main")
        if main is None:
            return base
        r = main.rect
        columns = 3 if max_x >= 150 else 2 if max_x >= 100 else 1
        width = max_x // columns
        panels = [p for p in base.panels if p.slot != "main"]
        for index in range(columns):
            panel_id = "port_diagnostics" if index == columns - 1 else "battery" if index == 0 else "power_input"
            slot = "diagnostics" if panel_id == "port_diagnostics" else f"focus_{panel_id}"
            x = index * width
            panels.append(PanelPlacement(panel_id, Rect(r.y, x, min(CHARGE_PANEL_H, r.h), max_x - x if index == columns - 1 else width), "normal", slot))
        return DashboardLayout(panels=tuple(panels))
    candidates = [p for p in base.panels if p.panel_id in ("charge", "port_diagnostics") and p.rect.h >= 5 and p.rect.w >= 30]
    if not candidates:
        candidates = [p for p in reversed(base.panels) if p.panel_id != "power_graph" and p.rect.h >= 5 and p.rect.w >= 30]
    if not candidates:
        return base
    target = candidates[0]
    r = target.rect
    replacement = PanelPlacement("port_diagnostics", Rect(r.y, r.x, min(CHARGE_PANEL_H, r.h), r.w), "normal", "diagnostics")
    return DashboardLayout(
        left_io_panel=base.left_io_panel,
        panels=tuple(replacement if p is target else p for p in base.panels),
    )


def dashboard_layout(
    max_y: int,
    max_x: int,
    layout_name: str,
    show_io: bool,
    process_panel: str,
    custom_layout: dict[str, dict[str, str]] | None = None,
    charge_panel: str = "usb",
    custom_grid: str = "classic",
    custom_show_graph: bool = True,
) -> DashboardLayout:
    layout_name = normalize_layout(layout_name)
    custom_grid = normalize_custom_grid(custom_grid)
    if layout_name == "custom" and custom_grid != "classic":
        return custom_grid_layout(max_y, max_x, custom_layout, custom_grid, custom_show_graph)
    template = LAYOUT_TEMPLATES.get(layout_name, LAYOUT_TEMPLATES["full"])
    custom_layout = sanitize_custom_layout(custom_layout or {})
    show_graph = layout_name != "custom" or custom_show_graph
    graph_h = power_graph_height(layout_name, max_y) if show_graph else 0
    graph = Rect(2, 0, graph_h, max_x)
    info_y = graph_h + 2
    top_h = 5 if layout_name == "compact" else 7
    left_w = max_x // 2
    right_w = max_x - left_w
    power = Rect(info_y, 0, top_h, left_w)
    thermals = Rect(info_y, left_w, top_h, right_w)
    rects = {"upper_left": power, "upper_right": thermals}
    if show_graph:
        rects["top"] = graph

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
    custom_wants_right_bottom = layout_name == "custom" and (
        show_io or "right_bottom" in custom_layout
    )
    requested_charge_h = (
        BATTERY_PANEL_H
        if layout_name == "full" and charge_panel == "battery"
        else CHARGE_PANEL_H
    )
    if (
        layout_name == "custom"
        and custom_slot_config(custom_layout, "right_lower")["panel"] == "battery"
    ):
        requested_charge_h = BATTERY_PANEL_H

    if layout_name == "compact":
        clocks_h = min(6, remaining_h)
        charge_h = 6 if remaining_h - clocks_h >= 11 else 0
        io_h = 0
    elif remaining_h >= 16:
        clocks_h = 6
        wants_charge = (
            custom_wants_right_lower
            or remaining_h - clocks_h >= requested_charge_h + MIN_RAM_PANEL_H
        )
        charge_h = (
            requested_charge_h
            if wants_charge
            and remaining_h - clocks_h - requested_charge_h >= MIN_RAM_PANEL_H
            else 0
        )
        need_right_bottom = custom_wants_right_bottom or (show_io and not left_io_panel)
        io_h = (
            IO_PANEL_H
            if need_right_bottom
            and remaining_h - clocks_h - charge_h - IO_PANEL_H >= MIN_RAM_PANEL_H
            else 0
        )
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
        panels=panel_placements_from_template(
            layout_name, template, rects, process_left, process_right, custom_layout
        ),
    )
