# Direct installation

Available starting with **0.9.0**, independently of Homebrew. The installer
requires the official versioned archive and its checksum; older releases without
these assets are refused without replacing anything. Use **0.9.1 or later** for
interactive installation and updates; 0.9.0's terminal confirmation is broken.

## One-command entry point

Run the installer from the pinned release tag:

```sh
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  https://raw.githubusercontent.com/Fxxrz/asmond/v1.2.0/install.sh | /bin/bash
```

Download and inspect the script before running it if preferred. There is no
blanket `sudo bash` installation.

The shell guard checks macOS/native architecture before attempting any Python or
Asmond installation. Linux, WSL and Windows Bash environments are unsupported;
Windows PowerShell does not directly execute this Bash script. Rosetta terminal
sessions are rejected so the installer cannot pick the wrong native interpreter.

Existing suitable native Python is reused. If none is found, the script asks via
the terminal whether to download the pinned, signed Python.org 3.14.10 universal2
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
locations. Use a dedicated installation directory and a command directory that
other users cannot replace or write to. The installer rejects unsafe directory
ownership, POSIX permissions and macOS ACL entries that grant write or delete
rights to other users or groups. It also rejects nonempty unmanaged
destinations, Homebrew Cellar trees and existing foreign commands. Homebrew is not
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
Updates require confirmation, use the installation's recorded channel (Stable by default),
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

## Stable and Nightly channels

Nightly channel support is included starting with **1.2.0**. A first Nightly
release and its Homebrew formula are published separately from Stable. A published Nightly prerelease with its archive
and checksum is required before these Nightly install commands can succeed.
The installer reports an unavailable channel without changing an installation;
it does not substitute Stable or download an unverified branch archive.

With an installer containing this support, choose the channel explicitly:

```sh
bash install.sh --channel nightly
```

New installations default to Stable. Nightly defaults to
`~/.local/share/asmond-nightly` and the command `~/.local/bin/asmond-nightly`.
Its launcher uses `~/Library/Application Support/Asmond Nightly/settings.json`;
an explicitly set `ASMOND_SETTINGS_DIR` takes precedence. Stable's paths,
command and settings remain independent. Custom prefixes/bin directories keep
the same ownership, permissions and command-conflict checks as Stable.

Use `asmond-nightly` to try Nightly and `asmond` to return to Stable. Install
Stable separately with `bash install.sh --channel stable` if needed. Each
managed prefix has one fixed channel; an explicit attempt to change it in place
is refused. This permits side-by-side comparison without cross-channel
replacement, implicit downgrades or configuration migration.

```sh
asmond-nightly --update
bash ~/.local/share/asmond-nightly/current/install.sh --channel nightly --rollback
```

For a custom Nightly installation, also supply `--prefix /custom/location`.
The invoked script's location does not select the installation directory;
`--channel nightly` selects the Nightly default rather than the Stable prefix.

Updates retain that prefix's recorded channel. Old Stable metadata without a
channel field remains Stable. Nightly discovery uses published GitHub
prereleases whose exact version is `X.Y.Z-dev.YYYYMMDDHHMMSS.gREVISION`:
the UTC build timestamp must be valid and REVISION has twelve lowercase commit
digits. CLI version output and reports retain the complete identity; a narrow
dashboard header shortens it to `nightly` to keep Theme/Layout/controls visible. Build order uses the base version and timestamp, not GitHub list order
or a guessed commit order. Ambiguous timestamps are refused. Discovery is
limited to ten pages of 100 releases; an exact `--version` can select a known
Nightly without scanning that history. Rate limits, missing releases and
malformed metadata leave the active installation unchanged.

Stable still accepts only published non-prereleases. Nightly has the same
HTTPS, exact asset URL/name, checksum, archive allowlist/size, smoke-test,
atomic-activation and verified-rollback checks. An ordinary `-dev` workspace
label is not a downloadable Nightly identity. Neither channel changes the
Python requirement or adds a package dependency.

Homebrew's future `asmond-nightly` formula uses the same immutable Nightly
package and exact SHA-256, with its own command, `asmond-nightly.1` manual and
Nightly settings directory. Once the first Nightly and its formula are
published:

```sh
brew install Fxxrz/asmond/asmond-nightly
brew upgrade asmond-nightly
man asmond-nightly
```

Homebrew installations remain managed by Homebrew; `asmond-nightly --update`
provides the matching Brew instruction instead of modifying the Cellar.

## For maintainers: local package testing

The sections above cover installing and using published releases. The following
commands are for maintainers working from a complete Asmond source tree with
native Python 3.10+; run them from the repository root.

```sh
bash install.sh --help
python3 tools/build-release-archive.py --output /path/to/new-assets-directory --allow-dev
```

`--allow-dev` is **only for isolated local testing**. It does not tag, publish,
upload or install anything. Install a locally prepared archive with explicit
`--archive /path/to/asmond-VERSION.tar.gz --sha256 DIGEST --version VERSION`,
preferably with separate temporary `--prefix` and `--bin-dir` paths. This is not a
development update channel and does not permit overwriting an existing version.

## For maintainers: published release assets

After selecting and validating a stable release version:

1. Finalize the actual release version, remove its active `-dev` labels from
   runtime/manual/README, and update version-specific documentation. Run the
   repository's test and lint checks and review the release contents. Keep the
   documented Intel capability/validation scope accurate; packaging does not
   expand hardware validation.
2. Run `python3 tools/build-release-archive.py --output /new/assets/directory`
   **without** `--allow-dev`. The default gate rejects development builds or a
   remaining active README development label. The output is reproducible and
   contains only the installer's explicit runtime/documentation whitelist, no Git
   history. Missing required modules and extra `asmond_*.py` scratch files stop
   the build before assets are written.
3. Upload both `asmond-X.Y.Z.tar.gz` and `asmond-X.Y.Z.tar.gz.sha256` as assets of
   the matching published `vX.Y.Z` GitHub release. Generated GitHub source tarballs
   are not substituted for these assets. Homebrew uses the separate GitHub source
   archive; its formula URL and SHA-256 must match that archive. The direct
   installer's package and the source archive serve different installation paths.
4. Verify the public installer/update path on native Intel and Apple Silicon as
   available. Local temporary installs and mocked network tests do not establish
   successful Python package installation on another physical machine.

The checksum pair relies on the same trusted publisher/HTTPS channel, not an
independent Asmond signing infrastructure. Python itself remains a separate
system prerequisite with its own security-update lifecycle.

## For maintainers: Nightly packages and tap formula

Start from a reviewed, clean development snapshot with its ordinary
`X.Y.Z-dev` label. Record its UTC build timestamp and source revision. For
example, from that checkout, with a new output directory:

```sh
nightly_id="$(date -u +%Y%m%d%H%M%S).g$(git rev-parse --short=12 HEAD)"
python3 tools/build-release-archive.py --output /new/nightly-assets \
  --nightly "$nightly_id" --formula-output /new/nightly-assets/asmond-nightly.rb
```

This writes a reproducible versioned archive, exact checksum sidecar and pinned
Homebrew formula. Only packaged runtime/manual/README labels receive the full
Nightly identity; the source checkout retains its development label. The
explicit Nightly mode cannot be combined with local-only `--allow-dev`.
Existing artifacts/formulas are never overwritten. The builder does not tag,
upload, publish or schedule anything.

After validating that package, publish the versioned source/tag with matching
Nightly labels and a non-draft GitHub prerelease named `vVERSION`.
Upload both exact package/checksum assets to that prerelease. Verify the public
bytes before committing the generated `asmond-nightly.rb` to the tap's
`Formula` directory; the Stable formula stays unchanged. The generated formula
uses the direct package asset, rather than Stable's separate GitHub source
archive. Never substitute a placeholder URL, unverified checksum or mutable
`HEAD` checkout. Publication and hardware qualification remain separate steps.
