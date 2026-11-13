#!/usr/bin/env python3
"""Build direct-install release assets locally; never tags, uploads or publishes."""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile


def build_archive(root: Path, output: Path, *, allow_dev: bool = False) -> tuple[Path, Path]:
    source = (root / "asmond.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    versions = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ValueError("Expected one literal VERSION in asmond.py")
    version = versions[0]
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev)?", version):
        raise ValueError("Invalid version")
    if version.endswith("-dev") and not allow_dev:
        raise ValueError("Development versions cannot be packaged for release; --allow-dev is for local tests only")
    if not allow_dev and re.search(r"Development build:.*-dev", (root / "README.md").read_text(encoding="utf-8")):
        raise ValueError("Remove the active README development-build label before packaging a release")
    manual = (root / "man/asmond.1").read_text(encoding="utf-8").splitlines()
    if not manual or f'"Asmond {version}"' not in manual[0]:
        raise ValueError("Manual and runtime versions differ")
    # Read the installer's literal allowlists without executing any installer
    # code. Build and extraction must agree even with local scratch modules.
    installer = (root / "install.sh").read_text(encoding="utf-8")
    engine = installer.split("<<'ASMOND_INSTALLER_PY'\n", 1)[1].rsplit("\nASMOND_INSTALLER_PY", 1)[0]
    lists = {}
    for node in ast.parse(engine).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"RUNTIME_FILES", "DOCUMENT_FILES"}:
                    lists[target.id] = ast.literal_eval(node.value)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dev", action="store_true", help="explicit local test packaging only; never publish these assets")
    args = parser.parse_args()
    try:
        paths = build_archive(Path(__file__).resolve().parent.parent, args.output, allow_dev=args.allow_dev)
    except (OSError, ValueError, SyntaxError) as exc:
        parser.error(str(exc))
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
