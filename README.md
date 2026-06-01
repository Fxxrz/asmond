# Asmond

Asmond is a macOS power, thermal and activity monitor for Apple Silicon.

It reads Apple's `powermetrics` plist stream and combines it with macOS system counters for memory, swap, battery, disk, network and process data. The project is intentionally small: one Python runtime file, one test file, and no third-party Python package dependencies.

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
- Optional process panel with CPU/RAM sorting, selection and confirmed TERM action
- Layout presets: `full`, `compact`, `focus`
- Alerts for throttling, high temperature, swap usage and battery drain
- Theme-colored ASCII logo on the waiting screen, help overlay and settings menu
- Local persistence for theme, interval, layout, graph sources, process panel and alert thresholds

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

Inspect raw flattened `powermetrics` keys:

```bash
python3 asmond.py probe --raw
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

## Controls

```text
q       quit
? / h   toggle help overlay
m       toggle settings menu
t       cycle theme
+/-     change and apply sample interval, down to 0.1s
r       reset power graph history and peaks
v       cycle layout preset: full, compact, focus
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
left/right change process sort
k       mark selected process, press k again to send TERM
```

In the settings menu, use Up/Down or Tab to move, Left/Right or Enter to change a value, `s` to save and Esc to close.

The `focus` layout is the combined power/thermal view. Older saved settings named `power-only` or `thermals-only` are mapped to `focus` automatically.

Process termination uses the current user's permissions. The full TUI refuses root launches by default; use normal `asmond` so only `powermetrics` receives elevated privileges. Root UI mode exists only as an explicit override with `--allow-root-ui`, and root process termination remains disabled unless enabled in the settings menu.

Settings are saved in:

```text
~/Library/Application Support/Asmond/settings.json
```

If Asmond is explicitly launched via `sudo` for a non-dashboard command, the settings path is resolved through `SUDO_USER` so the file still belongs to the real user.

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

## Notes

Asmond prefers exposed macOS counters over estimates. Some values are not publicly available on every system. ANE/NPU frequency is hidden because there is no reliable public counter, and ANE/NPU usage may fall back to a power-based proxy when active residency is unavailable. Media Engine values are shown only if the local `powermetrics` output contains usable fields.

Memory bandwidth support is a legacy, best-effort path. Older macOS/Apple Silicon combinations exposed `bandwidth_counters` in the `powermetrics` plist stream, but this appears to be unavailable on current macOS releases and is not covered by the maintainer's current hardware tests. When the counters are present, values are grouped by visible names such as CPU, GPU, ANE, DRAM or DCS and displayed as GB/s; otherwise the bandwidth rows stay hidden.

USB-C and MagSafe charge information is decoded from the AppleSmartBattery and AppleHPMInterface IOKit trees. Live voltage, current and wattage prefer measured telemetry when available and fall back to negotiated adapter/PD values. Physical port selection follows AppleHPMInterface `ConnectionActive`, because some battery controller contract fields can stay stale briefly after unplugging or moving a charger. Cable capability is intentionally conservative: Asmond reports `unknown` unless the controller data is strong enough to infer a 3A/5A power path.

RAM labels are macOS-specific: `Used` is active plus wired memory, while `Phys` is physical occupancy (`total - free/speculative`). `Pressure` uses Apple's `memory_pressure` command when available and otherwise falls back to a reclaimable-memory estimate.

## License

MIT
