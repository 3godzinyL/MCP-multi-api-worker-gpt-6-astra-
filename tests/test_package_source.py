"""Packaging regressions use isolated source snapshots and a synthetic PE header.

The synthetic executable is never run; actual release startup is an integration test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

import pytest

from scripts import package_release, package_source, scan_repository, verify_release


def write(root, relative, content="fixture\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "working snapshot"
    for relative in package_release.REQUIRED_RUNTIME | {
        "Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "src/main.rs", "src/lib.rs",
        "README.md", "README.en.md", "CONTRIBUTING.md", "SECURITY.md",
        "docs/images/dashboard-pl.png", "docs/STARTUP.md", "tests/test_example.py",
        "package.json", "package-lock.json", "dashboard/assets/app.js",
        "scripts/package_source.py", "scripts/package_release.py", "scripts/verify_release.py",
        "rust_launcher.py", "configure_codex.bat", "tests/browser/capture-demo.mjs",
        ".gitignore", ".github/workflows/ci.yml",
    }:
        write(root, relative)
    for relative in ("README.md", "README.en.md"):
        write(root, relative, "# 3API\n[Start](docs/STARTUP.md)\n![Panel](docs/images/dashboard-pl.png)\n")
    header = bytearray(128)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    header[64:70] = b"PE\0\0\x64\x86"
    binary = write(root, "target/release/3api.exe", bytes(header))
    # A final build must be at least as recent as all Rust inputs.
    newest = max(path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file())
    os.utime(binary, ns=(newest, newest))
    return root


def delivery(source, tmp_path):
    output = tmp_path / "GitHub"
    package_source.package_source(source, output)
    package_release.package_release(source, output / "releases" / "windows-x64")
    return output


def test_source_allowlist_excludes_private_files_and_does_not_use_git(source, tmp_path, monkeypatch):
    private = [
        "providers.toml", ".env", ".env.production", "python311.exe", "notes.txt",
        "data/user.sqlite3", "logs/server.log", "backups/README.md", "target/debug/3api.exe",
        "node_modules/pkg/index.js", ".venv/Lib/module.py", ".git/index", "test-results/report.json",
        "dashboard/__pycache__/store.pyc", "tests/fixtures/providers.toml",
        "docs/private.key", "docs/.cache/report.md", "docs/images/credentials.json",
        "scripts/one_off_export.py", "src/auth.json", "src/nested/providers.toml",
        "dashboard/assets/vendor/unreviewed.js",
    ]
    for relative in private:
        write(source, relative, "private fixture")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("packaging must not invoke Git"))
    output = tmp_path / "GitHub"
    before_index = (source / ".git/index").read_bytes()
    manifest = package_source.package_source(source, output)
    names = set(scan_repository.tree_paths(output))
    assert not names.intersection(private)
    assert {"Cargo.lock", "src/main.rs", "dashboard/assets/app.js", "tests/test_example.py",
            "docs/images/dashboard-pl.png", ".github/workflows/ci.yml", "tests/browser/capture-demo.mjs",
            "dashboard/assets/vendor/THREE-LICENSE.txt", "dashboard/assets/vendor/helvetiker_bold.typeface.json"}.issubset(names)
    assert (source / ".git/index").read_bytes() == before_index
    with zipfile.ZipFile(output / package_source.SOURCE_ARCHIVE) as archive:
        assert set(archive.namelist()) == {"3api/" + name for name in manifest["files"]} | {"3api/SOURCE_MANIFEST.json"}
    assert scan_repository.scan_paths(output, scan_repository.tree_paths(output), delivery=True) == []


def test_source_zip_and_checksums_are_reproducible(source, tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    package_source.package_source(source, first)
    os.utime(source / "README.md", (1700000000, 1700000000))
    package_source.package_source(source, second)
    for relative in (package_source.SOURCE_ARCHIVE, package_source.SOURCE_MANIFEST, package_source.CHECKSUMS):
        assert (first / relative).read_bytes() == (second / relative).read_bytes()


def test_release_has_worker_bootstrap_and_no_build_or_dependency_trees(source, tmp_path):
    output = delivery(source, tmp_path)
    release = output / "releases/windows-x64"
    manifest = json.loads((release / package_release.RELEASE_MANIFEST).read_text())
    assert package_release.REQUIRED_RUNTIME.issubset(manifest["files"])
    assert "3api.exe" in manifest["files"]
    assert {"rust_launcher.py", "configure_codex.bat"}.issubset(manifest["files"])
    assert manifest["python"]["bundled"] is False
    assert manifest["python"]["minimum_version"] == "3.11"
    assert not {"Cargo.toml", "src/main.rs", "package-lock.json", "tests/test_example.py"}.intersection(manifest["files"])
    assert verify_release.verify(output) == []
    unpacked = verify_release.unpack_release(output, tmp_path / "clean unpacked")
    assert (unpacked / "3api.exe").read_bytes() == (release / "3api.exe").read_bytes()
    assert not (unpacked / "target").exists()
    assert not (unpacked / "node_modules").exists()
    with pytest.raises(ValueError, match="new or empty"):
        verify_release.unpack_release(output, tmp_path / "clean unpacked")


def test_repack_preserves_release_and_rejects_unknown_output_without_overwriting(source, tmp_path):
    output = delivery(source, tmp_path)
    binary = output / "releases/windows-x64/3api.exe"
    before = binary.read_bytes()
    package_source.package_source(source, output)
    assert binary.read_bytes() == before
    readme = (output / "README.md").read_bytes()
    write(output, "providers.toml", "private")
    write(source, "README.md", "new source")
    with pytest.raises(ValueError, match="unmanaged or stale"):
        package_source.package_source(source, output)
    assert (output / "providers.toml").read_text() == "private"
    assert (output / "README.md").read_bytes() == readme


def test_rejects_secret_without_printing_value(source, tmp_path, capsys, monkeypatch):
    secret = "sk-" + "x" * 40
    write(source, "README.md", secret)
    monkeypatch.setattr(package_source, "ROOT", source)
    assert package_source.main(["--output-dir", str(tmp_path / "out")]) == 1
    output = capsys.readouterr().out
    assert secret not in output
    assert "OpenAI-like credential" in output
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("relative", ["../README.md", "/README.md", "C:/README.md", "docs\\readme.md", "docs/NUL.md", "docs/a./README.md", "docs/data/README.md"])
def test_unsafe_windows_or_private_paths_are_rejected(relative):
    assert scan_repository.path_reason(relative)
    assert not scan_repository.source_allowed(relative)


def test_output_must_be_outside_source_except_exact_github_export(source):
    for path in (source, source / "src", source / "GitHub/nested", source.parent):
        with pytest.raises(ValueError, match="separate"):
            package_source.package_source(source, path)


def test_in_checkout_github_export_does_not_package_itself(source):
    output = source / "GitHub"
    first = package_source.package_source(source, output)
    second = package_source.package_source(source, output)
    assert first == second
    assert not any(name.startswith("GitHub/") for name in second["files"])
    assert verify_release.verify(output, source_only=True) == []
    package_release.package_release(source, output / "releases/windows-x64")
    assert verify_release.verify(output) == []


def test_source_only_verification_detects_tampering_and_unexpected_release(source, tmp_path, capsys):
    output = tmp_path / "GitHub"
    package_source.package_source(source, output)
    assert verify_release.main(["--root", str(output), "--source-only"]) == 0
    assert "Windows x64 PE" not in capsys.readouterr().out
    write(output, "dashboard/store.py", "modified")
    assert any("hash/size mismatch" in item for item in verify_release.verify(output, source_only=True))
    package_source.package_source(source, output)
    package_release.package_release(source, output / "releases/windows-x64")
    assert any("omit --source-only" in item for item in verify_release.verify(output, source_only=True))


def test_rejects_symlink_in_sources_and_output(source, tmp_path):
    outside = write(tmp_path, "external.md", "not for publication")
    source_link = source / "docs/linked.md"
    try:
        source_link.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows user cannot create symlinks")
    with pytest.raises(ValueError, match="symlink or path escape"):
        package_source.package_source(source, tmp_path / "out")
    source_link.unlink()
    output = tmp_path / "out"
    output.mkdir()
    (output / "README.md").symlink_to(outside)
    with pytest.raises(ValueError, match="link or path escape"):
        package_source.package_source(source, output)
    assert outside.read_text() == "not for publication"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_rejects_windows_directory_junction_even_without_symlink_privilege(source, tmp_path):
    outside = tmp_path / "outside assets"
    write(outside, "private.md", "private fixture")
    junction = source / "docs" / "junction"
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        pytest.skip("Directory junction creation is unavailable")
    try:
        with pytest.raises(ValueError, match="link or escapes"):
            package_source.package_source(source, tmp_path / "out")
        assert (outside / "private.md").read_text() == "private fixture"
    finally:
        # Remove the junction itself; never recursively delete its target.
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_in_checkout_output_rejects_junction_without_overwriting_target(source, tmp_path):
    outside = tmp_path / "existing export"
    existing = write(outside, "README.md", "user-owned content")
    junction = source / "GitHub"
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        pytest.skip("Directory junction creation is unavailable")
    try:
        with pytest.raises(ValueError, match="links or junctions"):
            package_source.package_source(source, junction)
        with pytest.raises(ValueError, match="links or junctions"):
            package_release.package_release(source, junction / "releases/windows-x64")
        assert existing.read_text() == "user-owned content"
        assert not (outside / "releases").exists()
    finally:
        junction.rmdir()


def test_release_rejects_stale_or_wrong_architecture_binary_and_missing_worker(source, tmp_path):
    binary = source / "target/release/3api.exe"
    original = binary.read_bytes()
    os.utime(binary, (0, 0))
    with pytest.raises(ValueError, match="newer"):
        package_release.package_release(source, tmp_path / "out")
    binary.write_bytes(original.replace(b"\x64\x86", b"\x4c\x01"))
    with pytest.raises(ValueError, match="Windows x64"):
        package_release.package_release(source, tmp_path / "out")
    binary.write_bytes(original)
    (source / "dashboard/sidecar.py").unlink()
    with pytest.raises(ValueError, match="worker or startup"):
        package_release.package_release(source, tmp_path / "out")


def test_verifier_detects_payload_tampering(source, tmp_path):
    output = delivery(source, tmp_path)
    write(output, "releases/windows-x64/dashboard/store.py", "tampered")
    findings = verify_release.verify(output)
    assert any("hash/size mismatch" in finding for finding in findings)
    assert any("SHA-256 mismatch" in finding for finding in findings)


def test_verifier_rejects_changed_snapshot_after_repacking_only_sources(source, tmp_path):
    output = delivery(source, tmp_path)
    write(source, "dashboard/assets/app.js", "changed after release packaging")
    package_source.package_source(source, output)
    assert any("different source snapshot" in finding for finding in verify_release.verify(output))


@pytest.mark.parametrize("name", ["3api-windows-x64/../escape.txt", "3api-windows-x64/providers.toml", "3api-windows-x64/node_modules/pkg/index.js"])
def test_scanner_rejects_unsafe_or_private_archive_entries(source, tmp_path, name):
    output = delivery(source, tmp_path)
    archive = output / "releases/windows-x64" / package_release.RELEASE_ARCHIVE
    with zipfile.ZipFile(archive, "a") as zipped:
        zipped.writestr(name, "private fixture")
    findings = verify_release.verify(output)
    assert any(name in finding for finding in findings)
    with pytest.raises(ValueError, match="verification failed"):
        verify_release.unpack_release(output, tmp_path / "unpack")
    assert not (tmp_path / "unpack").exists()


def test_verifier_checks_zip_bytes_and_exact_checksum_coverage(source, tmp_path):
    output = delivery(source, tmp_path)
    release = output / "releases/windows-x64"
    archive = release / package_release.RELEASE_ARCHIVE
    with zipfile.ZipFile(archive) as old:
        payloads = {info.filename: old.read(info) for info in old.infolist()}
    payloads["3api-windows-x64/dashboard/store.py"] = b"different archived bytes"
    with zipfile.ZipFile(archive, "w") as changed:
        for name, data in payloads.items():
            changed.writestr(name, data)
    assert any("ZIP hash differs" in finding for finding in verify_release.verify(output))
    sums = release / "SHA256SUMS"
    sums.write_text("\n".join(sums.read_text().splitlines()[1:]) + "\n")
    assert any("Incomplete or unexpected" in finding for finding in verify_release.verify(output))


def test_verifier_reports_broken_readme_local_links(source, tmp_path):
    write(source, "README.md", "[Missing](docs/missing.md)\n<img src=\"docs/images/missing.png\">\n[Remote](https://example.invalid/docs)\n")
    output = delivery(source, tmp_path)
    findings = verify_release.verify(output)
    assert sum("Broken local link" in finding for finding in findings) == 4
    assert not any("example.invalid" in finding for finding in findings)


def test_scan_root_checks_every_file_in_delivery_and_cli_accepts_root(source, tmp_path, capsys):
    output = delivery(source, tmp_path)
    assert scan_repository.main(["--root", str(output)]) == 0
    assert verify_release.main(["--root", str(output)]) == 0
    write(output, "data/history.sqlite3", "private")
    assert scan_repository.main(["--root", str(output)]) == 1
    assert "private directory" in capsys.readouterr().out


def test_empty_private_output_directory_is_rejected(source, tmp_path):
    output = delivery(source, tmp_path)
    (output / "data").mkdir()
    assert any("private directory" in item for item in verify_release.verify(output))
    with pytest.raises(ValueError, match="unmanaged or stale"):
        package_source.package_source(source, output)


def test_repository_cli_help_documents_required_flags():
    root = Path(__file__).resolve().parents[1]
    for script, flag in (("package_source.py", "--output-dir"), ("package_release.py", "--output-dir"),
                         ("scan_repository.py", "--root"), ("verify_release.py", "--root")):
        result = subprocess.run([sys.executable, str(root / "scripts" / script), "--help"], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert flag in result.stdout
