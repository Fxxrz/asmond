#!/bin/bash

# Collect a small, anonymized Intel Mac telemetry bundle for parser research.
# Run this as a normal user; the script elevates only powermetrics.
set -u
umask 077

LABEL="${1:-}"
if [ -z "$LABEL" ]; then
  echo "usage: $0 <charging|battery> [output-directory]" >&2
  exit 2
fi

case "$LABEL" in
  charging|battery) ;;
  *)
    echo "label must be 'charging' or 'battery'" >&2
    exit 2
    ;;
esac

OUTPUT_DIR="${2:-$PWD}"
if [ ! -d "$OUTPUT_DIR" ]; then
  echo "output directory does not exist: $OUTPUT_DIR" >&2
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
  /usr/sbin/system_profiler /usr/bin/powermetrics /usr/bin/sudo /usr/bin/tar; do
  if [ ! -x "$tool" ]; then
    echo "required macOS tool is missing: $tool" >&2
    exit 1
  fi
done

STAMP="$(date '+%Y%m%d-%H%M%S')"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/asmond-intel-mac.XXXXXX")"
RAW_DIR="$WORK_DIR/raw"
OUT_DIR="$WORK_DIR/asmond-intel-mac-${LABEL}-${STAMP}"
mkdir -p "$RAW_DIR" "$OUT_DIR"

cleanup() {
  /bin/rm -rf "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

sanitize_text() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import os
import re
import sys

source, target = sys.argv[1:3]
with open(source, "r", encoding="utf-8", errors="replace") as handle:
    text = handle.read()

home = os.path.expanduser("~")
user = os.environ.get("USER", "")
host = ""
try:
    host = os.uname().nodename
except Exception:
    pass

if home and home != "/":
    text = text.replace(home, "~")
if user:
    text = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(user) + r"(?![A-Za-z0-9_.-])", "<user>", text)
if host:
    text = text.replace(host, "<host>")

text = re.sub(r"/Users/[^/\s]+", "/Users/<user>", text)
text = re.sub(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "<mac-address>", text)
text = re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27,}\b", "<uuid>", text)

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
    "serial", "uuid", "guid", "owner", "username", "user_name",
    "hostname", "computername", "mac-address", "mac_address",
)
DROP_KEYS = {"kern_bootargs", "kern_boottime"}
home = os.path.expanduser("~")
user = os.environ.get("USER", "")
host = ""
try:
    host = os.uname().nodename
except Exception:
    pass

def private_key(key):
    normalized = str(key).lower().replace(" ", "").replace("-", "_")
    return any(token.replace("-", "_") in normalized for token in REDACT_KEYS)

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
    return value

def clean(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace(" ", "_").replace("-", "_")
            if normalized in DROP_KEYS:
                continue
            result[key] = "<redacted>" if private_key(key) else clean(item)
        return result
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, tuple):
        return [clean(item) for item in value]
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

cat >"$OUT_DIR/README.txt" <<EOF
Asmond Intel Mac research capture

State requested by operator: $LABEL
Captured: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

This package intentionally excludes serial numbers, UUIDs, host/user names,
network addresses, process lists and Asmond settings. Binary IOKit properties
are replaced by their byte lengths. Only powermetrics runs through sudo.
Set ASMOND_SKIP_SUDO=1 to create a non-privileged partial capture.
EOF

{
  echo "label=$LABEL"
  echo "script_version=1"
  echo "python=$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
  echo "architecture=$(uname -m)"
  echo "kernel=$(uname -r)"
  /usr/bin/sw_vers
  for key in hw.model hw.ncpu hw.physicalcpu hw.logicalcpu hw.memsize machdep.cpu.brand_string; do
    value="$(/usr/sbin/sysctl -n "$key" 2>/dev/null || true)"
    [ -n "$value" ] && printf '%s=%s\n' "$key" "$value"
  done
} >"$RAW_DIR/system.txt"
sanitize_text "$RAW_DIR/system.txt" "$OUT_DIR/system.txt"

run_text powermetrics-help /usr/bin/powermetrics --help
run_text pmset-batt /usr/bin/pmset -g batt
run_text pmset-ps /usr/bin/pmset -g ps
run_text pmset-therm /usr/bin/pmset -g therm

run_plist battery /usr/sbin/ioreg -a -r -c AppleSmartBattery
run_plist battery-manager /usr/sbin/ioreg -a -r -c AppleSmartBatteryManager
run_plist smc /usr/sbin/ioreg -a -r -c AppleSMC
run_plist smc-fan /usr/sbin/ioreg -a -r -c AppleSMCFan
run_plist hw-sensors /usr/sbin/ioreg -a -r -c IOHWSensor
run_plist power-profile /usr/sbin/system_profiler SPPowerDataType -xml
run_plist display-profile /usr/sbin/system_profiler SPDisplaysDataType -xml

ASM_CMD=()
if [ -n "${ASMOND:-}" ] && [ -f "$ASMOND" ]; then
  ASM_CMD=("$PYTHON_BIN" "$ASMOND")
elif command -v asmond >/dev/null 2>&1; then
  ASM_CMD=("$(command -v asmond)")
else
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
  PM_ARGS=(--samplers cpu_power,gpu_power,thermal,smc,gpu_agpm_stats --sample-rate 1000 --sample-count 3)
  for option in --buffer-size --poweravg --show-plimits --show-extra-power-info --handle-invalid-values; do
    if /usr/bin/grep -q -- "$option" "$PM_HELP"; then
      case "$option" in
        --buffer-size|--poweravg) PM_ARGS+=("$option" 1) ;;
        *) PM_ARGS+=("$option") ;;
      esac
    fi
  done

  run_plist powermetrics /usr/bin/sudo -n /usr/bin/powermetrics "${PM_ARGS[@]}" --format plist
  run_text powermetrics-text /usr/bin/sudo -n /usr/bin/powermetrics "${PM_ARGS[@]}" --format text

  if [ "${#ASM_CMD[@]}" -gt 0 ]; then
    run_text asmond-live-report "${ASM_CMD[@]}" report --live --json
  fi
fi

ARCHIVE="$OUTPUT_DIR/asmond-intel-mac-${LABEL}-${STAMP}.tgz"
COPYFILE_DISABLE=1 /usr/bin/tar \
  --no-mac-metadata --no-xattrs --no-acls --no-fflags \
  --uid 0 --gid 0 --uname root --gname wheel \
  -czf "$ARCHIVE" -C "$WORK_DIR" "$(basename "$OUT_DIR")"
echo "done: $ARCHIVE"
