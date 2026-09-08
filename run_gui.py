"""Compatibility entry point for the Rust gateway and private worker."""
from rust_launcher import launch


if __name__ == "__main__":
    raise SystemExit(launch("serve"))
