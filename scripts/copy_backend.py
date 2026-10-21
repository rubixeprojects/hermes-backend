#!/usr/bin/env python3
"""Create a safe, separate source-tree copy of Hermes's Python backend."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

_MARKER = ".hermes-backend-generated.json"
_MARKER_VERSION = 1
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "release",
    "tests", "test", "testdata",
}
_RUNTIME_DIRS = (
    "assets", "locales", "native", "optional-mcps", "optional-skills",
    "plugin-catalog", "skills",
)
_ROOT_FILES = ("LICENSE", "README.md", "hermes", "cli-config.yaml.example", "uv.lock")
_README = """# Hermes Python backend source copy

This folder is a separate copy of the Python backend source and its runtime assets. TypeScript
frontends, tests, local credentials, VCS data, environments, and caches are not included.

The copy preserves the original Python package layout. It is not a prebuilt isolated runtime;
set up Python and the project dependencies here before launching Hermes. Use this directory as
the working tree for backend changes.

To regenerate from the original checkout, run `python3 scripts/copy_backend.py --replace`
from that checkout's root. Only an output created by that generator from the same source root
can be replaced.
"""


def _package_roots(source: Path) -> set[str]:
    metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    include = metadata.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {}).get("include", ["*"])
    return {item.split(".", 1)[0] for item in include if isinstance(item, str) and item}


def _ignore(directory: str, names: list[str]) -> list[str]:
    parent = Path(directory)
    return [
        name for name in names
        if name in _EXCLUDED_DIRS
        or name.endswith((".egg-info", ".pyc"))
        or name.startswith(".")
        or (parent / name).is_symlink()
    ]


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=_ignore, symlinks=False)


def _copy_source(source: Path, stage: Path) -> None:
    package_roots = _package_roots(source)
    for name in sorted(package_roots):
        candidate = source / name
        if candidate.is_dir() and not candidate.is_symlink():
            _copy_tree(candidate, stage / name)

    for entry in source.iterdir():
        if entry.is_file() and entry.suffix == ".py" and not entry.is_symlink():
            shutil.copy2(entry, stage / entry.name)

    for name in _RUNTIME_DIRS:
        candidate = source / name
        if candidate.is_dir() and not candidate.is_symlink():
            _copy_tree(candidate, stage / name)

    root_files = set(_ROOT_FILES)
    metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    readme = metadata.get("project", {}).get("readme")
    if isinstance(readme, str):
        root_files.add(readme)
    elif isinstance(readme, dict) and isinstance(readme.get("file"), str):
        root_files.add(readme["file"])
    root_files.update(p.name for p in source.glob("LICENSE*") if p.is_file() and not p.is_symlink())
    root_files.update(p.name for p in source.glob("CONTRIBUTING*") if p.is_file() and not p.is_symlink())
    root_files.update(("pyproject.toml", "setup.py", "setup.cfg"))

    for relative in sorted(root_files):
        candidate = source / relative
        if candidate.is_file() and not candidate.is_symlink():
            if not candidate.resolve().is_relative_to(source):
                raise ValueError(f"source file escapes repository root: {relative}")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)


def _owned_output(destination: Path, source: Path) -> bool:
    marker = destination / _MARKER
    if destination.is_symlink() or not destination.is_dir() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return record == {"version": _MARKER_VERSION, "source": str(source)}


def generate(source: Path, destination: Path, *, replace: bool = False) -> int:
    """Generate output, refusing to overwrite any tree not created for this source."""
    source = source.resolve(strict=True)
    if not (source / "pyproject.toml").is_file():
        raise ValueError(f"source does not contain pyproject.toml: {source}")
    destination = destination.absolute()
    if destination.exists() and destination.is_symlink():
        raise ValueError("destination must not be a symlink")
    destination_parent = destination.parent.resolve(strict=True)
    destination = destination_parent / destination.name
    if destination.is_relative_to(source) and not (
        destination.parent == source and destination.name == "hermes-backend"
    ):
        raise ValueError("destination inside the source must be named hermes-backend at its root")
    if source.is_relative_to(destination):
        raise ValueError("destination cannot contain the source")

    exists = destination.exists()
    if exists and (not replace or not _owned_output(destination, source)):
        raise FileExistsError(f"refusing to replace unowned output: {destination}")

    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.stage-", dir=destination_parent) as temp:
        stage = Path(temp) / destination.name
        stage.mkdir()
        _copy_source(source, stage)
        (stage / "HERMES_BACKEND_README.md").write_text(_README, encoding="utf-8")
        (stage / _MARKER).write_text(
            json.dumps({"version": _MARKER_VERSION, "source": str(source)}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if exists:
            backup = Path(temp) / "previous-output"
            os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except BaseException:
                os.replace(backup, destination)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(stage, destination)

    return sum(1 for path in destination.rglob("*") if path.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, default=None)
    parser.add_argument("--replace", action="store_true", help="replace only output previously generated from this source")
    args = parser.parse_args(argv)
    destination = args.destination or args.source / "hermes-backend"
    try:
        count = generate(args.source, destination, replace=args.replace)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"backend copy failed: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {destination.resolve()} ({count} files)")
    print("This is a source bundle; install dependencies from inside the directory before running Hermes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
