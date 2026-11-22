"""Read-only port diagnostics; never infer physical identity from array order."""

from __future__ import annotations

import time
import re
import unicodedata
from collections import deque
from dataclasses import replace
from typing import Any

from asmond_models import PortDiagnostic, UsbCPortStats, UsbDeviceInfo


COUNTER_KEYS = {
    "Attach": "PortControllerAttachCount",
    "Detach": "PortControllerDetachCount",
    "Hard reset": "PortControllerHardResetCount",
    "Short detect": "PortControllerShortDetectCount",
    "I2C error": "PortControllerI2cErrCount",
    "Data swap": "PortControllerDataRoleSwapCount",
    "Data swap fail": "PortControllerDataRoleSwapFailCount",
    "Power swap": "PortControllerPwrRoleSwapCount",
    "Power swap fail": "PortControllerPwrRoleSwapFailCount",
    "VDO fail": "PortControllerVdoFailCount",
    "Input FET fail": "PortControllerInpFetEnFailCount",
    "HV recovery": "PortControllerHvEnRecoveryCount",
    "Sleep cmd fail": "PortControllerSleepCmdFailCount",
    "Stuck cmd": "PortControllerStuckCmdCount",
    "Surprise ACK": "PortControllerSurpriseAckCount",
    "Surprise NACK": "PortControllerSurpriseNackCount",
    "Wake cmd fail": "PortControllerWakeCmdFailCount",
    "Wake fail": "PortControllerWakeFailCount",
    "Wake timeout": "PortControllerWakeTimeoutCount",
    "Overcurrent": "Overcurrent Count",
    "Plug event": "Plug Event Count",
}


def reported_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def reported_counter(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**64 - 1 else None


# IOUSBHost's USBSpeed and the legacy Device Speed use different enums.
HOST_SPEEDS = {1: 12_000_000, 2: 1_500_000, 3: 480_000_000,
               4: 5_000_000_000, 5: 10_000_000_000, 6: 20_000_000_000}
LEGACY_SPEEDS = {0: 1_500_000, 1: 12_000_000, 2: 480_000_000,
                 3: 5_000_000_000, 4: 10_000_000_000, 5: 20_000_000_000}
XHCI_COUNTER_KEYS = {
    "Connect": "kPortStatConnectCount",
    "Enumeration fail": "kPortStatEnumerationFailureCount",
    "Overcurrent": "kPortStatOverCurrentCount",
    "Remote wake": "kPortStatRemoteWakeCount",
    "EOF2 violation": "kPortStatEOF2ViolationCount",
}


def device_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(char if unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"} else " " for char in value).split())[:160]
    return "" if text.casefold() in {"n/a", "unknown", "none", "unavailable"} else text


def usb_device_info(device: dict[str, Any]) -> UsbDeviceInfo | None:
    """USB identity and one physical disk below this exact device, no hub sums.

    Whole=True also occurs on RAID/APFS virtual media. Only direct IOMedia
    children of IOBlockStorageDriver qualify, and traversal stops there.
    Multiple LUNs/drivers remain ambiguous instead of picking the first.
    """
    info = UsbDeviceInfo(
        manufacturer=device_text(device.get("USB Vendor Name")) or device_text(device.get("kUSBVendorString")),
        product=device_text(device.get("USB Product Name")) or device_text(device.get("kUSBProductString")),
    )
    stack = [(device, {}, 0)]
    disks = []
    visited = 0
    while stack:
        node, characteristics, depth = stack.pop()
        visited += 1
        if visited > 4096 or depth > 32:
            disks = []
            break
        if not isinstance(node, dict):
            continue
        if depth and node.get("IOObjectClass") == "IOUSBHostDevice":
            continue  # This is another device behind a hub, not the hub itself.
        props = node.get("Device Characteristics")
        if isinstance(props, dict):
            characteristics = props
        children = node.get("IORegistryEntryChildren")
        children = children if isinstance(children, list) else []
        if node.get("IOObjectClass") == "IOBlockStorageDriver":
            media = [child for child in children if isinstance(child, dict)
                     and child.get("IOObjectClass") == "IOMedia" and child.get("Whole") is True]
            size = media[0].get("Size") if len(media) == 1 else None
            capacity = size if type(size) is int and 0 < size < 2**64 else None
            disks.append((characteristics if len(media) <= 1 else {}, capacity))
            continue
        stack.extend((child, characteristics, depth + 1) for child in children)
    if len(disks) == 1:
        props, info.capacity_bytes = disks[0]
        info.storage_manufacturer = device_text(props.get("Vendor Name"))
        info.storage_model = device_text(props.get("Product Name"))
        info.medium_type = device_text(props.get("Medium Type"))
    return info if any((info.manufacturer, info.product, info.storage_manufacturer,
                        info.storage_model, info.capacity_bytes)) else None


def unique_port_locations(entries: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ports_by_location: dict[int, set[int]] = {}
    for number, location in entries:
        ports_by_location.setdefault(location, set()).add(number)
    return [(number, location) for number, location in entries if len(ports_by_location[location]) == 1]


def apple_usb_c_locations(ports: list[dict[str, Any]]) -> list[tuple[int, int]]:
    entries = []
    for port in ports:
        if not isinstance(port, dict):
            continue
        path, location = port.get("UsbIOPort"), port.get("locationID")
        match = re.search(r"/Port-USB-C@([0-9]{1,2})(?:/|$)", path) if isinstance(path, str) else None
        if match is None or not 1 <= (number := int(match[1])) <= 16 or type(location) is not int or location <= 0:
            continue
        entries.append((number, location))
    return unique_port_locations(entries)


def usb_direct_devices_by_port(
    ports: list[dict[str, Any]], devices: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    """Join Apple's explicit physical USB-C path to one direct device location."""
    by_location: dict[int, list[dict[str, Any]]] = {}
    for device in devices:
        if isinstance(device, dict) and type(location := device.get("locationID")) is int and location > 0:
            by_location.setdefault(location, []).append(device)
    by_port: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for number, location in apple_usb_c_locations(ports):
        by_port.setdefault((2, number), []).extend(by_location.get(location, ()))
    return {key: values[0] for key, values in by_port.items() if len(values) == 1}


def usb_devices_by_port(
    ports: list[dict[str, Any]], devices: list[dict[str, Any]],
    details: dict[int, UsbDeviceInfo] | None = None,
) -> dict[tuple[int, int], UsbDeviceInfo]:
    return {port: info for port, device in usb_direct_devices_by_port(ports, devices).items()
            if (info := device_info_with_details(device, details)) is not None}


def device_info_with_details(device: dict[str, Any], details: dict[int, UsbDeviceInfo] | None) -> UsbDeviceInfo | None:
    identity = device.get("IORegistryEntryID")
    cached = details.get(identity) if details is not None and type(identity) is int else None
    return cached if cached is not None else usb_device_info(device)


def xhci_usb_c_connections(
    ports: list[dict[str, Any]], devices: list[dict[str, Any]],
    details: dict[int, UsbDeviceInfo] | None = None,
) -> tuple[list[UsbCPortStats], list[PortDiagnostic]]:
    """Published Type-C identities and direct USB3 links, not PD or E-markers.

    A missing USB3 child cannot prove disconnection: a USB2 companion may be
    active. Keep that state unknown, without inventing companion joins.
    """
    roots = [item for item in ports if isinstance(item, dict)
             and item.get("IOObjectClass") == "AppleUSB30XHCITypeCPort"
             and type(item.get("UsbCPortNumber")) is int
             and 1 <= item["UsbCPortNumber"] <= 16]
    numbers = [item["UsbCPortNumber"] for item in roots]
    unique_locations = set(unique_port_locations([
        (item["UsbCPortNumber"], location) for item in roots
        if type(location := item.get("locationID")) is int and 0 < location <= 0xFFFFFFFF
    ]))
    output, diagnostics = [], []
    for item in roots:
        number = item["UsbCPortNumber"]
        location = item.get("locationID")
        if numbers.count(number) != 1 or type(location) is not int or not 0 < location <= 0xFFFFFFFF:
            continue
        label = f"USB-C {number}"
        direct = [device for device in devices if isinstance(device, dict)
                  and type(device.get("locationID")) is int and device["locationID"] == location]
        # More than one distinct device at the same location is ambiguous.
        connected = (number, location) in unique_locations and len(direct) == 1
        if connected:
            device = direct[0]
            speeds = []
            for key, enum in (("USBSpeed", HOST_SPEEDS), ("Device Speed", LEGACY_SPEEDS)):
                if key in device:
                    code = device[key]
                    speeds.append(enum.get(code) if type(code) is int else None)
            speed = speeds[0] if speeds and None not in speeds and len(set(speeds)) == 1 else None
            output.append(UsbCPortStats(label, connected=True, active_transports=["USB3"],
                                        data_link_speed_bps=speed, data_link_query_supported=True,
                                        device_info=device_info_with_details(device, details), charge_query_supported=False))
        stats = item.get("port-statistics")
        counters = {name: count for name, key in XHCI_COUNTER_KEYS.items()
                    if isinstance(stats, dict) and (count := reported_counter(stats.get(key))) is not None}
        errors = reported_counter(item.get("link-error-count"))
        if errors is not None:
            counters["Link error"] = errors
        status = reported_counter(item.get("port-status"))
        # IOUSBHostFamilyDefinitions.h tIOUSBHostPortStatus, not xHCI PORTSC.
        diagnostics.append(PortDiagnostic(
            f"xhci:{number}:{location:08x}", label, connected=True if connected else None,
            source="XHCI USB3 path; published UsbCPortNumber and exact device location; no PD/liquid telemetry",
            counters=counters, overcurrent_active=bool(status & (1 << 14)) if status is not None and status <= 0xFFFF else None,
        ))
    return sorted(output, key=lambda port: port.label), sorted(diagnostics, key=lambda port: port.label)


def diagnostic_snapshot(key: str, label: str, item: dict[str, Any], *, mapped: bool = True) -> PortDiagnostic:
    counters = {
        name: count for name, source in COUNTER_KEYS.items()
        if (count := reported_counter(item.get(source))) is not None
    }
    trace = item.get("PortControllerEvtBuffer")
    state = item.get("LDCM_StateDescription")
    measurement = item.get("LDCM_MeasurementStatusDescription")
    return PortDiagnostic(
        key, label, mapped=mapped,
        connected=reported_bool(item.get("ConnectionActive")),
        liquid_detected=reported_bool(item.get("LDCM_LiquidDetected")),
        liquid_state=state[:160] if isinstance(state, str) else "",
        measurement_status=measurement[:160] if isinstance(measurement, str) else "",
        counters=counters,
        raw_trace=trace[:256] if isinstance(trace, bytes) else b"",
    )


def port_diagnostics(
    physical: list[tuple[str, dict[str, Any]]], controllers: Any,
) -> list[PortDiagnostic]:
    # Only unique, verified HPM identities are passed by the caller. Controller
    # dictionaries without explicit matching identity remain separate snapshots.
    result = [diagnostic_snapshot(key, key, item) for key, item in physical]
    by_key = {port.key: port for port in result}
    entries = controllers[:32] if isinstance(controllers, list) else []
    identities = []
    for item in entries:
        number = item.get("PortNumber") if isinstance(item, dict) else None
        kind = item.get("PortType") if isinstance(item, dict) else None
        identities.append(
            f"USB-C {number}" if type(kind) is int and kind == 2
            and type(number) is int and 1 <= number <= 16 else None
        )
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        identity = identities[index]
        mapped = identity in by_key and identities.count(identity) == 1
        snapshot = diagnostic_snapshot(
            identity if mapped else f"controller:{index}",
            identity if mapped else f"Controller {index + 1} (port unknown)",
            item, mapped=mapped,
        )
        if mapped:
            target = by_key[identity]
            target.counters.update(snapshot.counters)
            target.counter_sources.update(dict.fromkeys(snapshot.counters, "controller"))
            target.raw_trace = snapshot.raw_trace
        elif snapshot.counters or snapshot.raw_trace:
            result.append(snapshot)
    return result


class PortDiagnosticTracker:
    """Bounded, session-only observations; timestamps are when Asmond read them."""

    def __init__(self) -> None:
        self.previous: dict[str, PortDiagnostic] = {}
        self.logs: dict[str, deque] = {}
        self.totals: dict[str, dict[str, int]] = {}

    def invalidate_baselines(self) -> None:
        self.previous.clear()
        self.totals.clear()

    def update(self, ports: list[PortDiagnostic], now: float | None = None) -> list[PortDiagnostic]:
        timestamp = time.time() if now is None else now
        output = []
        active = {port.key for port in ports if port.mapped}
        for key in set(self.logs) - active:
            self.previous.pop(key, None)
            self.logs.pop(key, None)
            self.totals.pop(key, None)
        for port in ports:
            if not port.mapped:
                output.append(port)
                continue
            previous = self.previous.get(port.key)
            log = self.logs.setdefault(port.key, deque(maxlen=64))
            totals = self.totals.setdefault(port.key, {})
            if previous is None:
                log.append((timestamp, "Observation started (not lifetime history)"))
            else:
                for attr, title in (("connected", "Connection"), ("liquid_detected", "Liquid reported")):
                    old, new = getattr(previous, attr), getattr(port, attr)
                    if old is not None and new is not None and old != new:
                        state = ("connected" if new else "disconnected") if attr == "connected" else ("yes" if new else "no")
                        log.append((timestamp, f"{title}: {state}"))
                for name in totals.keys() - port.counters.keys():
                    totals.pop(name)
                for name, count in port.counters.items():
                    old = previous.counters.get(name)
                    if previous.counter_sources.get(name) != port.counter_sources.get(name):
                        totals.pop(name, None)
                        continue
                    if old is not None and count < old:
                        totals.pop(name, None)
                        log.append((timestamp, f"{name}: counter reset; new baseline"))
                    elif old is not None and count > old:
                        totals[name] = totals.get(name, 0) + count - old
                        log.append((timestamp, f"{name}: +{count - old} (reported counter)"))
            self.previous[port.key] = port
            output.append(replace(port, deltas=dict(totals), events=list(log)))
        return output


def liquid_label(port: PortDiagnostic) -> str:
    if port.liquid_detected is True:
        return "LIQUID REPORTED"
    if port.liquid_detected is False:
        return "not reported"
    return "unavailable"
