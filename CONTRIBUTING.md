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

For resource comparisons across installed Python versions, use the separate
[development benchmark](docs/performance.md); keep mock and live results distinct.

## State and lifecycle boundaries

The existing runtime modules stay in place. Within `asmond.py`,
`DashboardSession` owns worker startup and shutdown, including partial startup
failure. `DashboardSampler` owns the main stream, generation, bounded queues,
retry deadlines, current sample and idle-frequency cache. `SideMetricsState`
owns side observations, their source ages and failures. The TUI owns focus,
selection and text editing; charge updates return a key for selection reconciliation.

Preserve these contracts when changing acquisition or the UI:

- Retire a stream generation before stopping it. Late data or EOF from that
  generation cannot affect its replacement; closing prevents further publication.
- Acquisition time determines freshness. Receiving a queued result, retrying or
  restarting never makes an old measurement fresh. Shared age rules live in
  `asmond_models.py`, including the recorder's main-stream freshness check.
- A missing/expired reading is unavailable; measured zero remains data. Keep
  gaps and sticky diagnostic reports when their current measurements expire.
- Failed process identity validation invalidates the rows and requests a worker
  refresh. In-flight process observations started before that invalidation cannot
  repopulate the list; the UI does not poll a replacement list itself.
- Telemetry handoffs stay bounded and nonblocking. Native cache admission is
  shared across stream generations; a blocked reader must not create more waiters.
- Cleanup must reach all owned workers even if startup or UI restoration fails.
  Register a worker before calling `start()`. A thread ID can appear before
  `join()` accepts the start; retain ownership until completion is confirmed.
  Stops and joins are attempted before the first cleanup error is re-raised;
  failed or timed-out ownership remains available for a subsequent stop.
  A replacement main reader waits until its retired pump thread has exited;
  repeated interval changes cannot create additional waiting reader threads.

Joins have bounded waits. A noninterruptible native cache call can finish later,
but shared admission permits at most one such cache worker and its retired
publication target cannot populate a replacement stream.

All test cases inherit `IsolatedTestCase` from `test_asmond.py`. Its shared guard
covers fixture setup, the test and cleanup; unexpected sensor, subprocess,
credential or signal calls fail even if application code catches their exception.
Test modules disable native reader initialization when importing the application.
Replace intended readers explicitly with fixtures, including asynchronous
sensor-cache refreshes.
Hardware-specific fixtures declare their modeled architecture rather than
inheriting it from the Mac running the tests.
The state/TUI harness also blocks its high-level reader entry points.

Explicit local integration cases may launch the test interpreter, known local
filesystem/shell tools and temporary `--version` launchers. Signals are limited
to their own unreaped children. Filesystem ACL checks still use libc; the one
native process-counter test is separately marked and measures only itself.
These are explicit, trusted local integration cases, not a subprocess sandbox.
Keep live hardware acceptance and sudo use separate from the synthetic suite.

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
