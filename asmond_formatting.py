from __future__ import annotations


def fmt_power(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1000:
        return f"{sign}{value / 1000:.2f} W"
    return f"{sign}{value:.0f} mW"


def fmt_watts(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} W"


def fmt_voltage(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} V"


def fmt_current(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} A"


def fmt_bytes(value: int | None) -> str:
    if value is None:
        return "n/a"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(max(0, value))
    for unit in units:
        if number < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{number:.0f} {unit}"
            return f"{number:.2f} {unit}" if number < 10 else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TiB"


def fmt_bytes_zero(value: int | None) -> str:
    if value in (None, 0):
        return "0 B" if value == 0 else "n/a"
    return fmt_bytes(value)


def fmt_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{fmt_bytes(int(value))}/s"


def fmt_gb_s(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} GB/s"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}%"


def fmt_freq(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"{value / 1000:.2f} GHz"
    return f"{value:.0f} MHz"


def fmt_temp(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} C"


def fmt_minutes(value: int | None) -> str:
    if value is None or value < 0:
        return "n/a"
    hours, minutes = divmod(value, 60)
    if hours:
        return f"{hours}h{minutes:02d}"
    return f"{minutes}m"


def interval_step(value: float) -> float:
    if value < 1.0:
        return 0.1
    if value < 3.0:
        return 0.5
    return 1.0


def interval_text(value: float) -> str:
    return f"{value:.1f}s"
