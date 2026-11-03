from __future__ import annotations

import os
import platform
import subprocess
from typing import Callable


SUDO_PATH = "/usr/bin/sudo"
POWERMETRICS_PATH = "/usr/bin/powermetrics"
IOREG_PATH = "/usr/sbin/ioreg"
SYSCTL_PATH = "/usr/sbin/sysctl"
VM_STAT_PATH = "/usr/bin/vm_stat"
MEMORY_PRESSURE_PATH = "/usr/bin/memory_pressure"
NETSTAT_PATH = "/usr/sbin/netstat"
PS_PATH = "/bin/ps"

SYSTEM_COMMANDS = {
    "sudo": SUDO_PATH,
    "powermetrics": POWERMETRICS_PATH,
    "ioreg": IOREG_PATH,
    "sysctl": SYSCTL_PATH,
    "vm_stat": VM_STAT_PATH,
    "memory_pressure": MEMORY_PRESSURE_PATH,
    "ps": PS_PATH,
    "netstat": NETSTAT_PATH,
}

RunCommand = Callable[..., subprocess.CompletedProcess]


def system_environment() -> dict[str, str]:
    """Return a small environment for trusted macOS command-line tools."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
    for name in ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def physical_machine(
    machine: str | None = None,
    run: RunCommand = subprocess.run,
) -> tuple[str, bool]:
    """Return the physical Mac architecture and whether Python runs via Rosetta."""
    process_machine = (machine or platform.machine() or "unknown").lower()
    if process_machine not in {"x86_64", "amd64"}:
        return process_machine, False
    try:
        proc = run(
            [SYSCTL_PATH, "-n", "sysctl.proc_translated"],
            check=False,
            capture_output=True,
            timeout=1.0,
            env=system_environment(),
        )
    except Exception:
        return process_machine, False
    translated = proc.returncode == 0 and proc.stdout.decode("utf-8", "ignore").strip() == "1"
    return ("arm64", True) if translated else (process_machine, False)
