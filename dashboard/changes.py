"""Compare a task's source files with its own baseline, including shell edits."""
from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

EXCLUDED = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
            ".next", ".nuxt", ".cache", "dist", "build", "coverage", "logs", "backups", "target", "vendor"}
PANEL_DATA = Path(__file__).resolve().parents[1] / "data"
PRIVATE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".dpapi", ".sqlite3", ".sqlite", ".db", ".pyc"}
MAX_FILES, MAX_FILE_BYTES, MAX_TEXT_BYTES = 8000, 1024 * 1024, 24 * 1024 * 1024


def snapshot(root: Path):
    if not root.is_dir():
        raise OSError("Project directory is unavailable")
    result, total, incomplete = {}, 0, []

    def unreadable(error):
        try:
            incomplete.append(Path(error.filename).relative_to(root).as_posix())
        except (TypeError, ValueError):
            incomplete.append("")

    for folder, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
        kept = []
        for name in sorted(dirs):
            path = Path(folder) / name
            if name.lower() in EXCLUDED or path == PANEL_DATA or path.is_symlink():
                continue
            try:
                if getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
            except OSError:
                incomplete.append(path.relative_to(root).as_posix())
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = Path(folder) / name
            if name.lower().startswith(".env") or path.suffix.lower() in PRIVATE_SUFFIXES or path.is_symlink():
                continue
            if len(result) >= MAX_FILES:
                return {"files": result, "skipped": len(incomplete) + 1, "incomplete_paths": [*incomplete, ""]}
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    incomplete.append(path.relative_to(root).as_posix())
                    continue
                raw = path.read_bytes()
            except OSError:
                incomplete.append(path.relative_to(root).as_posix())
                continue
            record = {"hash": hashlib.sha256(raw).hexdigest(), "text": None}
            if b"\0" not in raw and total + len(raw) <= MAX_TEXT_BYTES:
                try:
                    record["text"] = raw.decode("utf-8-sig")
                    total += len(raw)
                except UnicodeDecodeError:
                    pass
            result[path.relative_to(root).as_posix()] = record
    return {"files": result, "skipped": len(incomplete), "incomplete_paths": incomplete}


def save_baseline(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_baseline(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def compare(before, after):
    changes = []
    old, new = before["files"], after["files"]
    old_unknown = before.get("incomplete_paths", [""] if before.get("skipped") else [])
    new_unknown = after.get("incomplete_paths", [""] if after.get("skipped") else [])

    def unknown(name, paths):
        return any(not path or name == path or name.startswith(path.rstrip("/") + "/") for path in paths)

    for name in sorted(old.keys() | new.keys()):
        a, b = old.get(name), new.get(name)
        if a and b and a["hash"] == b["hash"]:
            continue
        # A bounded scan must not turn unscanned files into apparent deletions.
        if a and not b and unknown(name, new_unknown):
            continue
        if b and not a and unknown(name, old_unknown):
            continue
        kind = "nowy" if a is None else "usunięty" if b is None else "zmieniony"
        atext, btext = a["text"] if a else "", b["text"] if b else ""
        added = removed = 0
        binary = atext is None or btext is None
        if binary:
            diff = "Plik binarny lub zbyt duży do podglądu tekstowego."
        else:
            left, right = atext.splitlines(), btext.splitlines()
            for opcode, i, j, x, y in difflib.SequenceMatcher(None, left, right).get_opcodes():
                if opcode in {"replace", "delete"}:
                    removed += j - i
                if opcode in {"replace", "insert"}:
                    added += y - x
            diff = "\n".join(difflib.unified_diff(left, right, fromfile="a/" + name, tofile="b/" + name, lineterm=""))
        changes.append({"path": name, "kind": kind, "added": added, "removed": removed, "binary": binary,
                        "diff": diff})
    return {"changes": changes, "files": len(changes), "added": sum(c["added"] for c in changes),
            "removed": sum(c["removed"] for c in changes), "scan_skipped": before.get("skipped", 0) + after.get("skipped", 0),
            "scan_incomplete_paths": sorted(set(old_unknown + new_unknown))}
