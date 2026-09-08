"""Launch the Rust binary from a source checkout or an unpacked release."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def binary_path(root=ROOT):
    name = "3api.exe" if sys.platform == "win32" else "3api"
    packaged = root / name
    if packaged.is_file():
        return packaged
    return root / "target" / "release" / name


def ensure_binary(root=ROOT):
    executable = binary_path(root)
    if executable.is_file() and not source_is_newer(root, executable):
        return executable
    cargo = shutil.which("cargo")
    if not cargo:
        candidate = Path.home() / ".cargo" / "bin" / ("cargo.exe" if sys.platform == "win32" else "cargo")
        if candidate.is_file():
            cargo = str(candidate)
    if not (root / "Cargo.toml").is_file() or not cargo:
        raise RuntimeError("Brak 3api.exe. Rozpakuj kompletne wydanie albo zainstaluj Rust/Cargo dla zrodel.")
    subprocess.run([cargo, "build", "--locked", "--release"], cwd=root, check=True)
    if not executable.is_file():
        raise RuntimeError("Kompilacja nie utworzyla pliku 3api.")
    return executable


def source_is_newer(root, executable):
    """A source checkout must not silently keep launching an old Rust build."""
    if executable.parent == root or not (root / "Cargo.toml").is_file():
        return False
    built = executable.stat().st_mtime_ns
    inputs = [root / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")]
    inputs.extend((root / "src").rglob("*.rs"))
    return any(path.is_file() and path.stat().st_mtime_ns > built for path in inputs)


def launch(mode, arguments=None):
    args = list(sys.argv[1:] if arguments is None else arguments)
    try:
        executable = ensure_binary(ROOT)
        if mode == "serve":
            import bootstrap
            if bootstrap.main() != 0:
                return 1
            if "--project-dir" not in args and not any(arg.startswith("--project-dir=") for arg in args):
                args += ["--project-dir", str(ROOT)]
        return subprocess.run([str(executable), mode, *args], cwd=ROOT, check=False).returncode
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Nie mozna uruchomic 3api:", str(exc), file=sys.stderr)
        return 1
