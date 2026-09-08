# Development

Work in this checkout. Keep private configurations, data, credentials and generated build files out of Git.
Rust owns public HTTP and MCP. Python is the private Codex worker; preserve its protocol and file-merge semantics.
Use local mock providers for tests. Do not expose prompt or secret values in diagnostics.
After changes run the relevant tests from README.md. Keep PL and EN UI strings consistent and preserve user-entered content.
Coordinate shared-file ownership before parallel edits. Do not change global Codex configuration as part of tests.
