> 📷 **Your Discord images:** `DISCORD_IMAGE_1_URL` · `DISCORD_IMAGE_2_URL`<br>
> Replace these placeholders with two direct links to your images. Local application demo screenshots follow below.

<p align="center"><img src="docs/images/cover.svg" alt="3api — your local AI workspace" width="100%"></p>

<p align="center"><a href="README.md">Polski</a> · <b>English</b> · <a href="docs/STARTUP.md">Startup</a> · <a href="docs/GALLERY.md">Gallery</a> · <a href="docs/MCP.md">MCP</a> · <a href="docs/ARCHITECTURE.md">Architecture</a> · <a href="docs/VERIFICATION.md">Verification</a></p>

# Your projects. Your providers. One workspace.

**3api** brings Codex conversations, parallel tasks, provider activity and code changes into one local dashboard. Rust handles HTTP, the Responses proxy and MCP. A private Python worker runs tasks and saves their history.

Open **http://127.0.0.1:4101/ui/** and start working. The dashboard establishes a local session automatically — no token entry.

![3api dashboard in English](docs/images/dashboard-en.png)

*The interface uses local demonstration data. Projects, providers and statistics illustrate the application; they are not measurements of paid services.*

## A workspace that remembers the work

| | What it does |
| --- | --- |
| **Persistent chats** | A new chat is saved immediately. Each project remembers its chat, provider selection and message draft. |
| **Visible progress** | Chat and project spinners indicate activity. Waiting, finalizing, completed, failed and interrupted runs have distinct states. |
| **Global provider activity** | See main, auxiliary and available API connections while viewing any project. |
| **Run history** | Each instruction keeps its own duration, actual agents, API usage and file changes. |
| **Persistent changes** | Diffs and project summaries remain available after refresh. Each run measures changes from its own starting point. |
| **Project or conversation** | Work in an existing project or create a separate chat with a chosen access scope. Removing an entry from the panel preserves files on disk. |
| **Ready-to-edit settings** | Work and task-delegation prompts are visible in settings. Each API has its own token budget and switching thresholds. |
| **Polish / English** | Switch the interface language while preserving conversation text, project names and code. |
| **MCP** | A trusted client reads status, usage and metadata through the same Rust executable. |

## Run from source on Windows

Open the project directory or extract `GitHub/3api-source.zip`. You need **Python 3.11+**, **Rust stable**, **MSVC Build Tools**, and **Codex CLI** in `PATH` for coding tasks. Run:

```powershell
.\start.bat
```

1. Open **http://127.0.0.1:4101/ui/**. The local session starts automatically.
2. Add a provider with its endpoint, exact model or deployment, and key.
3. Add a project directory or a chat, then choose compatible providers.
4. Submit an instruction. A follow-up continues the conversation and creates another history entry.

First startup creates `.venv`, installs Python dependencies and builds the Rust executable. On later starts, the launcher detects newer source files and rebuilds an outdated binary. Downloading dependencies requires internet access. Node.js is needed for browser tests, not everyday dashboard use. Finish active work and restart an already-running instance to use backend changes.

The **GitHub** folder is a prepared source export with code, documentation, screenshots and checksums. It does not require a prebuilt EXE. The optional Windows release process, including its worker and bootstrap files, is documented in [docs/RELEASE.md](docs/RELEASE.md).

Keep the server terminal open. **Ctrl+C** stops this instance. [Startup and troubleshooting](docs/STARTUP.md).

## One chat, a history of individual runs

A project can contain multiple chats. A chat stores the conversation; every accepted instruction creates a separate run (`run_id`). Continuing preserves earlier results. Select a run in history to inspect its changes and API events.

| State | Meaning |
| --- | --- |
| **Working** | The agent is executing a task; the chat and project show an animated spinner. |
| **Waiting** | A question or approval needs your response. |
| **Finalizing** | Results and changes are still being saved. |
| **Completed** | Final results have been saved; only this state receives a green check. |
| **Failed / interrupted** | The run ended without confirmed success; its history remains available. |

**Files, added lines and removed lines** describe the difference from the selected run's starting point. Touched files and change history also retain observed edits that were later reverted. Refreshing does not count the same diff again. Missing historical measurements remain unavailable.

Drafts and selection preferences are stored in this browser. Conversations, runs and results live in the server's local data directory. Restart with the same `data/rust` directory to return to saved history.

A separate **chat** receives its own folder under the system temporary directory. Choose approval-based work in that folder, or explicitly enable full computer access. The conversation folder survives refreshes and application restarts. If system cleanup removes it, history remains, but further work requires an existing folder. Removing a project from the list archives it in the application; adding the same directory again restores its history. Active work prevents removal.

## Multiple providers, shared capacity

In regular **standard / ultra** mode, the main client first chooses a free compatible API, then an API used only by auxiliary work, then the least loaded available API. Auxiliary threads prefer sharing an API already used for auxiliary work. Assignments are global across projects and persist between requests from the same thread.

The dashboard shows roles and project assignments. Selecting two APIs does not itself create two active agents: the count comes from actual threads. Model compatibility, cooldowns and waiting limits still apply. The separate **experimental team mode** retains work copies and merge checks.

Each API defaults to **1,000,000 tokens per minute**, a threshold of **900,000** for preferring another API, and **950,000** for deferring new requests. These settings control local traffic allocation; the provider determines the actual account quota. You can edit the limits for each connection.

The budget includes the last 60 seconds of reported usage and reservations for in-flight requests. Missing reports use a clearly marked estimate. Crossing the soft threshold prefers another compatible API; the hard threshold defers new requests without interrupting an existing response. Traffic outside this proxy is not visible to its counters.

## Prompts you can read and edit

New installations show the main-agent and coordinator instructions in settings immediately. They cover inspecting the project, preserving user work, assigning files to agents, saving progress, checking state after reconnects, and reporting actual tests. Saved custom instructions, including intentionally empty values, are preserved and remain editable.

In the experimental team, the Ultra coordinator defines a shared contract and two separate work packages without editing the project. The other two APIs run Ultra workers in independent copies, while the coordinator's API becomes a reserve. Shared files have one owner and results pass through merge checks. Tests requiring both work packages must run against the combined result. This is separate from regular ultra mode.

<details><summary><b>Explore default prompts, limits and standalone chats</b></summary>

![Default main-agent and coordinator prompts](docs/images/settings-prompts-en.png)
![Per-API token thresholds](docs/images/provider-limits-en.png)
![A new chat and its access scope](docs/images/workspace-chat-en.png)

</details>

[Full gallery: 8 local demo screenshots](docs/GALLERY.md).

![An archived run with its saved diff](docs/images/history-en.png)

<details><summary><b>Mobile view</b></summary>
<p align="center"><img src="docs/images/mobile-pl.png" alt="3api mobile dashboard in Polish" width="360"></p>
</details>

## Connect MCP

Example for an executable built from source in `C:\Apps\3api`:

```toml
[mcp_servers.three_api]
command = 'C:\Apps\3api\target\release\3api.exe'
args = ['mcp', '--config', 'C:\Apps\3api\providers.toml', '--data-dir', 'C:\Apps\3api\data\rust']
startup_timeout_sec = 10
tool_timeout_sec = 15
```

MCP uses **stdio** and exposes `3api_status`, `3api_providers`, `3api_usage`, `3api_projects`, and `3api_tasks`. It reads bounded metadata; it cannot start tasks or return prompts, diffs or credentials. Saved history is readable without the dashboard running; live proxy status requires a running proxy.

Merge the example into your chosen client configuration, preserving other settings. Starting 3api and preparing the GitHub folder do not modify global Codex configuration. [MCP guide](docs/MCP.md) · [Example file](examples/codex-mcp.toml) · [Official OpenAI documentation](https://developers.openai.com/codex/mcp).

## Source development and tests

Building from source requires **Rust stable** and **MSVC Build Tools** on Windows. In the source directory, `start.bat` prepares Python and rebuilds a missing or outdated executable. You can force a rebuild with `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -Build`.

```powershell
.\bootstrap.bat
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
cargo fmt --all --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/test_rust_proxy.py
.\.venv\Scripts\python.exe scripts/test_mcp.py
npm ci --ignore-scripts
npx playwright install chromium
npm run test:ui
```

Tests use local mocks and separate directories. A passing mock test does not confirm live API access or account quotas. **Checks actually performed and remaining limitations:** [docs/VERIFICATION.md](docs/VERIFICATION.md). [Contributing](CONTRIBUTING.md).

## Prepare the GitHub folder

Run from the source directory. The normal **`./GitHub`** folder is excluded from this checkout's Git index and is never recursively packaged into itself. `.github` is the separate workflow configuration directory.

```powershell
.\.venv\Scripts\python.exe scripts/package_source.py --output-dir ./GitHub
.\.venv\Scripts\python.exe scripts/scan_repository.py --root ./GitHub
.\.venv\Scripts\python.exe scripts/verify_release.py --root ./GitHub --source-only
```

Packaging uses an explicit allowlist for sources, assets, examples, tests and documentation. Private `providers.toml`, runtime data, logs, backups, environments, `node_modules`, caches and `target` stay local. The export includes a ZIP archive, manifest and `SHA256SUMS`. The script rejects unknown destination files and links. It does not publish a repository or modify the Git index.

[Release process](docs/RELEASE.md). A project-wide license has not yet been selected; vendored Three.js retains its [MIT license](dashboard/assets/vendor/THREE-LICENSE.txt).

## Local data and security boundaries

| Component | Default |
| --- | --- |
| Dashboard | `http://127.0.0.1:4101/ui/` |
| Responses proxy | `http://127.0.0.1:4100/v1` |
| Private worker | Random loopback port with a separate secret |
| History and telemetry | `data/rust/` |
| Local configuration | `providers.toml` |
| Windows credentials | Credential Manager or configured environment variables |

Automatic sessions preserve **Host/Origin** validation, **CSRF** and **HttpOnly / SameSite** cookies. The internal proxy token is prepared automatically. This application is designed for a trusted user working on their own computer.

Codex runs with the selected permissions. The default mode requests approval for additional operations; **YOLO** deliberately enables full access. History can contain private content. Installations using matching provider IDs may share Windows Credential Manager entries.

[Security model](docs/SECURITY.md) · [Migration and data](docs/MIGRATION.md) · [Vulnerability reporting](SECURITY.md)
