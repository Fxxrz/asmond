# M2 Max temperature comparison

This read-only comparison tool is restricted to the tested **Mac14,5 / Apple M2 Max / 8P+4E**. It collects evidence for temperature mapping; it does not establish every physical-core identifier or a continuously available idle CPU temperature. Python 3.10+ must run natively as arm64. Only Python's standard library, the Asmond source modules and existing macOS frameworks are used. No sudo, extra package, helper daemon, fan control, hardware write, automatic workload, screen capture or settings change is performed by the collector.

## What is already established

- M4 CPU/GPU averages use the verified individual sensor groups, not `TCMb`.
- The M2 Max comparison shows zero/fixed-offset CPU aliases. `TCMb` equals an observed bank maximum when a bank is readable; this does not establish a complete CPU maximum or average. `TCMb` and `TCMz` remain unassigned.
- The narrow Mac14,5 map enables the five verified GPU-cluster readings (`Tg0f`, `Tg0j`, `Tg1h`, `Tg1l`, `Tg2f`), AirPort (`TW0P`), power-supply proximity (`TPMP`), trackpad (`Ts0P`) and actuator (`Ts1P`). GPU average/maximum require the complete valid five-key group; unavailable members do not reuse older readings or substitute another GPU key. Other chassis and CPU/core labels are not inferred. Mapped fields do not duplicate their keys in the unassigned raw list.
- A subsequent controlled CPU-only comparison verifies a twelve-sensor CPU group for this exact model. Mean/maximum require every adjusted reading and its corresponding base; bases are read before and after the group. Zero/missing bases with positive fixed offsets invalidate the group. Unavailable readings do not reuse an old value, average a subset or substitute `TCMb`, `TCMz` or `Te05`. Global powermetrics core IDs remain unassigned.
- A plausible reference-app CPU average does not prove twelve current per-core measurements. During missing raw CPU banks the comparison shows several named reference rows sharing a common value. Asmond does not copy that value into independent cores or treat it as a complete CPU average.

## Obtain the source and select Python

Use a complete Asmond source tree containing `tools/compare-m2-max-temperatures.py` and the runtime modules. For example, with Git already available:

```sh
git clone --depth 1 https://github.com/Fxxrz/asmond.git "$HOME/Desktop/asmond-comparison"
```

Alternatively, download and expand the [GitHub source ZIP](https://github.com/Fxxrz/asmond/archive/refs/heads/main.zip), or copy a complete checkout from another Mac. Adapt the example folder path below to the actual extracted directory. A Homebrew/direct-install runtime package alone does not include this `tools/` script. When comparing a particular release or development candidate, use its matching source tree; the collector records the source's Asmond version. This source copy does not install or replace an existing Asmond command.

Use an existing native arm64 Python 3.10+ outside a Rosetta terminal. The commands below use `python3`; replace it with the appropriate interpreter path if needed, such as `/opt/homebrew/opt/python@3.14/bin/python3.14` for an existing Homebrew Python. No additional Python packages are needed.

## Prepare the comparison

1. Keep the charger state and connected devices unchanged throughout the run. Let the Mac settle first. Leave an existing Macs Fan Control installation (1.5.23 or newer) open in automatic mode with the complete sensor list visible. Version 1.5.23 corrected idle M2-cluster temperature behavior; see the [official release notes](https://crystalidea.com/macs-fan-control/release-notes). A corrected reference display still does not establish the physical identity/currentness of every raw core key. Close Asmond and unnecessary monitoring applications during the comparison.
2. Have an **already installed** CPU-heavy task and a GPU-heavy task ready. Start/stop them when the terminal changes phase. Prefer a CPU render/export with GPU acceleration disabled, then a GPU render or existing 3D workload. The collector does not launch either task and cannot prove that they isolate CPU and GPU perfectly. If no GPU task is available, do not install anything just for this test; report that the GPU phase was idle/unavailable.
3. Use macOS **Shift–Command–5** to record only the region containing Macs Fan Control and the collector terminal. Both the sensor names/values and the terminal's timestamp/phase must remain readable. Exclude private windows and notifications; disable microphone capture. The recording is manual and is **not** included in the collector archive.

## Start the collector

Run in Terminal as the normal user:

```sh
python3 "$HOME/Desktop/asmond-comparison/tools/compare-m2-max-temperatures.py"
```

The script locates the source modules relative to its own file, so it can be launched from any working directory. A missing-file error means the command does not match the source location; check the extracted folder name. If the script was copied separately, also pass `--source-dir /actual/path/to/asmond` pointing to the complete source tree.

The collector waits for Enter. Start the region recording **before pressing Enter**. Then follow five automatic phases, each 90 seconds (about 7½ minutes total):

| Phase | Your action |
| --- | --- |
| `idle` | Leave the Mac idle. |
| `cpu` | Start the prepared CPU-heavy task. |
| `cooldown_cpu` | Stop that task and allow cooling. |
| `gpu` | Start the prepared GPU-heavy task. |
| `cooldown_gpu` | Stop that task and allow cooling. |

The terminal shows local time including milliseconds, the current phase, remaining seconds and the sample count. Leave it visible beside MFC. Actual transitions need not be instant: the video shows their timing. If preparing a task takes longer, note it with the result. `--phase-seconds 120` allows more settling time; the permitted range is 30–300. Stop with Ctrl+C if necessary; received data is packaged with `status=interrupted`, not presented as a complete run. A failed capture is labelled `failed`.

## Output and sharing

- The collector writes **`asmond-m2-temperature-….tgz`** and matching **`.tgz.sha256`** to the Desktop by default. `--output-dir /existing/directory` selects another existing output directory.
- The corresponding screen recording is saved separately according to macOS recording settings. Keep a short description of the CPU/GPU tasks and any phase you could not perform.
- If an auxiliary reading clearly differs, note the sensor name and approximate time in the recording. No additional generic hardware dump is needed.

For a support comparison, agree with the maintainer on a private transfer method for the archive, checksum and recording. Review all three before sharing; keep raw support bundles and recordings out of public issues and Git.

The archive contains hardware model/OS/Python versions, phase events, raw temperature/fan keys, per-key start/end wall-clock and monotonic timestamps, named HID temperature readings, a host CPU-load average and checksums. Numeric zeros, fixed offsets, missing reads and raw `ioft` bytes remain distinguishable; `ioft` is not silently interpreted as a CPU/GPU temperature. The host CPU-load average is measured from Mach counters and is not per-core telemetry or evidence of GPU load. Reads are sequential, not simultaneous; an overrun reduces sample frequency rather than inventing extra samples. No process list, full settings, user/host name, serial, arbitrary SMC string or automatic screenshot is collected. Review the recording and files before sharing; do not publish private support bundles in Git.

This test can establish group response, refresh behavior and agreement with named MFC readings. It cannot guarantee individual physical-core numbering: a scheduler-managed workload does not prove which core a sensor belongs to. Exact labels may still require model-specific key metadata or a further narrowly targeted comparison. Ambiguous sensors stay unavailable, with no old-reading substitution or PMU-as-CPU fallback.
