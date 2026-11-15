# Optional local recording

This optional feature provides a manually started, headless monitoring mode. It reuses Asmond's macOS readers and leaves the curses interface and keyboard controls unchanged. No daemon, automatic restart/resume, database, network service, external notification service, process termination or additional runtime dependency is introduced. You can run the regular TUI separately; that creates a second set of readers, not a shared background service.

```sh
python3 asmond.py record
python3 asmond.py record --interval 2 --duration 3600 --max-size-mib 256
python3 asmond.py record --groups cpu,memory,processes,io --output-dir "$HOME/Desktop/Asmond-recordings"
```

Installed commands use `asmond record ...` and `asmond export ...`; source checkouts use `python3 asmond.py` as above. Recording and export are available starting with **0.9.0**. Use options after `record`; its default interval is 10 seconds, independently of saved TUI settings. Valid intervals are 1–3600 seconds. Stop with Ctrl+C; SIGTERM also requests a clean stop. A closed terminal may stop the foreground process; use an existing terminal/session arrangement if you want to disconnect. No launchd service or login hook is created.

## Selection and privileges

`--groups` accepts distinct comma-separated names: `cpu,power,temperatures,memory,processes,io,battery`. All are selected by default. Disabled groups are not polled. Use the memory/process/CPU/I/O example above when power telemetry is unnecessary: it does not request sudo. `temperatures` without `power` uses only the existing unprivileged SMC/HID path; Intel temperature sources available only through powermetrics may be missing.

Selecting `power` starts the existing continuous powermetrics reader and asks for its sudo authorization once; the recorder/UI remain unprivileged. If ordinary termination is denied, cached noninteractive sudo may signal only its exact, unreaped sampler child so long recording intervals do not delay shutdown. The existing sudo keepalive runs during recording. Failures, credential expiry, stream exits and stale streams produce explicit missing data/source transitions, with bounded retries, not fake zero power. Permission is not silently escalated again. macOS must allow a fresh authorization before the next manual session after a reboot.

`cpu` records the measured Mach host-average utilization, plus existing per-core/GPU readings when the power stream supplies them. No utilization is inferred from power. Temperatures use only existing verified groups and named fallback provenance; unassigned raw comparison keys are not promoted or included as physical temperatures. Temperature-reader provenance remains group-level, not invented per-value metadata.

## Files and storage limits

Power-only recording does not start the supplementary SMC/HID temperature/fan
readers. CPU/thermal fields already present in the same powermetrics payload do
not require an extra probe. Cached supplementary readings expire if a refresh
stalls, rather than being relabelled as current by a new power sample.

The supplementary cache's maximum age follows the consumer interval:
`max(5 seconds, 3 × requested interval)`. This allows the normal asynchronous
refresh at longer intervals while still expiring a blocked reader. The ordinary
supplementary refresh cadence is unchanged; age starts when acquisition begins.

The default parent is `~/Library/Logs/Asmond`. Every invocation creates a new private `asmond-record-...` session directory. Session directories are created with mode 0700 and JSONL/CSV files with mode 0600. On macOS, Asmond also checks the new output items for ACL grants before writing data. If an inherited grant is present or permissions cannot be verified, choose a private destination. The existing parent permissions are left intact. The output parent must be owned by the current user, not group/world-writable, and not a symlink. Existing recordings are never appended to, overwritten, automatically removed or resumed.

One JSONL file per **UTC day** contains schema-versioned `session_start`, `sample`, state-transition `event` and `session_end` records. Additional numbered files prevent overwriting if the clock moves back to an earlier day. Each complete row is flushed; forced termination/power loss can still leave an unfinished last row or no end marker. There is no claim of crash-proof storage or automatic repair. Avoid copying/exporting a still-growing session.

The default total JSONL limit is **256 MiB**, including metadata/events, not a retention timer. A small portion is reserved for the final stop record. `--max-size-mib` sets the limit; reaching it stops recording without deleting older files. Available duration depends on selected groups, interval, core count and process names. The first day of output provides a useful estimate for your server. Disk/permission errors stop writing with an error; an end marker cannot be guaranteed after a disk failure. Keep recordings out of Git and review them before sharing: process names, PIDs and operating patterns are sensitive even without arguments or user names.

## Process and memory measurements

- Two independent lists contain up to ten processes each: `top_cpu` and `top_memory`. The same process can appear in both. Memory ranks use resident bytes (RSS), not Activity Monitor's memory footprint. Shared resident pages can be counted in more than one process and must not be summed into total host RAM.
- Process records contain PID, reported start time, executable basename, RSS, RAM percentage, accumulated CPU seconds and CPU percentage over the actual measured interval. No user name, full path, environment, arguments or full command line is written. Recording uses `ps comm=` and does not fall back to `command=`.
- Interval CPU% comes from accumulated CPU-time differences, not `ps`'s decaying average. 100% means one logical CPU; multithreaded processes may exceed 100%. Source precision is limited by `ps`'s CPU-time resolution. The first reading, newly observed/reused identities, decreasing counters and long gaps have no interval CPU value. The first CPU ranking is empty rather than fabricated; memory ranks are immediately available. Exited processes between polls cannot be reconstructed, and short-lived activity can be missed.
- RAM `used_bytes` follows Asmond's existing active+wired definition, explicitly labelled. Pressure percent is `100 - memory_pressure`'s reported system-free percent, not Apple's native normal/warning/critical state. Missing source fields are null; the TUI's legacy fallback/default fields are not silently treated as successful logger reads.
- Compressor occupancy and the uncompressed content held in the compressor are distinct fields. Swap occupancy is independent of current swap activity.
- `Swapins`, `Swapouts`, `Compressions` and `Decompressions` are accumulated **page counters**. The recorder stores totals, deltas, actual interval duration, pages/s and MiB-equivalent/s using the reported page size, never a fixed 4 KiB assumption. Swap activity reflects the OS's compressed-swap page accounting, including maintenance, not exact physical SSD traffic or a process-specific swap quantity. Compression/decompression rates represent uncompressed-page equivalents, not compressed output bytes. Pageins/Pageouts are not substituted for Swapins/Swapouts.
- First observations, missing reads, resets, page-size changes and gaps greater than `max(5 s, 2.5 × requested interval)` reset the applicable rate baseline. A genuine zero delta remains zero. Disk and network rates each need a comparable set of counter sources. Adding or removing a disk or interface starts a new baseline for that group. A counter reset invalidates the affected direction. A failed disk or network read resets only its own baseline. The other source keeps its rates; the recorded I/O status marks partial availability. Failed acquisition never becomes a fabricated zero or an invisible reuse of an old baseline.

Requested intervals do not make the readers simultaneous. Samples contain monotonic acquisition bounds, actual collection duration and power-stream receive age. Source latency or suspend/resume can reduce frequency; there is no burst of synthetic catch-up samples. Prefer a short trial to measure collector overhead before choosing a 1-second interval for days of monitoring. Mock mode is explicitly marked generated and must not mix real side metrics into its samples.

Long sampling pauses or divergence between wall-clock and monotonic elapsed time are recorded as an observed clock discontinuity, not guessed to be sleep or a hardware fault. Rate baselines and received power are invalidated before the next measurement; fresh stream data is requested through the existing reader. Late samples from the invalidated stream cannot become fresh again. An observed gap also interrupts a pending retry delay so the reader can restart promptly. The SSD-temperature cache is also cleared; an in-flight read from before the gap cannot repopulate it, and a blocked native call does not spawn extra workers. This also handles systems whose monotonic clock does not advance during suspension. Recorded process start times have the resolution reported by `ps`, not an invented higher-precision identity.

## Events

Events record **observed** state transitions, not exact hardware occurrence times: data-source availability, explicitly reported throttling and crossings of Asmond's existing temperature, swap-occupancy and battery-drain thresholds. Initial states are logged once; an unchanged warning is not repeated on every sample. Missing measurements become an unavailable state, not a false recovery. Existing thresholds are read from settings without saving or changing them. Swap occupancy alone is not evidence of ongoing swap activity, and co-occurring process growth is not a proven cause of system-wide swap I/O.

Battery-drain events use the dashboard's strictly-above rule: exactly the configured drain is clear; a 0 W threshold activates on any discharge. Temperature and enabled swap thresholds include equality; a 0-byte swap threshold disables that event. Missing measurements remain unavailable for enabled thresholds.

The session header records the effective numeric thresholds and non-identifying OS/Python/architecture metadata. Clock discontinuities also create observation events. Only the recorder's own sampler subprocess is stopped during shutdown/retry; observed processes are never signalled.

## Session summary

Read an existing recording after it stops:

```sh
asmond summary "/path/to/asmond-record-session"
```

From a source checkout, use `python3 asmond.py summary` instead.

The summary reads the JSONL files once and prints to the terminal. It shows UTC
timestamps, monotonic elapsed time, the recorded stop reason, sample count,
sampling gaps and the longest sample spacing. Gaps count the recorder's marked
clock changes and monotonic sample intervals longer than the recorder's gap
threshold. A session without an end marker shows its last recorded timestamp
and an unknown stop reason.

The summary shows available peaks for host CPU, memory pressure, occupied swap
and swap activity. At positive swap-in and swap-out peaks it includes the first
available CPU and RSS leaders from the same sample. Those rankings are
observations alongside system-wide swap counters. The report may contain
process names and PIDs, so review its contents before sharing.

## CSV export

The exporter validates timestamps, finite numbers and nested measurement/process
types. It compares streamed input hashes across both passes and reports an error
if the recording changes during export. Stop the recorder before retrying.

After stopping the recorder:

```sh
python3 asmond.py export "/path/to/asmond-record-session" --output-dir "$HOME/Desktop/Asmond-CSV"
```

The output directory must be new. It receives `metrics.csv` (one row per sample, including source/status/age fields), `processes.csv` (one row per ranked process) and `events.csv`. Missing data becomes empty CSV cells. Formula-like text is escaped for spreadsheet use; numeric negative measurements remain numbers. Input remains unchanged. Oversized, malformed, incomplete or unsupported-schema rows are rejected. If a failed or interrupted export created its output directory, the error names the path where incomplete CSV files may remain. Inspect those files separately and choose a new `--output-dir` to retry. The exporter streams records in two passes instead of loading days of samples into RAM.
