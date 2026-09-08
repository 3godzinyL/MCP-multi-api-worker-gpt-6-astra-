"""Copy a reviewed, reproducible source snapshot into the ordinary GitHub folder."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

try:
    from .scan_repository import ROOT, candidates, scan_paths, tree_paths
except ImportError:
    from scan_repository import ROOT, candidates, scan_paths, tree_paths

SOURCE_MANIFEST = "SOURCE_MANIFEST.json"
SOURCE_ARCHIVE = "3api-source.zip"
CHECKSUMS = "SHA256SUMS"
ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root: Path, paths: list[str]) -> dict:
    return {relative: {"sha256": sha256(root / relative), "bytes": (root / relative).stat().st_size}
            for relative in sorted(paths)}


def snapshot_sha256(files: dict) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def create_zip(root: Path, output: Path, paths: list[str], prefix: str) -> None:
    """Fixed ordering, timestamps and modes make identical inputs reproducible."""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(paths):
            info = zipfile.ZipInfo(prefix + "/" + relative, ZIP_DATE)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (root / relative).read_bytes())


def write_checksums(root: Path, paths: list[str]) -> None:
    (root / CHECKSUMS).write_text("".join(f"{sha256(root / path)}  {path}\n" for path in sorted(paths)), encoding="utf-8", newline="\n")


def safe_output(root: Path, output: Path, *, internal_directory: str | None = None) -> Path:
    """Allow an external destination or one exact, allowlist-excluded export path.

    The explicit in-checkout exception never permits arbitrary source descendants.
    Check all existing ancestors before resolving so junctions cannot redirect an
    apparently safe GitHub output into source files or another user's directory.
    """
    root = root.resolve()
    output = Path(os.path.abspath(output))
    for ancestor in (output, *output.parents):
        try:
            attributes = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (ancestor.is_symlink() or getattr(attributes, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("Output and its ancestors must not be symbolic links or junctions")
    resolved = output.resolve()
    internal = root / internal_directory if internal_directory else None
    if (resolved == root or root.is_relative_to(resolved)
            or (resolved.is_relative_to(root) and (resolved != internal or output != internal))):
        raise ValueError("Output must be separate from the source tree")
    return resolved


def copy_files(root: Path, output: Path, paths: list[str]) -> None:
    for relative in paths:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)


def publish_files(staging: Path, output: Path, *, preserve_releases: bool = False) -> None:
    """Replace managed files only; never recursively erase a user's directory.

    An unknown or stale file makes packaging fail before any output is overwritten.
    Select a new, empty output directory when changing the package's file layout.
    """
    expected = set(tree_paths(staging))
    if output.exists():
        if not output.is_dir():
            raise ValueError("Output is not a directory")
        existing = tree_paths(output)
        for relative in existing:
            path = output / relative
            if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
                raise ValueError(f"Output contains a link or path escape: {relative}")
            if relative not in expected and not (preserve_releases and relative.startswith("releases/")):
                raise ValueError(f"Output contains an unmanaged or stale file: {relative}; choose an empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    for relative in sorted(expected):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        (staging / relative).replace(target)


def package_source(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = safe_output(root, output, internal_directory="GitHub")
    paths = candidates(root)
    if not paths:
        raise ValueError("No allowlisted source files found")
    findings = scan_paths(root, paths)
    if findings:
        raise ValueError("Source scan failed: " + "; ".join(f"{path}: {reason}" for path, reason in findings))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".3api-source-", dir=output.parent) as temporary:
        staging = Path(temporary)
        copy_files(root, staging, paths)
        files = inventory(staging, paths)
        manifest = {"schema_version": 1, "kind": "3api-source", "files": files,
                    "snapshot_sha256": snapshot_sha256(files)}
        write_json(staging / SOURCE_MANIFEST, manifest)
        create_zip(staging, staging / SOURCE_ARCHIVE, paths + [SOURCE_MANIFEST], "3api")
        write_checksums(staging, paths + [SOURCE_MANIFEST, SOURCE_ARCHIVE])
        publish_files(staging, output, preserve_releases=True)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "GitHub")
    args = parser.parse_args(argv)
    try:
        manifest = package_source(ROOT, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"Packaged {len(manifest['files'])} source files into {args.output_dir.resolve()}")
    print(f"Archive: {SOURCE_ARCHIVE}; checksums: {CHECKSUMS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
