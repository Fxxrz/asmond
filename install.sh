#!/bin/bash
# macOS direct installer. The Python installer below uses only the stdlib.
# Saved copies are retained; only our own temporary downloads are cleaned up.
set -euo pipefail

fail() { printf 'Asmond installer: %s\n' "$*" >&2; exit 1; }
[[ "$(/usr/bin/uname -s)" == Darwin ]] || fail 'This system is not supported. Asmond requires macOS.'
case "$(/usr/bin/uname -m)" in arm64|x86_64) ;; *) fail 'Unsupported Mac architecture.' ;; esac
[[ "$(/usr/bin/id -u)" != 0 ]] || fail 'Run as your normal user, not with sudo.'
[[ "$(/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true)" != 1 ]] || fail 'Open a native terminal, not a Rosetta terminal.'

requested_python=''
previous=''
for argument in "$@"; do
    if [[ "$previous" == --python ]]; then requested_python="$argument"; fi
    case "$argument" in
        --python=*) requested_python="${argument#--python=}" ;;
        --help|-h)
            printf '%s\n' 'Asmond direct installer (macOS only)' \
                'Options: --prefix DIR --bin-dir DIR --python FILE --version X.Y.Z' \
                '         --yes --add-path --allow-shadow --update --rollback' \
                'Local testing: --archive FILE --sha256 HASH --version X.Y.Z[-dev]' \
                'No root install, source builds or automatic Homebrew migration.'
            exit 0 ;;
    esac
    previous="$argument"
done

usable_python() {
    # /usr/bin/python3 can be a CLT stub that otherwise opens an installation UI.
    if [[ "$1" == /usr/bin/python3 ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then return 1; fi
    [[ -x "$1" ]] && "$1" -I -c 'import ctypes,curses,platform,sys; assert sys.version_info >= (3,10) and sys.maxsize > 2**32; assert platform.system()=="Darwin"; assert platform.machine()==sys.argv[1]' "$(/usr/bin/uname -m)" >/dev/null 2>&1
}

python_bin=''
if [[ -n "$requested_python" ]]; then
    usable_python "$requested_python" || fail 'The selected Python must be native, 64-bit, version 3.10+, with ctypes and curses.'
    python_bin="$requested_python"
else
    for candidate in "$(command -v python3 2>/dev/null || true)" \
        /opt/homebrew/opt/python@3.14/bin/python3.14 /usr/local/opt/python@3.14/bin/python3.14 \
        /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        /usr/bin/python3; do
        if usable_python "$candidate"; then python_bin="$candidate"; break; fi
    done
fi

bootstrap_temp=''
cleanup() { if [[ -n "$bootstrap_temp" ]]; then /bin/rm -f "$bootstrap_temp/python.pkg"; /bin/rmdir "$bootstrap_temp" 2>/dev/null || true; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ -z "$python_bin" ]]; then
    # This opt-in fallback never builds Python. Pin changes require reviewing the
    # official python.org release metadata and its macOS installer digest.
    printf '%s\n' 'No suitable Python was found. Download the signed Python.org 3.14.9 installer?' \
        'This installs Python system-wide and macOS will request administrator approval.'
    answer=''
    if ! { printf 'Continue? [y/N] ' >/dev/tty; read -r answer </dev/tty; } 2>/dev/null; then
        fail 'Install Python 3.10+ first, or provide --python /absolute/path/to/python3.'
    fi
    case "$answer" in y|Y|yes|YES) ;; *) fail 'Cancelled; Python was not installed.' ;; esac
    bootstrap_temp="$(/usr/bin/mktemp -d /tmp/asmond-python.XXXXXX)"
    /usr/bin/curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 300 --max-filesize 150000000 \
        --output "$bootstrap_temp/python.pkg" 'https://www.python.org/ftp/python/3.14.9/python-3.14.9-macos11.pkg'
    actual="$(/usr/bin/shasum -a 256 "$bootstrap_temp/python.pkg")"
    [[ "${actual%% *}" == 08e312f20844b524c2f8c5e5b2e7f49608c22e8c7cb194f5834d385c94383462 ]] || fail 'Python package checksum mismatch.'
    signature="$(/usr/sbin/pkgutil --check-signature "$bootstrap_temp/python.pkg")" || fail 'Invalid Python package signature.'
    [[ "$signature" == *'Developer ID Installer: Python Software Foundation'* ]] || fail 'Unexpected Python package signer.'
    /usr/sbin/spctl --assess --type install "$bootstrap_temp/python.pkg" || fail 'macOS did not accept the Python package.'
    /usr/bin/sudo /usr/sbin/installer -pkg "$bootstrap_temp/python.pkg" -target /
    python_bin=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
    usable_python "$python_bin" || fail 'Python installation did not complete. Rerun this installer after installing Python.'
fi

"$python_bin" -I - "$@" <<'ASMOND_INSTALLER_PY'
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

REPOSITORY = "Fxxrz/asmond"
MANAGER = "asmond-direct-v1"
RUNTIME_FILES = (
    "asmond.py", "asmond_cpu.py", "asmond_formatting.py", "asmond_layout.py",
    "asmond_metrics_memory.py", "asmond_models.py", "asmond_ports.py",
    "asmond_powermetrics.py", "asmond_process.py", "asmond_record.py", "asmond_settings.py",
    "asmond_smc.py", "asmond_system.py",
)
DOCUMENT_FILES = ("install.sh", "LICENSE", "README.md", "SECURITY.md", "man/asmond.1", "docs/direct-install.md", "docs/recording.md")
PACKAGE_FILES = RUNTIME_FILES + DOCUMENT_FILES
VERSION_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev)?"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


class InstallError(Exception):
    pass


def version_tuple(value, *, development=False):
    if not isinstance(value, str) or len(value) > 64:
        raise InstallError("Invalid version.")
    match = re.fullmatch(VERSION_PATTERN, value)
    if not match or (match[4] and not development):
        raise InstallError("Only stable release versions are accepted from GitHub.")
    return tuple(int(match[i]) for i in (1, 2, 3)) + (0 if match[4] else 1,)


def clean_path(value):
    path = Path(value).expanduser().absolute()
    if any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
        raise InstallError("Paths must not contain control characters.")
    return path


def owned_regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise InstallError(f"Not a private, owned regular file: {path}")


def read_json(path):
    owned_regular(path)
    if path.stat().st_size > 16384:
        raise InstallError("Installation metadata is too large.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallError("Invalid installation metadata.")
    return value


def atomic_file(path, content, mode=0o600):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallError(f"Refusing non-regular destination: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".asmond-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def curl_download(url, destination, limit):
    if not url.startswith("https://"):
        raise InstallError("Downloads must use HTTPS.")
    result = subprocess.run([
        "/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
        "--max-time", "120", "--max-filesize", str(limit), "--output", str(destination), url,
    ], check=False, timeout=130)
    if result.returncode or not destination.is_file() or destination.stat().st_size > limit:
        raise InstallError("Download failed or exceeded its size limit; the active version was not changed.")


def release_download(version, temporary):
    if version:
        version_tuple(version)
    endpoint = f"https://api.github.com/repos/{REPOSITORY}/releases/" + (f"tags/v{version}" if version else "latest")
    metadata = temporary / "release.json"
    curl_download(endpoint, metadata, 2 * 1024 * 1024)
    release = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise InstallError("GitHub did not return a published stable release.")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise InstallError("Invalid release tag.")
    actual_version = tag[1:]
    version_tuple(actual_version)
    if version and version != actual_version:
        raise InstallError("Release tag mismatch.")
    name = f"asmond-{actual_version}.tar.gz"
    base = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise InstallError("Release assets are missing.")
    for asset_name in (name, name + ".sha256"):
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == asset_name]
        if len(matches) != 1 or matches[0].get("browser_download_url") != base + asset_name:
            raise InstallError("This release has no verified direct-install assets. It may predate the direct installer; nothing was installed.")
    checksum = temporary / "archive.sha256"
    curl_download(base + name + ".sha256", checksum, 1024)
    text = checksum.read_text(encoding="ascii").strip()
    match = re.fullmatch(r"([a-fA-F0-9]{64})  " + re.escape(name), text)
    if not match:
        raise InstallError("Malformed release checksum.")
    archive = temporary / name
    curl_download(base + name, archive, MAX_ARCHIVE_BYTES)
    return actual_version, archive, match[1].lower()


def extract_package(archive, checksum, version, destination):
    if not re.fullmatch(r"[a-fA-F0-9]{64}", checksum or ""):
        raise InstallError("A SHA-256 digest is required.")
    with archive.open("rb") as handle:
        packed = handle.read(MAX_ARCHIVE_BYTES + 1)
    if len(packed) > MAX_ARCHIVE_BYTES:
        raise InstallError("Archive exceeds its size limit.")
    if hashlib.sha256(packed).hexdigest() != checksum.lower():
        raise InstallError("Asmond archive checksum mismatch.")
    expected_root = f"asmond-{version}"
    contents = {}
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(packed), mode="rb") as compressed:
        unpacked = compressed.read(MAX_ARCHIVE_BYTES + 1)
    if len(unpacked) > MAX_ARCHIVE_BYTES:
        raise InstallError("Unpacked archive exceeds its size limit.")
    with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as handle:
        for member in handle:
            total += member.size
            parts = PurePosixPath(member.name).parts
            if (total > MAX_ARCHIVE_BYTES or not member.isfile() or len(contents) >= len(PACKAGE_FILES)
                    or not parts or parts[0] != expected_root
                    or member.name != str(PurePosixPath(member.name)) or ".." in parts):
                raise InstallError("Unexpected archive structure, link or size.")
            relative = "/".join(parts[1:])
            if relative not in PACKAGE_FILES or relative in contents:
                raise InstallError("Unexpected or duplicate archive member.")
            stream = handle.extractfile(member)
            if stream is None:
                raise InstallError("Unreadable archive member.")
            contents[relative] = stream.read()
    if set(contents) != set(PACKAGE_FILES):
        raise InstallError("The archive is incomplete.")
    # No extractall: neither links, traversal nor archive permissions are trusted.
    for name, data in contents.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if name in ("asmond.py", "install.sh") else 0o644)


def smoke_test(python, release, version):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    with tempfile.TemporaryDirectory(prefix="asmond-smoke-") as config:
        env["ASMOND_SETTINGS_DIR"] = config
        for arguments in (("--version",), ("report", "--mock", "--json")):
            result = subprocess.run([str(python), "-B", "-E", "-s", str(release / "asmond.py"), *arguments],
                                    cwd=release, env=env, capture_output=True, text=True, timeout=20, check=False)
            if result.returncode:
                raise InstallError("Candidate failed its unprivileged smoke test; the active version was not changed.")
            if arguments == ("--version",):
                if result.stdout.strip() != f"Asmond {version}":
                    raise InstallError("Runtime and archive versions do not match.")
            else:
                report = json.loads(result.stdout)
                if not isinstance(report, dict) or report.get("version") != version:
                    raise InstallError("Candidate report version mismatch.")
    manual = (release / "man/asmond.1").read_text(encoding="utf-8").splitlines()
    if not manual or f'"Asmond {version}"' not in manual[0]:
        raise InstallError("Manual and runtime versions do not match.")


def launcher_bytes(prefix, python):
    return ("#!/bin/sh\n# Managed by Asmond direct installer; not Homebrew.\n"
            f"release=$(CDPATH= cd -P {shlex.quote(str(prefix / 'current'))} && pwd) || exit 1\n"
            f"exec {shlex.quote(str(python))} -B -E -s \"$release/asmond.py\" \"$@\"\n").encode()


def owned_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise InstallError("The installation directory must be owned, non-symlink and not group/world writable.")


def read_installation(prefix):
    owned_directory(prefix)
    config = read_json(prefix / ".asmond-install.json")
    if config.get("manager") != MANAGER or config.get("prefix") != str(prefix):
        raise InstallError("Not an Asmond direct installation; Homebrew and source checkouts must use their own updater.")
    for field in ("bin_dir", "python"):
        if not isinstance(config.get(field), str) or str(clean_path(config[field])) != config[field]:
            raise InstallError("Invalid installation paths.")
    releases = prefix / "releases"
    owned_directory(releases)
    active = prefix / "current"
    version = None
    if os.path.lexists(active):
        if not active.is_symlink():
            raise InstallError("The active version must be a managed symlink.")
        target = os.readlink(active)
        if not target.startswith("releases/"):
            raise InstallError("The active version points outside the installation.")
        version = target.removeprefix("releases/")
        version_tuple(version, development=True)
        folder = releases / version
        owned_directory(folder)
        metadata = read_json(folder / ".asmond-release.json")
        if metadata.get("version") != version or metadata.get("manager") != MANAGER:
            raise InstallError("Invalid release ownership metadata.")
    return config, version


def verify_retained(folder, candidate, version, checksum):
    owned_directory(folder)
    metadata = read_json(folder / ".asmond-release.json")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum) or metadata.get("manager") != MANAGER or metadata.get("version") != version or metadata.get("sha256") != checksum.lower():
        raise InstallError("An existing version directory has different ownership/content and will not be overwritten.")
    hashes = metadata.get("files")
    if not isinstance(hashes, dict) or set(hashes) != set(PACKAGE_FILES) or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values()):
        raise InstallError("The retained release has no complete integrity record.")
    allowed = set(PACKAGE_FILES) | {".asmond-release.json"}
    for path in folder.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            owned_directory(path)
        else:
            owned_regular(path)
            if path.relative_to(folder).as_posix() not in allowed:
                raise InstallError("Unexpected file in the retained version; nothing was overwritten.")
    for name in PACKAGE_FILES:
        owned_regular(folder / name)
        content = (folder / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != hashes[name] or (candidate is not None and content != (candidate / name).read_bytes()):
            raise InstallError("A retained release was modified; it will not be reactivated or overwritten.")


def activate(prefix, version):
    temporary = prefix / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(f"releases/{version}")
        os.replace(temporary, prefix / "current")
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def ask(text, default=""):
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as terminal:
            terminal.write(text)
            terminal.flush()
            return terminal.readline().strip() or default
    except OSError as exc:
        raise InstallError("No interactive terminal. Supply --yes and explicit options for unattended installs.") from exc


def confirm(text):
    return ask(text + " [y/N] ").casefold() in ("y", "yes")


def add_path(bin_dir):
    shell = Path(os.environ.get("SHELL", "")).name
    name = {"zsh": ".zprofile", "bash": ".bash_profile"}.get(shell)
    line = f"export PATH={shlex.quote(str(bin_dir))}:\"$PATH\""
    if name is None:
        print("Add this to your shell profile yourself:\n" + line)
        return
    profile = Path.home() / name
    if profile.exists() or profile.is_symlink():
        owned_regular(profile)
        text = profile.read_text(encoding="utf-8")
        mode = stat.S_IMODE(profile.stat().st_mode)
    else:
        text, mode = "", 0o600
    if line not in text.splitlines():
        atomic_file(profile, (text.rstrip("\n") + "\n\n# Asmond direct installer\n" + line + "\n").encode(), mode)
    print("PATH configured. Open a new login terminal to use the short asmond command.")


def configure_path(bin_dir, args):
    on_path = str(bin_dir) in [str(clean_path(entry).resolve()) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    if on_path:
        return
    if args.add_path or (not args.yes and confirm("Add the command directory to your login-shell PATH?")):
        try:
            add_path(bin_dir)
        except (OSError, InstallError, UnicodeError) as exc:
            print(f"Asmond is installed, but the shell profile was not changed: {exc}\nUse its full command path or configure PATH yourself.")
    else:
        print("The command directory is not on PATH. Add it yourself, or rerun with --add-path; existing shell files were not changed.")


@contextlib.contextmanager
def install_lock(prefix):
    lock = prefix / ".install.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise InstallError("Another installer may be running. An interrupted hard kill can leave .install.lock; inspect it before removing it.") from exc
    try:
        yield
    finally:
        lock.rmdir()


def install(args):
    if platform.system() != "Darwin" or platform.machine() not in ("arm64", "x86_64"):
        raise InstallError("This system is not supported. Asmond requires a native Intel or Apple Silicon Mac.")
    if os.geteuid() == 0:
        raise InstallError("Run as your normal user, not with sudo.")
    if args.update and args.rollback:
        raise InstallError("Choose update or rollback, not both.")
    if args.archive and (not args.sha256 or not args.version):
        raise InstallError("Local archives require --sha256 and --version.")
    if args.sha256 and not args.archive:
        raise InstallError("--sha256 requires --archive.")
    if args.rollback and (args.archive or args.version):
        raise InstallError("Rollback uses the previous installed version, not a download.")
    if not args.prefix and not args.yes:
        args.prefix = ask("Install directory [~/.local/share/asmond]: ", "~/.local/share/asmond")
    prefix = clean_path(args.prefix or "~/.local/share/asmond")
    if prefix.is_symlink():
        raise InstallError("The installation directory must not be a symlink.")
    prefix = prefix.parent.resolve() / prefix.name
    if prefix in (Path.home().resolve(), Path("/")) or "Cellar" in prefix.parts or prefix.name in ("bin", "lib", "share", "releases"):
        raise InstallError("Choose a dedicated Asmond directory, never a Homebrew Cellar, home or shared directory.")
    existing = (prefix / ".asmond-install.json").exists()
    if existing:
        config, current = read_installation(prefix)
        python = clean_path(config["python"])
        bin_dir = clean_path(config["bin_dir"])
        if args.bin_dir and clean_path(args.bin_dir).resolve() != bin_dir:
            raise InstallError("An update cannot change the managed bin directory.")
        if args.python and clean_path(args.python).resolve() != python.resolve():
            raise InstallError("An update cannot silently change its Python interpreter.")
    else:
        if args.update or args.rollback:
            raise InstallError("This is not a direct installation. Use brew upgrade asmond for Homebrew, or update your source checkout.")
        if prefix.exists() and (not prefix.is_dir() or any(prefix.iterdir())):
            raise InstallError("Refusing a nonempty, unmanaged installation directory.")
        # Keep a stable selected symlink (for example Homebrew opt/bin), rather
        # than pinning its current Cellar target which brew cleanup may remove.
        python = clean_path(args.python or sys.executable)
        bin_dir = clean_path(args.bin_dir or "~/.local/bin").resolve()
        config = {"manager": MANAGER, "prefix": str(prefix), "python": str(python), "bin_dir": str(bin_dir)}
        current = None
    if "Cellar" in bin_dir.parts or any(bin_dir == folder or folder in bin_dir.parents for folder in (prefix / "releases", prefix / "current")):
        raise InstallError("The command directory must be outside Homebrew Cellar and managed release directories.")
    command = bin_dir / "asmond"
    expected_launcher = launcher_bytes(prefix, python)
    if os.path.lexists(command):
        owned_regular(command)
        if not existing or command.read_bytes() != expected_launcher:
            raise InstallError("An existing asmond command is not owned by this installer; nothing will be overwritten.")
    found = shutil.which("asmond")
    if not existing and found and Path(found).absolute() != command and not args.allow_shadow:
        raise InstallError("Another asmond is already on PATH. Remove/migrate it yourself, or explicitly choose --allow-shadow for a separate install. Homebrew files are never changed.")
    print(f"Asmond direct installation\n  Files: {prefix}\n  Command: {command}\n  Python: {python}")
    if not args.yes and not confirm("Proceed with " + ("rollback?" if args.rollback else "installation/update?")):
        raise InstallError("Cancelled; the active version was not changed.")
    with tempfile.TemporaryDirectory(prefix="asmond-download-") as temporary_name:
        temporary = Path(temporary_name)
        if args.rollback:
            metadata = read_json(prefix / "releases" / current / ".asmond-release.json") if current else {}
            version = metadata.get("previous")
            if version is None:
                raise InstallError("No previous installed version is available for rollback.")
            version_tuple(version, development=True)
            folder = prefix / "releases" / version
            owned_directory(folder)
            previous_meta = read_json(folder / ".asmond-release.json")
            if previous_meta.get("manager") != MANAGER or previous_meta.get("version") != version:
                raise InstallError("The previous release is not managed by this installer.")
            with install_lock(prefix):
                _config, latest = read_installation(prefix)
                if latest != current:
                    raise InstallError("The active installation changed; retry.")
                verify_retained(folder, None, version, previous_meta.get("sha256", ""))
                smoke_test(python, folder, version)
                activate(prefix, version)
            print(f"Restored Asmond {version}; user settings were preserved.")
            return
        if args.archive:
            version = args.version
            version_tuple(version, development=True)
            archive, checksum = clean_path(args.archive), args.sha256
        else:
            version, archive, checksum = release_download(args.version, temporary)
        if current and version_tuple(version, development=True) <= version_tuple(current, development=True):
            print(f"Asmond {current} is already current/newer; no downgrade or overwrite performed.")
            configure_path(bin_dir, args)
            return
        staged = temporary / "candidate"
        staged.mkdir()
        extract_package(archive, checksum, version, staged)
        smoke_test(python, staged, version)
        prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existing:
            info = prefix.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise InstallError("Choose an owned directory without group/world write access.")
        with install_lock(prefix):
            if existing:
                _config, latest = read_installation(prefix)
                if latest != current:
                    raise InstallError("The active installation changed; retry.")
            elif (prefix / ".asmond-install.json").exists():
                raise InstallError("Another installer initialized this directory; retry.")
            elif any(path.name != ".install.lock" for path in prefix.iterdir()):
                raise InstallError("The unmanaged destination changed; nothing there was overwritten.")
            releases = prefix / "releases"
            releases.mkdir(exist_ok=True, mode=0o700)
            if releases.is_symlink():
                raise InstallError("Release directory must not be a symlink.")
            destination = releases / version
            if os.path.lexists(destination):
                # An update after rollback can reactivate an exact verified copy,
                # but must never overwrite different or modified retained files.
                verify_retained(destination, staged, version, checksum)
                smoke_test(python, destination, version)
            else:
                # Stage on the same filesystem so the complete release appears atomically.
                with tempfile.TemporaryDirectory(prefix=".candidate-", dir=releases) as stage_name:
                    stage = Path(stage_name)
                    shutil.copytree(staged, stage / "release")
                    release_meta = {"manager": MANAGER, "version": version, "previous": current, "sha256": checksum.lower(),
                                    "files": {name: hashlib.sha256((staged / name).read_bytes()).hexdigest() for name in PACKAGE_FILES}}
                    atomic_file(stage / "release/.asmond-release.json", (json.dumps(release_meta) + "\n").encode())
                    os.replace(stage / "release", destination)
            bin_dir.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(command) and (not existing or command.is_symlink() or command.read_bytes() != expected_launcher):
                raise InstallError("The command destination changed; it was not overwritten.")
            atomic_file(command, expected_launcher, 0o755)
            if not existing:
                atomic_file(prefix / ".asmond-install.json", (json.dumps(config) + "\n").encode())
            activate(prefix, version)
        print(f"Installed Asmond {version}. Settings were preserved.\nRun now: {shlex.quote(str(command))}\nUpdate: asmond --update")
        configure_path(bin_dir, args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dependency-free direct Asmond installer; macOS only.")
    for name in ("prefix", "bin-dir", "python", "version", "archive", "sha256"):
        parser.add_argument("--" + name)
    for name in ("yes", "add-path", "allow-shadow", "update", "rollback"):
        parser.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    try:
        install(args)
        return 0
    except (InstallError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"Asmond installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
ASMOND_INSTALLER_PY
