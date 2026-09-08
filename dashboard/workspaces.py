"""Validate project locations and create private working folders for chats.

The temporary working folder is deliberately not cleaned up on shutdown. Chat
history is held by DashboardStore, and the same folder is reused on later runs.
Removing a project from the panel never calls a filesystem deletion helper.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile


def _name(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Nazwa może mieć do 120 znaków i nie może zawierać znaków sterujących.")
    return value.strip() or None


def _plain_directory(path):
    """Do not create a managed workspace through a symlink or Windows junction."""
    if path.exists() or path.is_symlink():
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Folder roboczy czatu nie może być dowiązaniem.")
    path.mkdir(exist_ok=True)
    if not path.is_dir() or path.resolve() != path:
        raise ValueError("Folder roboczy czatu jest niedostępny.")


def prepare_workspace(body, data_dir):
    """Return the metadata to persist; only chat creation allocates a directory."""
    if not isinstance(body, dict):
        raise ValueError("Niepoprawne dane projektu lub czatu.")
    kind = body.get("kind", "project")
    if kind not in ("project", "chat"):
        raise ValueError("Wybierz projekt albo czat.")
    name = _name(body.get("name"))
    if kind == "project":
        if body.get("access_mode", "project") != "project":
            raise ValueError("Zakres dostępu wybierz w uprawnieniach zadania projektu.")
        folder = body.get("path")
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("Wybierz istniejący folder projektu.")
        path = Path(folder).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Wybierz istniejący folder projektu.")
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError("Wybierz folder projektu, zamiast całego dysku lub katalogu użytkownika.")
        return {"path": str(path), "name": name or path.name, "kind": kind, "access_mode": "project"}

    access = body.get("access_mode", "isolated")
    if access not in ("isolated", "full"):
        raise ValueError("Wybierz folder tymczasowy albo pełny dostęp do komputera.")
    if access == "full" and body.get("full_access_confirmed") is not True:
        raise ValueError("Potwierdź pełny dostęp czatu do komputera.")
    if body.get("path") not in (None, ""):
        raise ValueError("Folder rozmowy jest tworzony automatycznie. Własny folder dodaj jako projekt.")

    # Distinct installations do not reuse one another's chat files. Neither a
    # user-entered name nor any request-supplied path is used as a path segment.
    installation = os.path.normcase(str(Path(data_dir).resolve()))
    identity = hashlib.sha256(installation.encode("utf-8")).hexdigest()[:24]
    base = Path(tempfile.gettempdir()).resolve(strict=True) / "3api-chats"
    _plain_directory(base)
    private = base / identity
    _plain_directory(private)
    path = Path(tempfile.mkdtemp(prefix="chat-", dir=private)).resolve(strict=True)
    if path.parent != private:
        raise ValueError("Nie można bezpiecznie utworzyć folderu rozmowy.")
    return {"path": str(path), "name": name or "Czat", "kind": kind, "access_mode": access}


def workspace_permission(project, requested_permission):
    """A chat's explicit scope wins over saved global/composer defaults."""
    if project.get("kind", "project") == "chat":
        access = project.get("access_mode", "isolated")
        if access == "isolated":
            return "approval"
        if access == "full":
            return "yolo"
        raise ValueError("Czat ma niepoprawny zakres dostępu.")
    if requested_permission not in ("approval", "yolo"):
        raise ValueError("Wybierz poprawne uprawnienia zadania.")
    return requested_permission
