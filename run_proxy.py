"""Compatibility entry point: public API is implemented by the Rust binary."""
from rust_launcher import launch


def main():
    return launch("proxy")


if __name__ == "__main__":
    raise SystemExit(main())
