"""Create or repair this computer's venv; never reuse a broken copied environment."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def environment_works(root: Path) -> bool:
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-I", "-c",
             "import sys; from pathlib import Path; "
             "sys.exit(0 if sys.version_info >= (3, 11) and "
             "Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve() else 1)",
             str(root / ".venv")],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_environment(root: Path) -> Path:
    root = root.resolve()
    environment = root / ".venv"
    if environment_works(root):
        return environment / "Scripts" / "python.exe"
    if environment.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = root / "backups" / ("venv-" + stamp)
        # Validate both absolute targets before moving any directory on Windows.
        if (environment.is_symlink() or environment.resolve().parent != root
                or not backup.resolve().is_relative_to(root)):
            raise RuntimeError("Environment backup path must stay inside this project")
        backup.parent.mkdir(parents=True, exist_ok=True)
        environment.rename(backup)
        print("Copied or broken .venv preserved in:", backup, flush=True)
    base_python = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    print("Creating a local Python environment...", flush=True)
    subprocess.run([base_python, "-m", "venv", str(environment)], check=True)
    if not environment_works(root):
        raise RuntimeError("The new Python environment did not start")
    return environment / "Scripts" / "python.exe"


def dependencies_ready(root: Path, python: Path) -> bool:
    requirements = root / "requirements.txt"
    expected = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = root / ".venv" / ".deps-ready"
    try:
        installed = marker.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return False
    if installed != expected:
        return False
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", "import httpx, keyring, psutil, starlette, tomlkit, uvicorn"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    try:
        python = ensure_environment(ROOT)
        if not dependencies_ready(ROOT, python):
            subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                            "-r", str(ROOT / "requirements.txt")], check=True)
            fingerprint = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
            (ROOT / ".venv" / ".deps-ready").write_text(fingerprint + "\n", encoding="ascii")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Python setup failed:", type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
