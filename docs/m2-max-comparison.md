# M2 Max temperature comparison

This is an unreleased, read-only diagnostic for the tested **Mac14,5 / Apple M2 Max / 8P+4E**, not a promise of a complete temperature map. Python 3.10+ must run natively as arm64. Only Python's standard library, the extracted Asmond modules and existing macOS frameworks are used. No sudo, extra package, helper daemon, fan control, hardware write, automatic workload, screen capture or settings change is performed by the collector.

## What is already established

- M4 CPU/GPU averages use the verified individual sensor groups, not `TCMb`.
- The M2 Max comparison shows zero/fixed-offset CPU aliases. `TCMb` equals an observed bank maximum when a bank is readable; this does not establish a complete CPU maximum or average. `TCMb` and `TCMz` remain unassigned.
- The narrow Mac14,5 auxiliary map enables AirPort (`TW0P`), trackpad (`Ts0P`) and actuator (`Ts1P`). Other chassis, CPU/GPU/core labels and supply proximity are not inferred. Named auxiliary fields no longer duplicate these keys in the unassigned raw list.

## Prepare once on the M2 Max

1. Put the supplied test archive on the **Desktop** and expand it. Keep all files together. The examples below assume its folder is named `asmond-m2-compare-20261028`; adapt only that folder name if yours differs. This does not install or replace Homebrew Asmond.
2. Keep the charger state and connected devices unchanged throughout the run. Let the Mac settle first. Leave Macs Fan Control open in automatic mode with the complete sensor list visible. Close Asmond and unnecessary monitoring applications during the comparison.
3. Have an **already installed** CPU-heavy task and a GPU-heavy task ready. Start/stop them when the terminal changes phase. Prefer a CPU render/export with GPU acceleration disabled, then a GPU render or existing 3D workload. The collector does not launch either task and cannot prove that they isolate CPU and GPU perfectly. If no GPU task is available, do not install anything just for this test; report that the GPU phase was idle/unavailable.
4. Use macOS **Shift–Command–5** to record only the region containing Macs Fan Control and the collector terminal. Both the sensor names/values and the terminal's timestamp/phase must remain readable. Exclude private windows and notifications; disable microphone capture. The recording is manual and is **not** included in the collector archive.

## Start the collector

Run in Terminal as the normal user:

```sh
/opt/homebrew/opt/python@3.14/bin/python3.14 "$HOME/Desktop/asmond-m2-compare-20261028/tools/compare-m2-max-temperatures.py"
```

The command uses the native Homebrew Python already present on the tested M2. Another suitable native Python can be used instead; do not install a new interpreter if the existing one works. A missing folder error means the archive has not been expanded at the example Desktop path. A copied script outside the extracted tree needs `--source-dir /actual/path/to/asmond`.

The collector waits for Enter. Start the region recording **before pressing Enter**. Then follow five automatic phases, each 90 seconds (about 7½ minutes total):

| Phase | Your action |
| --- | --- |
| `idle` | Leave the Mac idle. |
| `cpu` | Start the prepared CPU-heavy task. |
| `cooldown_cpu` | Stop that task and allow cooling. |
| `gpu` | Start the prepared GPU-heavy task. |
| `cooldown_gpu` | Stop that task and allow cooling. |

The terminal shows local time including milliseconds, the current phase, remaining seconds and the sample count. Leave it visible beside MFC. Actual transitions need not be instant: the video shows their timing. If preparing a task takes longer, note it when sending the result. `--phase-seconds 120` allows more settling time; the permitted range is 30–300. Stop with Ctrl+C if necessary; received data is packaged with `status=interrupted`, not presented as a complete run. A failed capture is labelled `failed`.

## Send back

- The newly generated **`asmond-m2-temperature-….tgz`** and matching **`.tgz.sha256`**, written to the Desktop.
- The corresponding screen recording, privately, plus a short description of the CPU/GPU tasks and any phase you could not perform. Recording location follows the macOS recording settings, not the collector's output directory.
- If an auxiliary reading clearly differs, mention the sensor name and approximate time in the recording. No additional generic hardware dump is needed.

The archive contains hardware model/OS/Python versions, phase events, raw temperature/fan keys, per-key start/end wall-clock and monotonic timestamps, named HID temperature readings, a host CPU-load average and checksums. Numeric zeros, fixed offsets, missing reads and raw `ioft` bytes remain distinguishable; `ioft` is not silently interpreted as a CPU/GPU temperature. The host CPU-load average is measured from Mach counters and is not per-core telemetry or evidence of GPU load. Reads are sequential, not simultaneous; an overrun reduces sample frequency rather than inventing extra samples. No process list, full settings, user/host name, serial, arbitrary SMC string or automatic screenshot is collected. Review the recording and files before sharing; do not publish private support bundles in Git.

This test can establish group response, refresh behavior and agreement with named MFC readings. It cannot guarantee individual physical-core numbering: a scheduler-managed workload does not prove which core a sensor belongs to. Exact labels may still require model-specific key metadata or a further narrowly targeted comparison. Ambiguous sensors stay unavailable, with no old-reading substitution or PMU-as-CPU fallback.
