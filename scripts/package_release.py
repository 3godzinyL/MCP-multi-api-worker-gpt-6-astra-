"""Package the final Windows x64 executable with its private Python worker."""
from __future__ import annotations

import argparse
from pathlib import Path
import struct
import tempfile

try:
    from .package_source import (CHECKSUMS, copy_files, create_zip, inventory,
        publish_files, safe_output, sha256, snapshot_sha256, write_checksums, write_json)
    from .scan_repository import ROOT, candidates, runtime_allowed, scan_paths
except ImportError:
    from package_source import (CHECKSUMS, copy_files, create_zip, inventory,
        publish_files, safe_output, sha256, snapshot_sha256, write_checksums, write_json)
    from scan_repository import ROOT, candidates, runtime_allowed, scan_paths

RELEASE_MANIFEST = "RELEASE_MANIFEST.json"
RELEASE_ARCHIVE = "3api-windows-x64.zip"
REQUIRED_RUNTIME = frozenset({
    "bootstrap.py", "bootstrap.bat", "requirements.txt", "manage.py", "start.bat",
    "scripts/start.ps1", "providers.example.toml", "dashboard/__init__.py",
    "dashboard/sidecar.py", "dashboard/server.py", "dashboard/runner.py",
    "dashboard/store.py", "dashboard/assets/index.html", "proxy/__init__.py",
    "proxy/config.py", "proxy/credentials.py", "proxy/server.py", "examples/codex-mcp.toml",
    "dashboard/assets/vendor/THREE-LICENSE.txt", "dashboard/assets/vendor/three.module.min.js",
    "dashboard/assets/vendor/FontLoader.js", "dashboard/assets/vendor/TextGeometry.js",
    "dashboard/assets/vendor/RoomEnvironment.js", "dashboard/assets/vendor/helvetiker_bold.typeface.json",
    "dashboard/assets/vendor/three.core.min.js", "dashboard/assets/vendor/HELVETIKER-LICENSE.txt",
    "dashboard/assets/vendor/README.md",
})


def is_windows_x64(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            offset = struct.unpack_from("<I", header, 60)[0]
            if offset < 64 or offset > path.stat().st_size - 6:
                return False
            stream.seek(offset)
            return stream.read(6) == b"PE\0\0\x64\x86"
    except (OSError, struct.error):
        return False


def package_release(root: Path, output: Path, binary: Path | None = None) -> dict:
    root = root.resolve()
    output = safe_output(root, output, internal_directory="GitHub/releases/windows-x64")
    binary = (binary or root / "target" / "release" / "3api.exe").resolve()
    if not is_windows_x64(binary):
        raise ValueError("A Windows x64 3api.exe is required; run cargo build --locked --release first")
    source_paths = candidates(root)
    paths = [path for path in source_paths if runtime_allowed(path)]
    missing = REQUIRED_RUNTIME - set(paths)
    if missing:
        raise ValueError("Required worker or startup files missing: " + ", ".join(sorted(missing)))
    findings = scan_paths(root, source_paths)
    if findings:
        raise ValueError("Source scan failed: " + "; ".join(f"{path}: {reason}" for path, reason in findings))
    build_paths = [path for path in source_paths if path.startswith("src/") or path in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"}]
    if any((root / path).stat().st_mtime_ns > binary.stat().st_mtime_ns for path in build_paths):
        raise ValueError("Rust inputs are newer than 3api.exe; rebuild the final snapshot with cargo build --locked --release")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".3api-release-", dir=output.parent) as temporary:
        staging = Path(temporary)
        copy_files(root, staging, paths)
        (staging / "3api.exe").write_bytes(binary.read_bytes())
        payload = paths + ["3api.exe"]
        source_files = inventory(root, source_paths)
        manifest = {
            "schema_version": 1, "kind": "3api-windows-x64", "platform": "windows-x64",
            "files": inventory(staging, payload),
            "source_snapshot_sha256": snapshot_sha256(source_files),
            "build_inputs": inventory(root, build_paths),
            "python": {"minimum_version": "3.11", "bundled": False, "bootstrap": "bootstrap.bat",
                       "requirements_sha256": sha256(staging / "requirements.txt"),
                       "first_start_requires_network": True},
        }
        write_json(staging / RELEASE_MANIFEST, manifest)
        create_zip(staging, staging / RELEASE_ARCHIVE, payload + [RELEASE_MANIFEST], "3api-windows-x64")
        write_checksums(staging, payload + [RELEASE_MANIFEST, RELEASE_ARCHIVE])
        publish_files(staging, output)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "GitHub" / "releases" / "windows-x64")
    parser.add_argument("--binary", type=Path, help="Final Windows x64 executable (default: target/release/3api.exe)")
    args = parser.parse_args(argv)
    try:
        manifest = package_release(ROOT, args.output_dir, args.binary)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"Packaged {len(manifest['files'])} runtime files into {args.output_dir.resolve()}")
    print(f"Archive: {RELEASE_ARCHIVE}; checksums: {CHECKSUMS}")
    print("The panel includes a private Python worker. First start needs Python 3.11+ and network access for bootstrap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
