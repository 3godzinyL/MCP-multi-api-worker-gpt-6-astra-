"""Allowlist and credential-pattern checks for source snapshots and deliveries.

No Git command is used: a checkout without a first commit and its index stay intact.
The patterns are a useful check, not a guarantee that a file contains no secrets.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = frozenset({
    ".gitattributes", ".gitignore", "AGENTS.md", "Cargo.toml", "Cargo.lock",
    "rust-toolchain.toml", "README.md", "README.en.md", "CONTRIBUTING.md",
    "SECURITY.md", "LICENSE", "LICENSE.md", "providers.example.toml",
    "requirements.txt", "requirements-dev.txt", "pytest.ini", "package.json",
    "package-lock.json", "playwright.config.js", "bootstrap.py", "bootstrap.bat",
    "manage.py", "rust_launcher.py", "start.bat", "gui.bat", "status.bat",
    "configure_codex.bat", "set_keys.bat", "test.bat", "stop.bat", "stop_gui.bat",
    "start_background.bat", "run_proxy.py", "run_gui.py", "start_gui.py",
    "start_background.py", "stop_proxy.py", "smoke_test.py",
    "scripts/start.ps1", "scripts/serve_demo.py", "scripts/check_live.py",
    "scripts/test_mcp.py", "scripts/test_rust_proxy.py", "scripts/package_source.py",
    "scripts/package_release.py", "scripts/scan_repository.py", "scripts/verify_release.py",
    "examples/codex-mcp.toml", "examples/backend-handoff.md", "examples/ui-contract.json",
    "legacy/start.bat", "legacy/README.md",
    "dashboard/assets/vendor/THREE-LICENSE.txt",
    "dashboard/assets/vendor/HELVETIKER-LICENSE.txt",
    "dashboard/assets/vendor/README.md",
    "dashboard/assets/vendor/three.module.min.js",
    "dashboard/assets/vendor/three.core.min.js",
    "dashboard/assets/vendor/FontLoader.js",
    "dashboard/assets/vendor/TextGeometry.js",
    "dashboard/assets/vendor/RoomEnvironment.js",
    "dashboard/assets/vendor/helvetiker_bold.typeface.json",
    ".github/dependabot.yml", ".github/workflows/ci.yml", ".github/workflows/security.yml",
})
# A prefix and a suffix must both match; adding a file to Git grants no permission.
SOURCE_TREES = {
    "src": frozenset({".rs"}),
    "dashboard": frozenset({".py"}),
    "dashboard/assets": frozenset({".js", ".css", ".html", ".svg", ".png", ".webp", ".woff", ".woff2"}),
    "proxy": frozenset({".py"}),
    "tests": frozenset({".py"}),
    "tests/browser": frozenset({".js", ".mjs"}),
    "tests/fixtures": frozenset({".json", ".toml", ".txt"}),
    "docs": frozenset({".md"}),
    "docs/images": frozenset({".svg", ".png", ".jpg", ".jpeg", ".webp"}),
}
RUNTIME_FILES = frozenset({
    "bootstrap.py", "bootstrap.bat", "manage.py", "rust_launcher.py", "requirements.txt",
    "providers.example.toml", "start.bat", "gui.bat", "status.bat", "set_keys.bat",
    "configure_codex.bat",
    "scripts/start.ps1", "examples/codex-mcp.toml", "README.md", "README.en.md",
    "SECURITY.md", "CONTRIBUTING.md", "LICENSE", "LICENSE.md",
})
PRIVATE_PARTS = frozenset({
    ".git", ".venv", "venv", "env", ".local", ".artifacts", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    "data", "logs", "backups", "target", "node_modules", "__pycache__",
    "playwright-report", "test-results", "dist", "build",
})
PRIVATE_NAMES = frozenset({
    "providers.toml", ".env", "python311.exe", "credentials.json", "auth.json",
    "thumbs.db", ".ds_store",
})
PRIVATE_SUFFIXES = frozenset({
    ".pem", ".key", ".pfx", ".p12", ".dpapi", ".pyc", ".pyo", ".log",
    ".bak", ".tmp", ".db", ".sqlite", ".sqlite3", ".dmp", ".pdb",
})
PATTERNS = {
    "OpenAI-like credential": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "GitHub credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
SOURCE_METADATA = frozenset({"SOURCE_MANIFEST.json", "SHA256SUMS", "3api-source.zip"})
RELEASE_METADATA = frozenset({"RELEASE_MANIFEST.json", "SHA256SUMS", "3api-windows-x64.zip"})
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def path_reason(relative: str) -> str | None:
    """Reject paths that would be ambiguous or unsafe when unpacked on Windows."""
    parts = PurePosixPath(relative).parts
    if (not relative or relative.startswith("/") or "\\" in relative or ":" in relative
            or any(ord(char) < 32 for char in relative) or "//" in relative
            or any(part in {".", ".."} for part in relative.split("/"))):
        return "unsafe path"
    if any(part.endswith((" ", ".")) for part in parts):
        return "ambiguous Windows path"
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if any(part.split(".", 1)[0].lower() in reserved for part in parts):
        return "reserved Windows path"
    if PRIVATE_PARTS.intersection(part.lower() for part in parts):
        return "private directory"
    name = parts[-1].lower() if parts else ""
    if (name in PRIVATE_NAMES or name.startswith(".env.")
            or PurePosixPath(name).suffix in PRIVATE_SUFFIXES
            or ".sqlite" in name or name.endswith((".db-wal", ".db-shm"))):
        return "private file"
    return None


def source_allowed(relative: str) -> bool:
    if path_reason(relative):
        return False
    if relative in SOURCE_FILES:
        return True
    if relative.startswith("dashboard/assets/vendor/"):
        return False  # Vendored dependencies and licenses are reviewed file by file.
    path = PurePosixPath(relative)
    if any(part.startswith(".") for part in path.parts):
        return False
    return any(relative.startswith(prefix + "/") and path.suffix.lower() in suffixes
               for prefix, suffixes in SOURCE_TREES.items())


def runtime_allowed(relative: str) -> bool:
    return source_allowed(relative) and (
        relative in RUNTIME_FILES or relative.startswith(("dashboard/", "proxy/", "docs/"))
    )


def delivery_allowed(relative: str) -> bool:
    if path_reason(relative):
        return False
    prefix = "releases/windows-x64/"
    if relative.startswith(prefix):
        name = relative[len(prefix):]
        return name == "3api.exe" or name in RELEASE_METADATA or runtime_allowed(name)
    return relative in SOURCE_METADATA or source_allowed(relative)


def candidates(root: Path = ROOT) -> list[str]:
    """Read allowlisted source paths without walking dependency or runtime trees."""
    root = root.resolve()
    allowed_tops = {PurePosixPath(path).parts[0] for path in SOURCE_FILES | SOURCE_TREES.keys()}
    result = []
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        kept = []
        for name in directories:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if (name.lower() in PRIVATE_PARTS or (base == root and name not in allowed_tops)
                    or (name.startswith(".") and relative != ".github")):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"Source path is a link or escapes the checkout: {relative}")
            kept.append(name)
        directories[:] = kept
        for name in files:
            relative = (base / name).relative_to(root).as_posix()
            if source_allowed(relative):
                result.append(relative)
    return sorted(result)


def content_findings(relative: str, raw: bytes) -> list[tuple[str, str]]:
    if b"\0" in raw[:4096]:
        return []
    text = raw.decode("utf-8", errors="replace")
    return [(relative, label) for label, pattern in PATTERNS.items() if pattern.search(text)]


def archive_findings(path: Path, relative: str) -> list[tuple[str, str]]:
    findings = []
    source = path.name == "3api-source.zip"
    prefix = "3api/" if source else "3api-windows-x64/"
    allowed = source_allowed if source else runtime_allowed
    try:
        with zipfile.ZipFile(path) as archive:
            seen = set()
            size = 0
            for item in archive.infolist():
                name = item.filename
                label = relative + "!" + name
                size += item.file_size
                if size > MAX_ARCHIVE_BYTES:
                    findings.append((relative, "archive exceeds the unpacked size limit"))
                    break
                if (name.casefold() in seen or not name.startswith(prefix)
                        or path_reason(name) or stat.S_ISLNK(item.external_attr >> 16)):
                    findings.append((label, "unsafe or duplicate archive entry"))
                    continue
                seen.add(name.casefold())
                child = name[len(prefix):]
                metadata = {"SOURCE_MANIFEST.json"} if source else {"3api.exe", "RELEASE_MANIFEST.json"}
                if item.is_dir() or not (allowed(child) or child in metadata):
                    findings.append((label, "entry outside the archive allowlist"))
                    continue
                if child.endswith(".exe"):
                    continue
                if item.file_size > MAX_TEXT_BYTES:
                    findings.append((label, "oversized source artifact"))
                else:
                    findings.extend(content_findings(label, archive.read(item)))
    except (OSError, zipfile.BadZipFile, RuntimeError):
        findings.append((relative, "unreadable ZIP archive"))
    return findings


def scan_paths(root: Path, paths: list[str], *, delivery: bool = False) -> list[tuple[str, str]]:
    root = root.resolve()
    findings = []
    seen = set()
    for relative in paths:
        path = root / relative
        reason = path_reason(relative)
        if reason:
            findings.append((relative, reason))
            continue
        if relative.casefold() in seen:
            findings.append((relative, "case-insensitive duplicate path"))
            continue
        seen.add(relative.casefold())
        if not (delivery_allowed(relative) if delivery else source_allowed(relative)):
            findings.append((relative, "file outside the allowlist"))
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.append((relative, "symlink or path escape"))
            continue
        if not path.is_file():
            findings.append((relative, "not a regular file"))
            continue
        if path.suffix == ".zip":
            findings.extend(archive_findings(path, relative))
        elif relative == "releases/windows-x64/3api.exe":
            # Executable architecture and hashes are checked by verify_release.py.
            continue
        elif path.stat().st_size > MAX_TEXT_BYTES:
            findings.append((relative, "oversized source artifact"))
        else:
            findings.extend(content_findings(relative, path.read_bytes()))
    return findings


def tree_paths(root: Path) -> list[str]:
    """Inventory an output, retaining links as findings rather than following them."""
    root = root.resolve()
    result = []
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(directories):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path_reason(relative):
                result.append(relative)
                directories.remove(name)
            elif not any(path.iterdir()):
                # A generated package never needs an empty directory. Do not silently
                # preserve empty private/cache folders in a previously used output.
                result.append(relative)
        result.extend((base / name).relative_to(root).as_posix() for name in files)
    return sorted(result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Scan every file in a prepared GitHub delivery")
    args = parser.parse_args(argv)
    root = (args.root or ROOT).resolve()
    if not root.is_dir():
        print("FAIL: scan root does not exist")
        return 1
    try:
        paths = tree_paths(root) if args.root else candidates(root)
        findings = scan_paths(root, paths, delivery=bool(args.root))
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    for path, reason in findings:
        print(f"FAIL {path}: {reason}")
    print(f"Checked {len(paths)} files; {len(findings)} findings. Pattern scan is not a proof of no secrets.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
