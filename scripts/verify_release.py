"""Verify source/runtime inventories, checksums, ZIP contents and local README links.

This is a static delivery check. Starting the unpacked application, completing a mock
task, restarting it and speaking MCP are separate integration checks; this command
never claims to have run them or to have verified an external website.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import zipfile

try:
    from .package_release import RELEASE_ARCHIVE, RELEASE_MANIFEST, REQUIRED_RUNTIME, is_windows_x64
    from .package_source import CHECKSUMS, SOURCE_ARCHIVE, SOURCE_MANIFEST, sha256, snapshot_sha256
    from .scan_repository import (ROOT, candidates, path_reason, runtime_allowed,
                                 scan_paths, source_allowed, tree_paths)
except ImportError:
    from package_release import RELEASE_ARCHIVE, RELEASE_MANIFEST, REQUIRED_RUNTIME, is_windows_x64
    from package_source import CHECKSUMS, SOURCE_ARCHIVE, SOURCE_MANIFEST, sha256, snapshot_sha256
    from scan_repository import (ROOT, candidates, path_reason, runtime_allowed,
                                scan_paths, source_allowed, tree_paths)

HASH = re.compile(r"^[0-9a-f]{64}$")


def read_manifest(root: Path, filename: str, kind: str) -> dict:
    document = json.loads((root / filename).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1 or document.get("kind") != kind:
        raise ValueError(f"Invalid {filename} schema or kind")
    files = document.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError(f"Invalid {filename} file inventory")
    seen = set()
    for relative, record in files.items():
        permitted = source_allowed(relative) if kind == "3api-source" else (relative == "3api.exe" or runtime_allowed(relative))
        if not permitted or relative.casefold() in seen:
            raise ValueError(f"Unsafe or duplicate path in {filename}: {relative}")
        seen.add(relative.casefold())
        if (not isinstance(record, dict) or not isinstance(record.get("sha256"), str)
                or not HASH.fullmatch(record["sha256"])
                or type(record.get("bytes")) is not int or record["bytes"] < 0):
            raise ValueError(f"Invalid file record in {filename}: {relative}")
    return document


def check_inventory(root: Path, files: dict) -> list[str]:
    findings = []
    for relative, record in files.items():
        path = root / relative
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())):
            findings.append(f"Missing or unsafe payload file: {relative}")
        elif path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            findings.append(f"Payload hash/size mismatch: {relative}")
    return findings


def check_checksums(root: Path, expected: set[str]) -> list[str]:
    records = {}
    for line in (root / CHECKSUMS).read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not HASH.fullmatch(digest) or path_reason(relative) or relative in records:
            return [f"Malformed or duplicate entry in {root.name}/{CHECKSUMS}"]
        records[relative] = digest
    if set(records) != expected:
        return [f"Incomplete or unexpected entries in {root.name}/{CHECKSUMS}"]
    findings = []
    for relative, digest in records.items():
        path = root / relative
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                or sha256(path) != digest):
            findings.append(f"SHA-256 mismatch: {root.name}/{relative}")
    return findings


def check_archive(root: Path, filename: str, files: dict, manifest: str, prefix: str) -> list[str]:
    expected = {prefix + "/" + relative: record["sha256"] for relative, record in files.items()}
    expected[prefix + "/" + manifest] = sha256(root / manifest)
    try:
        with zipfile.ZipFile(root / filename) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected):
                return [f"ZIP inventory differs from payload: {filename}"]
            for name in names:
                if hashlib.sha256(archive.read(name)).hexdigest() != expected[name]:
                    return [f"ZIP hash differs from payload: {filename}!{name}"]
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return [f"Unreadable ZIP: {filename}"]
    return []


def readme_links(root: Path) -> list[str]:
    """Check local file/image destinations; remote URLs are deliberately not fetched."""
    findings = []
    for filename in ("README.md", "README.en.md"):
        path = root / filename
        if not path.is_file():
            findings.append(f"Missing README: {filename}")
            continue
        text = re.sub(r"```[^\n]*\n.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        links = re.findall(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)", text)
        links += re.findall(r"(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", text, flags=re.I)
        links += re.findall(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", text, flags=re.M)
        for link in links:
            link = link.strip("<>")
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (root / unquote(parsed.path).lstrip("/")).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                findings.append(f"Broken local link in {filename}: {link}")
    return findings


def verify(root: Path, *, source_only: bool = False) -> list[str]:
    root = root.resolve()
    if not root.is_dir():
        return ["Delivery root does not exist"]
    try:
        findings = [f"{path}: {reason}" for path, reason in scan_paths(root, tree_paths(root), delivery=True)]
        if findings:
            return findings
        source = read_manifest(root, SOURCE_MANIFEST, "3api-source")
        files = source["files"]
        if source.get("snapshot_sha256") != snapshot_sha256(files):
            findings.append("Source snapshot fingerprint is invalid")
        if set(candidates(root)) != set(files):
            findings.append("Source manifest does not cover the exact source tree")
        findings.extend(check_inventory(root, files))
        findings.extend(check_checksums(root, set(files) | {SOURCE_MANIFEST, SOURCE_ARCHIVE}))
        findings.extend(check_archive(root, SOURCE_ARCHIVE, files, SOURCE_MANIFEST, "3api"))
        findings.extend(readme_links(root))

        if source_only:
            if set(tree_paths(root)) != set(files) | {SOURCE_MANIFEST, SOURCE_ARCHIVE, CHECKSUMS}:
                findings.append("Source folder contains missing or unmanifested files; omit --source-only for a full release")
            return findings

        release_root = root / "releases" / "windows-x64"
        release = read_manifest(release_root, RELEASE_MANIFEST, "3api-windows-x64")
        runtime = release["files"]
        if release.get("platform") != "windows-x64" or not is_windows_x64(release_root / "3api.exe"):
            findings.append("Release executable is not Windows x64 PE")
        if not REQUIRED_RUNTIME.issubset(runtime):
            findings.append("Release is missing required worker/bootstrap files")
        expected_runtime = {path for path in files if runtime_allowed(path)} | {"3api.exe"}
        if set(runtime) != expected_runtime:
            findings.append("Release does not contain the exact runtime allowlist")
        if set(tree_paths(release_root)) != set(runtime) | {RELEASE_MANIFEST, RELEASE_ARCHIVE, CHECKSUMS}:
            findings.append("Release folder contains missing or unmanifested files")
        if release.get("source_snapshot_sha256") != source.get("snapshot_sha256"):
            findings.append("Release was packaged from a different source snapshot")
        for relative in set(files) & set(runtime):
            if files[relative] != runtime[relative]:
                findings.append(f"Runtime/source file mismatch: {relative}")
        build_inputs = {path: record for path, record in files.items()
                        if path.startswith("src/") or path in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"}}
        if release.get("build_inputs") != build_inputs:
            findings.append("Release Rust input fingerprints differ from sources")
        python = release.get("python", {})
        if (not isinstance(python, dict) or python.get("minimum_version") != "3.11"
                or python.get("bundled") is not False or python.get("bootstrap") != "bootstrap.bat"
                or python.get("first_start_requires_network") is not True
                or python.get("requirements_sha256") != runtime.get("requirements.txt", {}).get("sha256")):
            findings.append("Release Python bootstrap metadata is invalid")
        findings.extend(check_inventory(release_root, runtime))
        findings.extend(check_checksums(release_root, set(runtime) | {RELEASE_MANIFEST, RELEASE_ARCHIVE}))
        findings.extend(check_archive(release_root, RELEASE_ARCHIVE, runtime, RELEASE_MANIFEST, "3api-windows-x64"))
        findings.extend(readme_links(release_root))
        return findings
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ["Cannot verify delivery: " + str(exc)]


def unpack_release(root: Path, destination: Path) -> Path:
    """Unpack a verified release for a subsequent smoke test, without overwriting files."""
    findings = verify(root)
    if findings:
        raise ValueError("Delivery verification failed: " + "; ".join(findings))
    if destination.is_symlink():
        raise ValueError("Extraction destination must not be a symbolic link")
    destination = destination.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Extraction destination must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = root / "releases" / "windows-x64" / RELEASE_ARCHIVE
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            target = destination / info.filename
            if not target.resolve().is_relative_to(destination):
                raise ValueError("ZIP entry escapes extraction destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(archive.read(info))
    return destination / "3api-windows-x64"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "GitHub")
    parser.add_argument("--source-only", action="store_true", help="Verify a source snapshot without a Windows release")
    parser.add_argument("--extract-to", type=Path, help="After verification, unpack into a new/empty directory for a separate smoke test")
    args = parser.parse_args(argv)
    if args.source_only and args.extract_to:
        parser.error("--source-only cannot be combined with --extract-to")
    if args.extract_to:
        try:
            unpacked = unpack_release(args.root, args.extract_to)
        except (OSError, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 1
        print(f"Unpacked verified release into {unpacked}")
    else:
        findings = verify(args.root, source_only=args.source_only)
        for finding in findings:
            print("FAIL: " + finding)
        if findings:
            return 1
    if args.source_only:
        print("PASS: source inventory, SHA-256, ZIP contents and local README links")
    else:
        print("PASS: source/runtime inventories, SHA-256, ZIP contents, Windows x64 PE, source consistency and local README links")
    print("Runtime bootstrap, browser, mock-task/restart and MCP checks must be recorded separately; external links were not fetched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
