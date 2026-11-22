#!/usr/bin/env python3
"""Build direct-install release assets locally; never tags, uploads or publishes."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile


def build_archive(root: Path, output: Path, *, allow_dev: bool = False, nightly: str | None = None) -> tuple[Path, Path]:
    source = (root / "asmond.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    versions = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ValueError("Expected one literal VERSION in asmond.py")
    source_version = versions[0]
    installer = (root / "install.sh").read_text(encoding="utf-8")
    engine = installer.split("<<'ASMOND_INSTALLER_PY'\n", 1)[1].rsplit("\nASMOND_INSTALLER_PY", 1)[0]
    lists = {}
    for node in ast.parse(engine).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"RUNTIME_FILES", "DOCUMENT_FILES", "VERSION_PATTERN"}:
                    lists[target.id] = ast.literal_eval(node.value)
    version = source_version
    if nightly is not None:
        if allow_dev or not source_version.endswith("-dev"):
            raise ValueError("Nightly packaging requires a -dev source and cannot use --allow-dev")
        if not re.fullmatch(r"[0-9]{14}\.g[a-f0-9]{12}", nightly):
            raise ValueError("Nightly identity must be YYYYMMDDHHMMSS.g followed by 12 lowercase commit digits")
        version = f"{source_version}.{nightly}"
    match = re.fullmatch(lists["VERSION_PATTERN"], version)
    if not match or len(version) > 96:
        raise ValueError("Invalid version")
    if match[5]:
        datetime.strptime(match[5], "%Y%m%d%H%M%S")
    if match[4] and not allow_dev and nightly is None:
        raise ValueError("Development versions cannot be packaged for release; choose --nightly or local --allow-dev")
    if not allow_dev and nightly is None and re.search(r"Development (?:build|checkout):.*-dev", (root / "README.md").read_text(encoding="utf-8")):
        raise ValueError("Remove the active README development-build label before packaging a release")
    manual = (root / "man/asmond.1").read_text(encoding="utf-8").splitlines()
    if not manual or f'"Asmond {source_version}"' not in manual[0]:
        raise ValueError("Manual and runtime versions differ")
    # Literal installer metadata is shared without executing installer code.
    names = lists["RUNTIME_FILES"] + lists["DOCUMENT_FILES"]
    if not isinstance(names, tuple) or len(set(names)) != len(names) or any(not isinstance(name, str) for name in names):
        raise ValueError("Invalid installer package allowlist")
    extras = {path.name for path in root.glob("asmond_*.py")} - set(lists["RUNTIME_FILES"])
    if extras:
        raise ValueError("Unexpected runtime modules: " + ", ".join(sorted(extras)))
    payloads = {}
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or symlinked package file: {name}")
        payloads[name] = path.read_bytes()
        if nightly is not None:
            if name == "asmond.py":
                updated, count = re.subn(r'^VERSION = "[^"\n]+"$', f'VERSION = "{version}"', source, count=1, flags=re.M)
                if count != 1:
                    raise ValueError("Expected a literal VERSION assignment for nightly packaging")
                payloads[name] = updated.encode("utf-8")
            elif name == "man/asmond.1":
                payloads[name] = payloads[name].replace(f'"Asmond {source_version}"'.encode(), f'"Asmond {version}"'.encode(), 1)
            elif name == "README.md":
                payloads[name] = re.sub(r"^Development (?:build|checkout):.*$", f"Nightly build: {version}.", payloads[name].decode("utf-8"), count=1, flags=re.M).encode("utf-8")
        if name.endswith(".py"):
            compile(payloads[name], name, "exec")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"asmond-{version}.tar.gz"
    checksum = output / f"{archive.name}.sha256"
    if archive.exists() or archive.is_symlink() or checksum.exists() or checksum.is_symlink():
        raise ValueError("Release assets already exist; refusing to overwrite them")
    # Deterministic gzip and tar metadata, with no Git history or local identifiers.
    with archive.open("xb") as file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for name, data in sorted(payloads.items()):
                    info = tarfile.TarInfo(f"asmond-{version}/{name}")
                    info.size = len(data)
                    info.mode = 0o755 if name in ("asmond.py", "install.sh") else 0o644
                    info.mtime = info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with checksum.open("x", encoding="ascii") as file:
        file.write(f"{digest}  {archive.name}\n")
    return archive, checksum


def write_nightly_formula(archive: Path, checksum: Path, destination: Path) -> Path:
    """Render a pinned formula only from the archive/checksum just built."""
    version = archive.name.removeprefix("asmond-").removesuffix(".tar.gz")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]{14}\.g[a-f0-9]{12}", version):
        raise ValueError("A versioned nightly archive is required")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksum.read_text(encoding="ascii") != f"{digest}  {archive.name}\n":
        raise ValueError("Nightly archive/checksum mismatch")
    formula = '''class AsmondNightly < Formula
  desc "Nightly macOS terminal power, thermal and activity monitor"
  homepage "https://github.com/Fxxrz/asmond"
  url "https://github.com/Fxxrz/asmond/releases/download/v@VERSION@/asmond-@VERSION@.tar.gz"
  version "@VERSION@"
  sha256 "@SHA256@"
  license "MIT"

  depends_on :macos
  depends_on "python@3.14"

  def install
    python = formula_opt_bin("python@3.14")/"python3.14"
    libexec.install "asmond.py"
    libexec.install Dir["asmond_*.py"]
    (bin/"asmond-nightly").write <<~EOS
      #!/bin/sh
      export ASMOND_SETTINGS_DIR="${ASMOND_SETTINGS_DIR:-$HOME/Library/Application Support/Asmond Nightly}"
      exec "#{python}" -B -E -s "#{libexec}/asmond.py" "$@"
    EOS
    chmod 0755, bin/"asmond-nightly"
    man1.install "man/asmond.1" => "asmond-nightly.1"
  end

  def caveats
    <<~EOS
      Run asmond-nightly; Stable remains available as asmond.
      Nightly settings default to:
        ~/Library/Application Support/Asmond Nightly/settings.json
      ASMOND_SETTINGS_DIR overrides this path when explicitly set.
      Update Nightly with brew upgrade asmond-nightly.
    EOS
  end

  test do
    ENV["ASMOND_SETTINGS_DIR"] = testpath/"settings"
    assert_equal "Asmond #{version}\\n", shell_output("#{bin}/asmond-nightly --version")
    assert_match '"app": "Asmond"', shell_output("#{bin}/asmond-nightly report --mock --json")
  end
end
'''
    formula = formula.replace("@VERSION@", version).replace("@SHA256@", digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as file:
        file.write(formula)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dev", action="store_true", help="explicit local test packaging only; never publish these assets")
    parser.add_argument("--nightly", metavar="YYYYMMDDHHMMSS.gREVISION", help="build an immutable nightly from -dev source; UTC timestamp and 12-digit source commit")
    parser.add_argument("--formula-output", type=Path, help="also write an exact-URL/SHA asmond-nightly.rb for the tap; does not publish")
    args = parser.parse_args()
    try:
        if args.formula_output and (not args.nightly or args.formula_output.exists() or args.formula_output.is_symlink()):
            raise ValueError("A new formula destination and --nightly are required")
        paths = build_archive(Path(__file__).resolve().parent.parent, args.output, allow_dev=args.allow_dev, nightly=args.nightly)
        if args.formula_output:
            paths += (write_nightly_formula(*paths, args.formula_output),)
    except (OSError, ValueError, SyntaxError) as exc:
        parser.error(str(exc))
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
