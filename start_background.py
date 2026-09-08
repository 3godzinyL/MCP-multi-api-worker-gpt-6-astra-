"""The new Rust supervisor deliberately stays attached to its terminal."""
from rust_launcher import launch


def main():
    return launch("proxy")


if __name__ == "__main__":
    raise SystemExit(main())
