# Asmond

Asmond is a macOS power, thermal and activity monitor for Apple Silicon.

It reads Apple's `powermetrics` plist stream and combines it with macOS system counters for memory, swap, battery, disk, network and process data. The project is intentionally small: a compact Python runtime, one main test file, and no third-party Python package dependencies.

## Features

- SoC, CPU, GPU and ANE/NPU power where `powermetrics` exposes it
- Shared-scale split power graph with independently selectable upper and lower sources
- Current, 30 second average and peak values for power readings
- Thermal pressure, throttling state, SoC temperature average/max and battery temperature
- P-core/E-core usage rows, CPU/GPU average rows and an alternate CPU/GPU graph view
- P-core/E-core and GPU clocks, with live smoothing for idle `0 Hz` cluster samples
- RAM, swap and memory pressure using `vm_stat`, `vm.swapusage` and `memory_pressure`
- Legacy memory bandwidth counters in the RAM panel when `powermetrics` exposes `bandwidth_counters`
- Battery details: charge, state, health, capacity, cycle count, power and time remaining
- USB-C/MagSafe charge details from IOKit: active port, negotiated voltage/current/power and PD profiles when exposed
- Optional compact disk/network I/O graph with selectable read/write sources
- Optional process panel with PID, CPU%, RAM%, RSS, and GPU% when macOS exposes a usable counter
- Layout presets: `full`, `compact`, `focus`, plus an editable `custom` layout
- Alerts for throttling, high temperature, swap usage and battery drain
- Theme-colored ASCII logo on the waiting screen, help overlay and settings menu
- Local persistence for theme, interval, layout, custom slots, graph sources, process panel and alert thresholds
- `doctor` and anonymized `report` commands for troubleshooting

## Requirements

- macOS on Apple Silicon
- Python 3.10 or newer
- `sudo` access for `powermetrics`

`requirements.txt` is intentionally empty of package requirements.

## Installation

With Homebrew:

```bash
brew tap Fxxrz/asmond
brew install asmond
```

Run:

```bash
asmond
```

Asmond keeps the terminal UI unprivileged and starts only `powermetrics` with `sudo`.
Homebrew releases starting with 0.3.0 also install the manual page, available as `man asmond`.

## Usage

Run the dashboard:

```bash
python3 asmond.py
```

Run with generated demo data:

```bash
python3 asmond.py --mock
```

Show one parsed sample:

```bash
python3 asmond.py probe
```

Inspect raw flattened `powermetrics` keys and values:

```bash
python3 asmond.py probe --raw
```

`probe --raw` is not anonymized. Prefer `asmond report` for public bug reports.

Check local data sources without starting the TUI:

```bash
python3 asmond.py doctor
```

Create an anonymized support report:

```bash
python3 asmond.py report
python3 asmond.py report --live
```

Print the settings path:

```bash
python3 asmond.py --config-path
```

Useful options:

```bash
python3 asmond.py -i 0.2 --history 240 --theme nord
python3 asmond.py --theme dracula --layout compact --show-io
```

Run tests:

```bash
python3 -m unittest -v
```

Measure test coverage:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m coverage run -m unittest
python3 -m coverage report
```

Preview the manual page from a checkout:

```bash
man ./man/asmond.1
```

## Controls

```text
q       quit
? / h   toggle help overlay
m       toggle settings menu
t       cycle theme
T       tailor mode for numbered custom-layout slots
+/-     change and apply sample interval, down to 0.1s
r       reset power graph history and peaks
v       cycle layout preset: full, compact, focus, custom
d       show or hide Disk/Network I/O panel
i       cycle the upper Disk/Network graph source
o       cycle the lower Disk/Network graph source
L       toggle CPU/GPU average view
b       toggle full-layout charge panel between Battery and USB-C
S/C/G/A select SoC, CPU, GPU or ANE/NPU for the upper power graph
s/c/g/a select SoC, CPU, GPU or ANE/NPU for the lower power graph
u       cycle the upper power graph
n       cycle the lower power graph
p       cycle process panel: hidden, left, right
up/down select process
left/right change process sort: CPU, GPU, RAM, PID, name
k       mark selected process, press k again to send TERM
```

In the settings menu, use Up/Down or Tab to move, Left/Right or Enter to change a value, `s` to save and Esc to close.

When the `custom` layout is active, press `T` to enter Tailor mode. The editable slots are numbered directly on the dashboard. Press a number to open the small slot editor away from that panel, then use Up/Down, Left/Right and Enter to change the slot, panel, detail level or custom layout name. Panels without meaningful detail variants keep a single fixed detail value. `T` or Esc closes Tailor mode.

The `focus` layout is the combined power/thermal view. Older saved settings named `power-only` or `thermals-only` are mapped to `focus` automatically.

Process termination uses the current user's permissions. The full TUI refuses root launches by default; use normal `asmond` so only `powermetrics` receives elevated privileges. Root UI mode exists only as an explicit override with `--allow-root-ui`, and root process termination remains disabled unless enabled in the settings menu.

Settings are saved in:

```text
~/Library/Application Support/Asmond/settings.json
```

If Asmond is explicitly launched via `sudo` for a non-dashboard command, the settings path is resolved through `SUDO_USER` so the file still belongs to the real user.

Command-line help shows the currently saved defaults for options such as interval, theme and layout.

Remove saved settings:

```bash
asmond --remove-settings
```

Homebrew does not remove per-user settings automatically on uninstall. To remove everything:

```bash
asmond --remove-settings
brew uninstall asmond
```

## Themes

Available themes:

```text
classic matrix solar mono nord dracula ocean ember
```

## Data Accuracy

Asmond prefers exposed macOS counters over estimates. Some values are not publicly available on every system. ANE/NPU frequency is hidden because there is no reliable public counter, and ANE/NPU usage may fall back to a power-based proxy when active residency is unavailable. Media Engine values are shown only if the local `powermetrics` output contains usable fields.

Per-process GPU% is a legacy, best-effort path. `powermetrics --show-process-gpu` documents per-process GPU time, but Apple notes that it is only available on certain hardware. Current tested Apple Silicon/macOS builds either omit the counter from plist output or report only zeroes in text output; in that case Asmond hides the GPU% process column instead of showing misleading 0.0% values.

Memory bandwidth support is a legacy, best-effort path. Older macOS/Apple Silicon combinations exposed `bandwidth_counters` in the `powermetrics` plist stream, but this appears to be unavailable on current macOS releases and is not covered by the maintainer's current hardware tests. When the counters are present, values are grouped by visible names such as CPU, GPU, ANE, DRAM or DCS and displayed as GB/s; otherwise the bandwidth rows stay hidden.

USB-C and MagSafe charge information is decoded from the AppleSmartBattery and AppleHPMInterface IOKit trees. Live voltage, current and wattage prefer measured telemetry when available and fall back to negotiated adapter/PD values. Physical port selection follows AppleHPMInterface `ConnectionActive`, because some battery controller contract fields can stay stale briefly after unplugging or moving a charger. Cable capability is intentionally conservative: Asmond reports `unknown` unless the controller data is strong enough to infer a 3A/5A power path.

RAM labels are macOS-specific: `Used` is active plus wired memory, while `Phys` is physical occupancy (`total - free/speculative`). `Pressure` uses Apple's `memory_pressure` command when available and otherwise falls back to a reclaimable-memory estimate.

For bug reports, include `asmond doctor` and, if comfortable, `asmond report`. Use `asmond report --live` only when a live `powermetrics` snapshot is relevant.

## License

MIT
