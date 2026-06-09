from __future__ import annotations

import math
import re
from typing import Any, Iterable

from asmond_models import CoreMetric, MetricSample


ANE_MAX_POWER_MW = 8000.0
POWER_FALLBACK_EXCLUDES = (
    "limit",
    "cap",
    "state",
    "mode",
    "level",
    "index",
    "count",
    "ratio",
    "residency",
    "idle",
    "active",
)
USAGE_FALLBACK_EXCLUDES = (
    "frequency",
    "freq",
    "count",
    "per s",
    "samples",
    "ticks",
    "time",
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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


def bandwidth_label(name: str) -> str:
    key = normalize_key(name)
    if "cpu" in key:
        return "CPU"
    if "gpu" in key:
        return "GPU"
    if "ane" in key or "neural" in key:
        return "ANE"
    if any(word in key for word in ("media", "video", "decoder", "encoder")):
        return "Media"
    if "dram" in key:
        return "DRAM"
    if "dcs" in key:
        return "DCS"
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", name).strip()
    return (cleaned or "BW")[:10]


def bytes_per_s_to_gb_s(value: float | None) -> float | None:
    if value is None or value < 0:
        return None
    return value / 1_000_000_000.0


def bandwidth_counters_from_plist(obj: dict[str, Any]) -> dict[str, float]:
    counters = obj.get("bandwidth_counters")
    if not isinstance(counters, list):
        return {}
    values: dict[str, float] = {}
    for item in counters:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        raw_value = item.get("value")
        if not isinstance(raw_name, str):
            continue
        value = dict_number(item, "value") if raw_value is not None else None
        gb_s = bytes_per_s_to_gb_s(value)
        if gb_s is None:
            continue
        label = bandwidth_label(raw_name)
        values[label] = values.get(label, 0.0) + gb_s
    return values


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


def any_present(*values: Any) -> bool:
    return any(value is not None for value in values)


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
    sample.memory_bandwidth_gbps = bandwidth_counters_from_plist(obj)

    sample.cpu_power_mw = first_non_none(sample.cpu_power_mw, find_best(
        flat, ("cpu", "power"), exclude=(*POWER_FALLBACK_EXCLUDES, "battery"), converter=as_mw
    ))
    sample.gpu_power_mw = first_non_none(sample.gpu_power_mw, find_best(
        flat, ("gpu", "power"), exclude=(*POWER_FALLBACK_EXCLUDES, "battery"), converter=as_mw
    ))
    sample.ane_power_mw = first_non_none(sample.ane_power_mw, find_best(
        flat,
        ("power",),
        any_of=("ane", "neural"),
        exclude=(*POWER_FALLBACK_EXCLUDES, "battery"),
        converter=as_mw,
    ))
    sample.media_power_mw = first_non_none(sample.media_power_mw, find_best(
        flat,
        ("power",),
        any_of=("media", "decoder", "encoder", "video"),
        exclude=(*POWER_FALLBACK_EXCLUDES, "battery"),
        converter=as_mw,
    ))
    sample.soc_power_mw = first_non_none(sample.soc_power_mw, find_best(
        flat,
        ("power",),
        any_of=("soc", "processor", "package", "combined"),
        exclude=(*POWER_FALLBACK_EXCLUDES, "battery"),
        converter=as_mw,
    ))
    sample.battery_power_mw = first_non_none(sample.battery_power_mw, find_best(
        flat, ("battery", "power"), exclude=(*POWER_FALLBACK_EXCLUDES, "accumulated"), converter=as_mw
    ))

    sample.p_usage_pct = first_non_none(sample.p_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("p cluster", "performance"),
        exclude=USAGE_FALLBACK_EXCLUDES,
        converter=as_pct,
    ))
    sample.e_usage_pct = first_non_none(sample.e_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("e cluster", "efficiency"),
        exclude=USAGE_FALLBACK_EXCLUDES,
        converter=as_pct,
    ))
    sample.cpu_usage_pct = first_non_none(sample.cpu_usage_pct, find_best(
        flat,
        ("cpu", "active"),
        any_of=("residency", "duty", "usage", "utilization"),
        exclude=USAGE_FALLBACK_EXCLUDES,
        converter=as_pct,
    ))
    sample.gpu_usage_pct = first_non_none(sample.gpu_usage_pct, find_best(
        flat,
        ("gpu", "active"),
        any_of=("residency", "duty", "usage", "utilization"),
        exclude=USAGE_FALLBACK_EXCLUDES,
        converter=as_pct,
    ))
    sample.ane_usage_pct = first_non_none(sample.ane_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("ane", "neural"),
        exclude=USAGE_FALLBACK_EXCLUDES,
        converter=as_pct,
    ))
    sample.media_usage_pct = first_non_none(sample.media_usage_pct, find_best(
        flat,
        ("active",),
        any_of=("media", "decoder", "encoder", "video"),
        exclude=USAGE_FALLBACK_EXCLUDES,
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
