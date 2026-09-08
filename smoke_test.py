"""Compatibility entry point for the Rust smoke checker; live requests are opt-in."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "scripts" / "check_live.py"), run_name="__main__")
