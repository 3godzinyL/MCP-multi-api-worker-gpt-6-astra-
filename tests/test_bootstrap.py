import os
import shutil
import subprocess
from pathlib import Path

import pytest

import bootstrap


@pytest.mark.skipif(os.name != "nt", reason="Windows copied venv regression")
def test_copied_venv_with_missing_base_python_is_backed_up_and_rebuilt(tmp_path):
    root = tmp_path / "copied project with spaces"
    environment = root / ".venv"
    scripts = environment / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(bootstrap.ROOT / ".venv" / "Scripts" / "python.exe", scripts / "python.exe")
    config = "home = C:\\nonexistent-proxy-test-python\\Python311\nversion = 3.11.9\n"
    (environment / "pyvenv.cfg").write_text(config)
    (environment / ".deps-ready").write_text("")
    assert not bootstrap.environment_works(root)

    python = bootstrap.ensure_environment(root)

    assert bootstrap.environment_works(root)
    backups = list((root / "backups").glob("venv-*"))
    assert len(backups) == 1
    assert (backups[0] / "pyvenv.cfg").read_text() == config
    assert not (environment / ".deps-ready").exists()
    result = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True, timeout=15)
    assert result.returncode == 0
    bootstrap.ensure_environment(root)
    assert list((root / "backups").glob("venv-*")) == backups


def test_legacy_or_changed_dependency_marker_requires_install(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "requirements.txt").write_text("httpx==0.28.1\n")
    marker = tmp_path / ".venv" / ".deps-ready"
    marker.write_text("")
    assert not bootstrap.dependencies_ready(tmp_path, Path("unused"))
    marker.write_text("outdated-requirements-fingerprint")
    assert not bootstrap.dependencies_ready(tmp_path, Path("unused"))


def test_corrupted_dependency_marker_can_be_repaired(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "requirements.txt").write_text("httpx==0.28.1\n")
    (tmp_path / ".venv" / ".deps-ready").write_bytes(b"\xff\xfe")
    assert not bootstrap.dependencies_ready(tmp_path, Path("unused"))
