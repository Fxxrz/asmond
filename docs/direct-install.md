# Direct installation

Available starting with **0.9.0**, independently of Homebrew. The installer
requires the official versioned archive and its checksum; older releases without
these assets are refused without replacing anything.

## One-command entry point

Run the installer from the pinned release tag:

```sh
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  https://raw.githubusercontent.com/Fxxrz/asmond/v0.9.0/install.sh | /bin/bash
```

The public repository follows the existing release-only export workflow.
Download and inspect the script
before running it if preferred. There is no blanket `sudo bash` installation.

The shell guard checks macOS/native architecture before attempting any Python or
Asmond installation. Linux, WSL and Windows Bash environments are unsupported;
Windows PowerShell does not directly execute this Bash script. Rosetta terminal
sessions are rejected so the installer cannot pick the wrong native interpreter.

Existing suitable native Python is reused. If none is found, the script asks via
the terminal whether to download the pinned, signed Python.org 3.14.9 universal2
package. It verifies its checksum, PSF signer and macOS assessment, then requests
administrator authorization **only for that system-wide Python package**. `--yes`
does not bypass this separate approval. No source build, pip package, Git checkout,
additional Python dependency or package manager is required. Python.org releases
and requirements are documented at <https://docs.python.org/3/using/mac.html>.

## Directories, command and PATH

- Files: `~/.local/share/asmond/releases/<version>/`, including the complete runtime,
  installer, license, README, security notes and manual.
- Active release: the managed `current` symlink, switched only after validation.
- Command: `~/.local/bin/asmond`, a quoted shell launcher bound to the selected Python.
- Preferences: the existing Asmond settings location, never copied or replaced.

An interactive first run offers a different install directory and confirms the
plan. `--prefix DIR`, `--bin-dir DIR` and `--python /absolute/path` select explicit
locations. A dedicated directory is required; arbitrary nonempty directories,
Homebrew Cellar trees and existing foreign commands are refused. Homebrew is not
installed or emulated. A different existing `asmond` on PATH is reported; the
user must migrate it deliberately or explicitly request a separate installation
with `--allow-shadow`. The installer never runs `brew uninstall` itself.

If the command directory is missing from PATH, adding it to `.zprofile` or
`.bash_profile` is offered separately (`--add-path` for explicit noninteractive
approval). Symlinked/unowned profiles are not edited. A new login terminal may be
needed; the full launcher path works immediately. Custom shell configuration is
left to its owner. `--yes` alone does not modify shell profiles.

The manual can be read with `man ~/.local/share/asmond/current/man/asmond.1`,
adjusting that path for a custom prefix. No global MANPATH is changed.

## Updates and rollback

```sh
asmond --update
```

Only a managed direct installation delegates to its local installer. Homebrew
and source checkouts receive an explanatory message and are never self-modified.
Updates require confirmation, use published non-prerelease GitHub release assets,
validate the checksum and exact allowlisted contents, and test `--version` plus an
unprivileged mock report with temporary settings. No live telemetry or sudo probe
is started. Incomplete, mismatched and failed candidates do not change the active
version. Same-version replacement and implicit downgrades are refused.
After rollback, an update can reactivate a retained release only if its owned
files and metadata exactly match the freshly verified archive; modified retained
files are neither overwritten nor activated.

The launcher resolves a concrete version directory on each start; an update
does not rewrite files underneath a running instance. Existing versions are
retained. Explicit rollback, for the default prefix:

```sh
bash ~/.local/share/asmond/current/install.sh --rollback
```

Supply `--prefix /custom/location` for a custom installation. Rollback checks the
previous version's complete file set, ownership and saved per-file SHA-256 values
before executing any candidate smoke test or activating it. Missing integrity
metadata or modified files cause refusal; nothing is repaired or overwritten.
These local records are not a security boundary against an attacker controlling
the same user account. Rollback does not revert
user settings or update Python. Changing the chosen interpreter/bin directory
is not silently performed during an update.
The selected Python must remain available. Stable executable symlinks are kept
as selected rather than rewritten to a version-specific Homebrew Cellar path;
this avoids unnecessarily breaking the launcher during Homebrew cleanup.

Piped execution does not save a bootstrap script. Deliberately saved script
copies are not self-deleted. Owned temporary downloads are cleaned up on normal
exit, Ctrl-C and termination; a hard kill or power loss may leave temporary files
or `.install.lock`. Inspect those before removing them. No user directories are
recursively deleted and there is no automatic old-version cleanup.

## Local preparation and release assets

```sh
bash install.sh --help
python3 tools/build-release-archive.py --output /path/to/new-assets-directory --allow-dev
```

`--allow-dev` is **only for isolated local testing**. It does not tag, publish,
upload or install anything. Install a locally prepared archive with explicit
`--archive /path/to/asmond-VERSION.tar.gz --sha256 DIGEST --version VERSION`,
preferably with separate temporary `--prefix` and `--bin-dir` paths. This is not a
development update channel and does not permit overwriting an existing version.

When preparing a later approved public release:

1. Finalize the actual release version, remove its active `-dev` labels from
   runtime/manual/README, and update version-specific documentation. Run the normal
   test/review/export procedure; Intel's experimental support status is unchanged.
2. Run `python3 tools/build-release-archive.py --output /new/assets/directory`
   **without** `--allow-dev`. The default gate rejects development builds or a
   remaining active README development label. The output is reproducible and
   contains only the installer's explicit runtime/documentation whitelist, no Git
   history. Missing required modules and extra `asmond_*.py` scratch files stop
   the build before assets are written.
3. Upload both `asmond-X.Y.Z.tar.gz` and `asmond-X.Y.Z.tar.gz.sha256` as assets of
   the matching published `vX.Y.Z` GitHub release. Generated GitHub source tarballs
   are not substituted for these assets. Keep the existing parentless public
   export and Homebrew source-archive/hash procedure; the two archive types differ.
4. Verify the public installer/update path on native Intel and Apple Silicon as
   available. Local temporary installs and mocked network tests do not establish
   successful Python package installation on another physical machine.

The checksum pair relies on the same trusted publisher/HTTPS channel, not an
independent Asmond signing infrastructure. Python itself remains a separate
system prerequisite with its own security-update lifecycle.
