"""Stop only the verified proxy process belonging to this checkout."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent


def same_path(value, expected):
    if not value:
        return False
    return os.path.normcase(os.path.abspath(value)) == os.path.normcase(str(expected.resolve()))


def is_entrypoint(process, script):
    arguments = process.cmdline()
    return len(arguments) >= 2 and Path(arguments[1]).is_absolute() and same_path(arguments[1], script)


def owns_proxy(process, root=ROOT):
    script, python = root / "run_proxy.py", root / ".venv" / "Scripts" / "python.exe"
    if not is_entrypoint(process, script):
        return False
    if same_path(process.exe(), python):
        return True
    # On Windows the venv launcher may own a child running the base interpreter.
    parent = process.parent()
    return parent is not None and same_path(parent.exe(), python) and is_entrypoint(parent, script)


def listeners(connections):
    return {c.pid for c in connections(kind="tcp4") if c.status == psutil.CONN_LISTEN
            and c.laddr and c.laddr.ip == "127.0.0.1" and c.laddr.port == 4000}


def stop_proxy(*, root=ROOT, check_only=False, connections=psutil.net_connections, process_factory=psutil.Process):
    owners = listeners(connections)
    if not owners:
        return "Proxy juz jest zatrzymane. Nie zatrzymano zadnego procesu."
    if len(owners) != 1 or None in owners:
        raise RuntimeError("Nie mozna jednoznacznie wskazac procesu na porcie 4000. Niczego nie zatrzymano.")
    process = process_factory(next(iter(owners)))
    # psutil.Process retains process creation time and checks PID reuse before
    # terminate(). No stale PID file, shell command or arbitrary process name is used.
    if not owns_proxy(process, Path(root)):
        raise RuntimeError("Port 4000 zajmuje inny program lub inna kopia projektu. Niczego nie zatrzymano.")
    if check_only:
        return f"Potwierdzono proxy tego projektu. PID: {process.pid}. Proces nadal dziala."
    process.terminate()
    process.wait(timeout=10)
    if listeners(connections):
        raise RuntimeError("Po zatrzymaniu proxy port 4000 nadal jest zajety. Nie zatrzymano kolejnego procesu.")
    return f"Proxy zatrzymane. PID: {process.pid}. Port 4000 jest wolny."


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zatrzymaj lokalne proxy tego projektu")
    parser.add_argument("--check", action="store_true", help="Sprawdz wlasciciela procesu bez zatrzymywania")
    args = parser.parse_args(argv)
    try:
        print(stop_proxy(check_only=args.check))
        return 0
    except (psutil.Error, OSError, RuntimeError) as exc:
        print("Nie zatrzymano proxy:", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
