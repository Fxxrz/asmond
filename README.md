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
- Battery details: charge, state, health, capacity, cycle count, power and time remaining
- Optional compact disk/network I/O graph with selectable read/write sources
- Optional process panel with CPU/RAM sorting, selection and confirmed TERM action
- Layout presets: `full`, `compact`, `power-only`, `thermals-only`
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
sudo asmond
```

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
?       toggle help overlay
m       toggle settings menu
t       cycle theme
+/-     change and apply sample interval, down to 0.1s
r       reset power graph history and peaks
v       cycle layout preset
d       show or hide Disk/Network I/O panel
i       cycle the upper Disk/Network graph source
o       cycle the lower Disk/Network graph source
L       toggle CPU/GPU average view
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

Settings are saved in `.asmond.json` next to `asmond.py`.

## Themes

Available themes:

```text
classic matrix solar mono nord dracula ocean ember
```

## Notes

Asmond prefers exposed macOS counters over estimates. Some values are not publicly available on every system. ANE/NPU frequency is hidden because there is no reliable public counter, and ANE/NPU usage may fall back to a power-based proxy when active residency is unavailable. Media Engine values are shown only if the local `powermetrics` output contains usable fields.

RAM labels are macOS-specific: `Used` is active plus wired memory, while `Phys` is physical occupancy (`total - free/speculative`). `Pressure` uses Apple's `memory_pressure` command when available and otherwise falls back to a reclaimable-memory estimate.

## License

MIT
