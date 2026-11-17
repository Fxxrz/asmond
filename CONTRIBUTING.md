# Contributing to Asmond

Bug reports, focused fixes and documentation improvements are welcome. Asmond is
maintained as a small hobby project; review happens on a best-effort basis.
Please discuss larger features or architecture changes in an issue first.

## Reporting problems

Include the Asmond version, Mac model/chip, macOS version, reproduction steps and
expected behavior. For display problems, add the terminal app, font and window
size. `asmond doctor` and `asmond report` can help describe the environment.

Review any attachment before sharing it. Raw probes, IORegistry dumps and
recordings can contain private identifiers, paths or process information.
For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.

## Development and checks

Use native macOS with Python 3.10 or newer. The runtime uses only the Python
standard library and macOS interfaces/tools. From the checkout root, preview the
UI without sudo or live hardware sampling:

```sh
python3 asmond.py --mock
```

For code changes, run the unit tests:

```sh
python3 -m unittest -v
```

To run the development-only lint and coverage checks in an isolated environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check .
python3 -m coverage run -m unittest
python3 -m coverage report
```

Add focused regressions for changed behavior; do not lower the coverage gate.
Documentation-only changes need link and wording checks rather than new tests.
The [CI workflow](.github/workflows/tests.yml) defines the full checks and Python
matrix. Mock/fixture tests do not establish live hardware support: for sensor or
platform fixes, include the tested model, macOS version and source evidence, and
state what remains untested. See [Data Accuracy](README.md#data-accuracy).

## Project rules

- Keep changes small, readable and consistent with the existing module boundaries.
  Avoid unrelated refactoring or restructuring.
- Keep the runtime free of third-party packages. Ruff and Coverage are development
  tools only.
- Use actual macOS telemetry and verified sensor/port identities. Keep measured,
  calculated, negotiated and unavailable values distinct; do not invent readings
  or infer a sensor mapping from another model. Missing/stale data is not zero.
- Keep the UI and recorder unprivileged. Existing sudo use is limited to
  powermetrics sampling and stopping Asmond's own unreaped sampler when needed.
  Do not widen privilege boundaries or add background services implicitly.
- Preserve the compact terminal layout, keyboard navigation and graceful handling
  of unavailable capabilities. Update documentation when behavior changes.

## Pull requests and publication

Open a focused pull request against the current `main` and describe the problem,
change, checks and known limits. Use screenshots for visible UI changes and safe,
minimal fixtures for parser fixes. State skipped checks and why they were skipped.

Development history lives on [Forgejo](https://git.fxrz.de/fxrz/asmond).
GitHub receives independent parentless snapshots; GitHub pull requests are still
welcome, and maintainers transfer accepted changes to the development repository.
If a snapshot update changes your pull request's base, coordinate an updated
patch with the maintainer.

Keep the existing development version suffix. Release versions, tags, published
archives and Homebrew updates are handled separately by the maintainer.
