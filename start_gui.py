"""Compatibility launcher; the owned Rust supervisor runs in the foreground."""
from rust_launcher import launch


def main():
    return launch("serve")


if __name__ == "__main__":
    raise SystemExit(main())
