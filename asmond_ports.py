"""Read-only port diagnostics; never infer physical identity from array order."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import replace
from typing import Any

from asmond_models import PortDiagnostic


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

    def update(self, ports: list[PortDiagnostic], now: float | None = None) -> list[PortDiagnostic]:
        timestamp = time.time() if now is None else now
        output = []
        active = {port.key for port in ports if port.mapped}
        for key in set(self.previous) - active:
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
                for name, count in port.counters.items():
                    old = previous.counters.get(name)
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
