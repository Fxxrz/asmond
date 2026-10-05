#!/bin/bash

# Collect an anonymized Intel Mac validation bundle for parser and UI research.
# Run this as a normal user; the script elevates only powermetrics.
set -u
umask 077

usage() {
  echo "usage: $0 <state-label> [output-directory]" >&2
  echo "examples: charging, battery, charging-load, battery-load, reconnected" >&2
}

LABEL="${1:-}"
if [ "$LABEL" = "-h" ] || [ "$LABEL" = "--help" ]; then
  usage
  exit 0
fi
if [ -z "$LABEL" ]; then
  usage
  exit 2
fi

if ! printf '%s\n' "$LABEL" | /usr/bin/grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$'; then
  echo "state label must use 1-40 letters, numbers, dots, underscores or hyphens" >&2
  exit 2
fi

OUTPUT_DIR="${2:-$PWD}"
if [ ! -d "$OUTPUT_DIR" ]; then
  echo "output directory does not exist: $OUTPUT_DIR" >&2
  exit 2
fi
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

ARCHITECTURE="$(uname -m)"
if [ "$ARCHITECTURE" != "x86_64" ] && [ "${ASMOND_ALLOW_NON_INTEL:-0}" != "1" ]; then
  echo "this collector is for a native Intel Mac (found: $ARCHITECTURE)" >&2
  echo "set ASMOND_ALLOW_NON_INTEL=1 only for collector development/testing" >&2
  exit 1
fi

SAMPLE_COUNT="${ASMOND_SAMPLE_COUNT:-5}"
if ! printf '%s\n' "$SAMPLE_COUNT" | /usr/bin/grep -Eq '^[1-9]$|^10$'; then
  echo "ASMOND_SAMPLE_COUNT must be an integer from 1 through 10" >&2
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif [ -x /usr/bin/python3 ]; then
    PYTHON_BIN=/usr/bin/python3
  else
    echo "python3 is required to anonymize the collected plist data" >&2
    exit 1
  fi
fi

for tool in /usr/bin/sw_vers /usr/sbin/sysctl /usr/sbin/ioreg /usr/bin/pmset \
  /usr/sbin/system_profiler /usr/bin/powermetrics /usr/bin/sudo /usr/bin/tar \
  /usr/bin/shasum /usr/bin/grep /usr/bin/sort /usr/bin/find; do
  if [ ! -x "$tool" ]; then
    echo "required macOS tool is missing: $tool" >&2
    exit 1
  fi
done

STAMP="$(date '+%Y%m%d-%H%M%S')"
if ! WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/asmond-intel-mac.XXXXXX")" || [ -z "$WORK_DIR" ]; then
  echo "could not create a temporary working directory" >&2
  exit 1
fi
RAW_DIR="$WORK_DIR/raw"
OUT_DIR="$WORK_DIR/asmond-intel-mac-${LABEL}-${STAMP}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
    case "$(basename "$WORK_DIR")" in
      asmond-intel-mac.*) /bin/rm -rf "$WORK_DIR" ;;
    esac
  fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if ! mkdir -p "$RAW_DIR" "$OUT_DIR"; then
  echo "could not prepare the temporary capture directories" >&2
  exit 1
fi

sanitize_text() {
  "$PYTHON_BIN" - "$1" "$2" "${3:-text}" <<'PY'
import os
import re
import sys

source, target = sys.argv[1:3]
mode = sys.argv[3] if len(sys.argv) > 3 else "text"
with open(source, "r", encoding="utf-8", errors="replace") as handle:
    text = handle.read()

home = os.path.expanduser("~")
user = os.environ.get("USER", "")
host = ""
try:
    host = os.uname().nodename
except Exception:
    pass

text = re.sub(r"(?m)^proc_pidpath\s+\d+\b", "proc_pidpath <pid>", text)

if mode == "process-tasks":
    number = r"-?\d+(?:\.\d+)?"
    task_row = re.compile(r"^(.+?)\s+(-?\d+)\s+(" + number + r"(?:\s+" + number + r"){7})\s*$")
    sanitized_lines = []
    process_index = 0
    for line in text.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        match = task_row.match(line.rstrip("\r\n"))
        if match:
            pid = int(match.group(2))
            if pid >= 0:
                process_index += 1
                line = "process_%03d %d %s%s" % (process_index, 1000 + process_index, match.group(3), ending)
            else:
                line = "all_tasks -2 %s%s" % (match.group(3), ending)
        sanitized_lines.append(line)
    text = "".join(sanitized_lines)

text = re.sub(r"(?m)^Boot arguments:.*$", "Boot arguments: <redacted>", text)

if home and home != "/":
    text = text.replace(home, "~")
if user:
    text = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(user) + r"(?![A-Za-z0-9_.-])", "<user>", text)
if host:
    text = text.replace(host, "<host>")

text = re.sub(r"/Users/[^/\s]+", "/Users/<user>", text)
text = re.sub(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "<mac-address>", text)
text = re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27,}\b", "<uuid>", text)
text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip-address>", text)
text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "<email>", text)

with open(target, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
}

sanitize_plist() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import os
import plistlib
import re
import sys

source, target = sys.argv[1:3]
REDACT_KEYS = (
    "serial", "uuid", "guid", "udid", "owner", "username", "user_name",
    "hostname", "host_name", "computername", "computer_name",
    "mac_address", "boot_volume", "volume_name", "sales_order",
)
DROP_KEYS = {"kern_bootargs", "kern_boottime"}
PROCESS_ID_KEYS = {"pid", "process_id", "processid", "task_pid"}
PROCESS_PRIVATE_KEYS = {
    "name", "process_name", "command", "command_line", "path", "executable",
    "bundle_id", "bundle_identifier", "user", "uid",
}
process_ids = {}
home = os.path.expanduser("~")
user = os.environ.get("USER", "")
host = ""
try:
    host = os.uname().nodename
except Exception:
    pass

def normalized_key(key):
    return str(key).lower().replace(" ", "_").replace("-", "_")

def private_key(key):
    normalized = normalized_key(key)
    return any(token in normalized for token in REDACT_KEYS)

def clean_string(value):
    if home and home != "/":
        value = value.replace(home, "~")
    if user:
        value = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(user) + r"(?![A-Za-z0-9_.-])", "<user>", value)
    if host:
        value = value.replace(host, "<host>")
    value = re.sub(r"/Users/[^/\s]+", "/Users/<user>", value)
    value = re.sub(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "<mac-address>", value)
    value = re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27,}\b", "<uuid>", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip-address>", value)
    value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "<email>", value)
    return value

def synthetic_process_id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 0:
        return -2
    token = repr(value)
    if token not in process_ids:
        process_ids[token] = 1001 + len(process_ids)
    return process_ids[token]

def clean(value, process_mapping=False, inherited_process_id=None):
    if isinstance(value, dict):
        normalized_keys = {normalized_key(key) for key in value}
        is_process = process_mapping or (
            bool(normalized_keys & PROCESS_ID_KEYS)
            and any("gpu" in key or "energy" in key or "cpu" in key for key in normalized_keys)
        )
        process_id = inherited_process_id
        if is_process:
            for candidate_key, candidate_value in value.items():
                if normalized_key(candidate_key) in PROCESS_ID_KEYS:
                    process_id = synthetic_process_id(candidate_value)
                    break
        result = {}
        for key, item in value.items():
            normalized = normalized_key(key)
            if normalized in DROP_KEYS:
                continue
            if is_process and normalized in PROCESS_ID_KEYS:
                result[key] = process_id if process_id is not None else 1000
            elif is_process and normalized in PROCESS_PRIVATE_KEYS:
                result[key] = "<redacted-process>"
            else:
                result[key] = "<redacted>" if private_key(key) else clean(item, is_process, process_id)
        return result
    if isinstance(value, list):
        return [clean(item, process_mapping, inherited_process_id) for item in value]
    if isinstance(value, tuple):
        return [clean(item, process_mapping, inherited_process_id) for item in value]
    if isinstance(value, bytes):
        return "<binary data: %d bytes>" % len(value)
    if isinstance(value, str):
        return clean_string(value)
    return value

with open(source, "rb") as handle:
    raw = handle.read()

documents = []
for chunk in raw.split(b"\0"):
    chunk = chunk.strip()
    if not chunk:
        continue
    documents.append(plistlib.dumps(clean(plistlib.loads(chunk)), fmt=plistlib.FMT_XML, sort_keys=True))

if not documents:
    raise SystemExit("no plist document found in %s" % source)

with open(target, "wb") as handle:
    handle.write(b"\0".join(documents))
PY
}

run_text() {
  name="$1"
  shift
  raw="$RAW_DIR/${name}.txt"
  status_file="$OUT_DIR/${name}.status"
  "$@" >"$raw" 2>&1
  code=$?
  printf 'exit_code=%s\n' "$code" >"$status_file"
  sanitize_text "$raw" "$OUT_DIR/${name}.txt"
  return 0
}

run_process_text() {
  name="$1"
  shift
  raw="$RAW_DIR/${name}.txt"
  status_file="$OUT_DIR/${name}.status"
  "$@" >"$raw" 2>&1
  code=$?
  printf 'exit_code=%s\n' "$code" >"$status_file"
  sanitize_text "$raw" "$OUT_DIR/${name}.txt" process-tasks
  return 0
}

run_plist() {
  name="$1"
  shift
  raw="$RAW_DIR/${name}.plist"
  status_file="$OUT_DIR/${name}.status"
  "$@" >"$raw" 2>"$RAW_DIR/${name}.stderr"
  code=$?
  printf 'exit_code=%s\n' "$code" >"$status_file"
  sanitize_text "$RAW_DIR/${name}.stderr" "$OUT_DIR/${name}.stderr.txt"
  if [ "$code" -eq 0 ] && [ -s "$raw" ]; then
    if ! sanitize_plist "$raw" "$OUT_DIR/${name}.plist" 2>"$RAW_DIR/${name}.sanitize-error"; then
      sanitize_text "$RAW_DIR/${name}.sanitize-error" "$OUT_DIR/${name}.sanitize-error.txt"
    fi
  fi
  return 0
}

run_tui_smoke() {
  status_file="$OUT_DIR/asmond-tui-smoke.status"
  "$PYTHON_BIN" - "$status_file" "${ASM_CMD[@]}" <<'PY'
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

status_path = sys.argv[1]
command = sys.argv[2:]
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
env = os.environ.copy()
env.setdefault("TERM", "xterm-256color")
process = subprocess.Popen(
    command,
    stdin=slave,
    stdout=slave,
    stderr=slave,
    env=env,
)
os.close(slave)
captured = bytearray()
quit_sent = False
deadline = time.monotonic() + 15.0
quit_at = time.monotonic() + 8.0

try:
    while process.poll() is None and time.monotonic() < deadline:
        if not quit_sent and time.monotonic() >= quit_at:
            os.write(master, b"q")
            quit_sent = True
        ready, _, _ = select.select([master], [], [], 0.25)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            if len(captured) < 2_000_000:
                captured.extend(chunk[: 2_000_000 - len(captured)])
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
finally:
    os.close(master)

text = captured.decode("utf-8", "ignore")
with open(status_path, "w", encoding="utf-8") as handle:
    handle.write("exit_code=%s\n" % process.returncode)
    handle.write("quit_sent=%s\n" % int(quit_sent))
    handle.write("traceback_seen=%s\n" % int("Traceback (most recent call last)" in text))
    handle.write("bytes_seen=%s\n" % len(captured))
PY
}

cat >"$OUT_DIR/README.txt" <<EOF
Asmond Intel Mac research capture

State requested by operator: $LABEL
Captured: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
Collector version: 2

This package intentionally excludes serial numbers, UUIDs, host/user names,
network addresses, real process identities and Asmond settings. Process-task
rows retain only synthetic names/PIDs and their numeric telemetry. Binary IOKit
properties are replaced by their byte lengths. Only powermetrics runs through
sudo. Raw temporary files are deleted after the archive is created.

Recommended captures after each state has settled for about 30 seconds:
  charging, battery, charging-load, battery-load, reconnected

The two idle captures are required. Load and reconnect captures are optional,
but useful for clocks, thermals, fan response and stale charger-state checks.
Set ASMOND_SKIP_SUDO=1 for a partial capture, ASMOND_SKIP_TUI=1 to skip the
automated dashboard launch, or ASMOND_SAMPLE_COUNT=1..10 to adjust sampling.
EOF

{
  echo "label=$LABEL"
  echo "script_version=2"
  echo "python=$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
  echo "architecture=$ARCHITECTURE"
  echo "kernel=$(uname -r)"
  /usr/bin/sw_vers
  for key in hw.model hw.ncpu hw.physicalcpu hw.logicalcpu hw.memsize \
    machdep.cpu.brand_string machdep.cpu.family machdep.cpu.model machdep.cpu.stepping \
    machdep.cpu.core_count machdep.cpu.thread_count machdep.cpu.microcode_version \
    machdep.cpu.features machdep.cpu.leaf7_features machdep.cpu.extfeatures; do
    value="$(/usr/sbin/sysctl -n "$key" 2>/dev/null || true)"
    [ -n "$value" ] && printf '%s=%s\n' "$key" "$value"
  done
  if [ -d "$SCRIPT_DIR/../.git" ] && command -v git >/dev/null 2>&1; then
    commit="$(git -C "$SCRIPT_DIR/.." rev-parse HEAD 2>/dev/null || true)"
    [ -n "$commit" ] && printf 'asmond_git_commit=%s\n' "$commit"
  fi
} >"$RAW_DIR/system.txt"
sanitize_text "$RAW_DIR/system.txt" "$OUT_DIR/system.txt"

run_text powermetrics-help /usr/bin/powermetrics --help
run_text pmset-batt /usr/bin/pmset -g batt
run_text pmset-ps /usr/bin/pmset -g ps
run_text pmset-therm /usr/bin/pmset -g therm
run_text pmset-capabilities /usr/bin/pmset -g cap
run_text pmset-custom /usr/bin/pmset -g custom

run_plist battery /usr/sbin/ioreg -a -r -c AppleSmartBattery
run_plist battery-manager /usr/sbin/ioreg -a -r -c AppleSmartBatteryManager
run_plist charge-ports /usr/sbin/ioreg -a -r -c AppleHPMInterface
run_plist smc /usr/sbin/ioreg -a -r -c AppleSMC
run_plist smc-fan /usr/sbin/ioreg -a -r -c AppleSMCFan
run_plist hw-sensors /usr/sbin/ioreg -a -r -c IOHWSensor
run_plist platform-plugin /usr/sbin/ioreg -a -r -c IOPlatformPluginDevice
run_plist hardware-profile /usr/sbin/system_profiler SPHardwareDataType -xml
run_plist software-profile /usr/sbin/system_profiler SPSoftwareDataType -xml
run_plist power-profile /usr/sbin/system_profiler SPPowerDataType -xml
run_plist display-profile /usr/sbin/system_profiler SPDisplaysDataType -xml

ASM_CMD=()
if [ -n "${ASMOND:-}" ] && [ -f "$ASMOND" ]; then
  ASM_CMD=("$PYTHON_BIN" "$ASMOND")
elif command -v asmond >/dev/null 2>&1; then
  ASM_CMD=("$(command -v asmond)")
else
  if [ -f "$SCRIPT_DIR/../asmond.py" ]; then
    ASM_CMD=("$PYTHON_BIN" "$SCRIPT_DIR/../asmond.py")
  fi
fi

if [ "${#ASM_CMD[@]}" -gt 0 ]; then
  run_text asmond-version "${ASM_CMD[@]}" --version
  run_text asmond-doctor "${ASM_CMD[@]}" doctor --json
  run_text asmond-report "${ASM_CMD[@]}" report --json
else
  echo "Asmond executable not found; set ASMOND=/path/to/asmond.py to include its report." >"$OUT_DIR/asmond.status"
fi

SUDO_READY=0
if [ "${ASMOND_SKIP_SUDO:-0}" = "1" ]; then
  echo "privileged samples skipped by ASMOND_SKIP_SUDO=1" >"$OUT_DIR/powermetrics.status"
elif [ ! -t 0 ]; then
  echo "privileged samples skipped because no interactive terminal is available" >"$OUT_DIR/powermetrics.status"
else
  echo "Asmond research capture needs one sudo authorization for powermetrics." >&2
  if /usr/bin/sudo -v; then
    SUDO_READY=1
  else
    echo "sudo authorization failed; privileged samples were skipped" >"$OUT_DIR/powermetrics.status"
  fi
fi

if [ "$SUDO_READY" -eq 1 ]; then
  PM_HELP="$RAW_DIR/powermetrics-help.txt"
  /usr/bin/powermetrics --help >"$PM_HELP" 2>&1 || true
  PM_SAMPLERS=""
  for sampler in cpu_power gpu_power thermal smc gpu_agpm_stats; do
    if /usr/bin/grep -Eq "(^|[^A-Za-z0-9_])${sampler}([^A-Za-z0-9_]|$)" "$PM_HELP"; then
      if [ -n "$PM_SAMPLERS" ]; then
        PM_SAMPLERS="${PM_SAMPLERS},${sampler}"
      else
        PM_SAMPLERS="$sampler"
      fi
    fi
  done

  if [ -n "$PM_SAMPLERS" ]; then
    PM_ARGS=(--samplers "$PM_SAMPLERS" --sample-rate 1000 --sample-count "$SAMPLE_COUNT")
    for option in --buffer-size --poweravg --show-plimits --show-extra-power-info --handle-invalid-values; do
      if /usr/bin/grep -q -- "$option" "$PM_HELP"; then
        case "$option" in
          --buffer-size|--poweravg) PM_ARGS+=("$option" 1) ;;
          *) PM_ARGS+=("$option") ;;
        esac
      fi
    done
    printf 'samplers=%s\nsample_count=%s\n' "$PM_SAMPLERS" "$SAMPLE_COUNT" >"$OUT_DIR/powermetrics-selection.txt"
    run_plist powermetrics /usr/bin/sudo -n /usr/bin/powermetrics "${PM_ARGS[@]}" --format plist
    run_text powermetrics-text /usr/bin/sudo -n /usr/bin/powermetrics "${PM_ARGS[@]}" --format text
  else
    echo "no targeted Intel powermetrics sampler was listed by this macOS version" >"$OUT_DIR/powermetrics.status"
  fi

  if /usr/bin/grep -q -- "--show-process-gpu" "$PM_HELP" \
    && /usr/bin/grep -Eq '(^|[^A-Za-z0-9_])tasks([^A-Za-z0-9_]|$)' "$PM_HELP"; then
    TASK_ARGS=(--samplers tasks --show-process-gpu --sample-rate 1000 --sample-count 1)
    if /usr/bin/grep -q -- "--show-process-energy" "$PM_HELP"; then
      TASK_ARGS+=(--show-process-energy)
    fi
    if /usr/bin/grep -q -- "--buffer-size" "$PM_HELP"; then
      TASK_ARGS+=(--buffer-size 1)
    fi
    if /usr/bin/grep -q -- "--handle-invalid-values" "$PM_HELP"; then
      TASK_ARGS+=(--handle-invalid-values)
    fi
    run_plist powermetrics-tasks /usr/bin/sudo -n /usr/bin/powermetrics "${TASK_ARGS[@]}" --format plist
    run_process_text powermetrics-tasks-text /usr/bin/sudo -n /usr/bin/powermetrics "${TASK_ARGS[@]}" --format text
  else
    echo "process GPU/task sampling is not advertised by this powermetrics version" >"$OUT_DIR/powermetrics-tasks.status"
  fi

  if [ "${#ASM_CMD[@]}" -gt 0 ]; then
    run_text asmond-probe-raw-sanitized "${ASM_CMD[@]}" probe --raw
    run_text asmond-live-report "${ASM_CMD[@]}" report --live --json
    if [ "${ASMOND_SKIP_TUI:-0}" = "1" ]; then
      echo "dashboard smoke test skipped by ASMOND_SKIP_TUI=1" >"$OUT_DIR/asmond-tui-smoke.status"
    else
      run_tui_smoke
    fi
  fi
fi

if ! (
  cd "$OUT_DIR" || exit 1
  /usr/bin/find . -type f ! -name manifest.sha256 -print \
    | /usr/bin/sort \
    | while IFS= read -r file; do /usr/bin/shasum -a 256 "$file"; done
) >"$OUT_DIR/manifest.sha256"; then
  echo "could not create the bundle manifest" >&2
  exit 1
fi

ARCHIVE="$OUTPUT_DIR/asmond-intel-mac-${LABEL}-${STAMP}.tgz"
if ! COPYFILE_DISABLE=1 /usr/bin/tar \
    --no-mac-metadata --no-xattrs --no-acls --no-fflags \
    --uid 0 --gid 0 --uname root --gname wheel \
    -czf "$ARCHIVE" -C "$WORK_DIR" "$(basename "$OUT_DIR")"; then
  echo "could not create the output archive" >&2
  exit 1
fi
if ! (
  cd "$OUTPUT_DIR" || exit 1
  /usr/bin/shasum -a 256 "$(basename "$ARCHIVE")"
) >"$ARCHIVE.sha256"; then
  echo "could not create the archive checksum" >&2
  exit 1
fi
echo "done: $ARCHIVE"
echo "sha256: $ARCHIVE.sha256"
