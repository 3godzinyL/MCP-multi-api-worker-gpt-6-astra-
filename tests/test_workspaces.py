from pathlib import Path

import pytest

from dashboard.workspaces import prepare_workspace, workspace_permission


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    root = tmp_path / "system-temp"
    root.mkdir()
    monkeypatch.setattr("dashboard.workspaces.tempfile.gettempdir", lambda: str(root))
    return root


def test_project_accepts_name_without_changing_contents(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    document = folder / "user.txt"
    document.write_text("User content", encoding="utf-8")
    result = prepare_workspace({"path": str(folder), "name": "  Moja praca  "}, tmp_path / "data")
    assert result == {"path": str(folder.resolve()), "name": "Moja praca", "kind": "project", "access_mode": "project"}
    assert document.read_text(encoding="utf-8") == "User content"


def test_chats_have_distinct_persistent_temporary_folders(temp_root, tmp_path):
    first = prepare_workspace({"kind": "chat", "name": "../rozmowa"}, tmp_path / "data")
    path = Path(first["path"])
    (path / "notes.txt").write_text("Keep me", encoding="utf-8")
    second = prepare_workspace({"kind": "chat", "name": "../rozmowa"}, tmp_path / "data")
    other = prepare_workspace({"kind": "chat"}, tmp_path / "other-data")
    assert first["access_mode"] == "isolated"
    assert first["path"] != second["path"]
    assert Path(second["path"]).parent == path.parent
    assert Path(other["path"]).parent != path.parent
    assert path.is_relative_to(temp_root)
    assert (path / "notes.txt").read_text(encoding="utf-8") == "Keep me"


@pytest.mark.parametrize("confirmed", [None, False, "true", 1])
def test_full_access_requires_explicit_boolean_before_creating_files(temp_root, tmp_path, confirmed):
    with pytest.raises(ValueError, match="Potwierdź"):
        prepare_workspace({"kind": "chat", "access_mode": "full", "full_access_confirmed": confirmed}, tmp_path)
    assert not list(temp_root.iterdir())


def test_full_access_still_uses_its_own_working_folder(temp_root, tmp_path):
    result = prepare_workspace({"kind": "chat", "access_mode": "full", "full_access_confirmed": True}, tmp_path)
    assert Path(result["path"]).is_relative_to(temp_root)
    assert workspace_permission(result, "approval") == "yolo"


@pytest.mark.parametrize("body", [
    {"kind": "other"}, {"kind": []}, {"path": []},
    {"kind": "chat", "name": "name\nsecret"},
    {"kind": "chat", "path": "C:/user-folder"},
    {"kind": "chat", "access_mode": []},
    {"kind": "chat", "access_mode": "unknown"},
    {"kind": "chat", "name": "a" * 121},
])
def test_invalid_requests_do_not_allocate_workspaces(temp_root, tmp_path, body):
    with pytest.raises(ValueError):
        prepare_workspace(body, tmp_path)
    assert not list(temp_root.iterdir())


def test_chat_scope_cannot_be_overridden_by_yolo_default():
    assert workspace_permission({"kind": "chat", "access_mode": "isolated"}, "yolo") == "approval"
    assert workspace_permission({"kind": "chat"}, "yolo") == "approval"
    assert workspace_permission({"kind": "project"}, "yolo") == "yolo"


def test_chat_folder_rejects_redirects(temp_root, tmp_path, monkeypatch):
    root = temp_root / "3api-chats"
    root.mkdir()
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root or original(path))
    with pytest.raises(ValueError, match="dowiązaniem"):
        prepare_workspace({"kind": "chat"}, tmp_path)
    assert not list(root.iterdir())
