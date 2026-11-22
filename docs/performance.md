# Development resource measurements

`tools/benchmark-overhead.py` measures the actual Asmond process in a drained
pseudoterminal (PTY). It is a source-checkout development tool, uses only the
standard library and native macOS interfaces, and adds no runtime instrumentation.
No installed Asmond, personal settings, Homebrew package or recording is changed.

## Compare installed interpreters

From the checkout root, with native Python 3.10+:

```sh
python3 tools/benchmark-overhead.py \
  --python python3.14 --python python3.15 \
  --output "$HOME/Desktop/asmond-overhead-mock.json"
```

Each `--python` names an already installed interpreter or its full path. Nothing
is installed automatically. The default matrix uses Full and Compact, intervals
of 0.7 and 2 seconds, two repetitions, five seconds of warmup and thirty measured
seconds per case. With two interpreters this takes about ten minutes. Cases run
sequentially; interpreter order reverses on the second repetition.

Default mode uses Asmond's generated mock telemetry. The CPU and memory numbers
are real resource observations of that running UI, but do not measure the live
hardware collectors. To exercise real readers, use a separate result:

```sh
python3 tools/benchmark-overhead.py --live \
  --python python3.14 --python python3.15 \
  --output "$HOME/Desktop/asmond-overhead-live.json"
```

Run live mode in your own terminal. If sudo authorization is missing, the helper
requests it there for Asmond's existing powermetrics path; the helper and UI stay
unprivileged. It requires an observed live sample and rejects sampler
authorization/recovery warnings. Never transmit your password. The helper sends
`q` after each case and reaps its own child, with bounded termination on failure.

## What the numbers mean

- CPU is the change in the process's accumulated user/system CPU time divided by
  actual monotonic elapsed time. **100% means one logical CPU**; threads are
  included, subprocesses such as powermetrics, ps and ioreg are excluded.
- Native CPU counters use Mach time units, converted with `mach_timebase_info`.
  They are not assumed to be nanoseconds. A regression compares this conversion
  against Python's process CPU clock on the executing Mac.
- RSS and physical footprint are separate native byte readings sampled every
  roughly 0.5 seconds. Reported maxima are the largest observed values, not
  guaranteed lifetime peaks. They must not be added together.
- Startup ends at the first observed dashboard output marker. Three menu
  responses are measured separately, after the resource window, from sending
  `m` to receiving its output marker. These are PTY-observed latencies, not frame
  presentation times or terminal-emulator performance.

The JSON includes OS/model/architecture, terminal geometry, exact interpreter
versions, runtime-file hashes and each individual case. It contains no terminal
transcript or unrelated process list. Results are created with mode 0600 and
existing files are never overwritten. A failed run is marked `incomplete`; use a
new filename after correcting its cause. Interrupted cases are not valid results.

## Reproducibility and limits

Use the same source, machine, power state, geometry, theme and benchmark options.
Avoid competing workloads, simultaneous benchmark runs, sleep and source changes
during measurement. Keep individual repeats: a small difference is not proof
that one Python release is generally faster. Startup also depends on cache state.
Mock activity follows the machine's monotonic clock, so successive cases can
show different demo phases; they are not identical replay workloads. Treat this
as an application-resource baseline, not an isolated Python speed benchmark.
Each case records system load averages and the reported power source at its two
endpoints. A changed endpoint power source invalidates the run; these endpoint
checks cannot exclude an intervening change that returned to the original state.

Default geometry is 170 columns by 48 rows, theme Solar, I/O and the right process
panel enabled, ten-minute history and mouse reporting off. Content still follows
each layout's normal visibility rules. Short cases do not fill the complete
history retention window or establish long-session memory bounds. Use longer
cases separately when investigating those conditions.

This tool provides comparison baselines, not a fixed CPU/RAM performance gate in
CI. Live measurements still omit the resource cost of external reader processes
and the terminal emulator. Source counter semantics follow Apple's
[rusage layout](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/resource.h),
[rusage population](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/bsd_kern.c)
and [task CPU accounting](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/task.c).
