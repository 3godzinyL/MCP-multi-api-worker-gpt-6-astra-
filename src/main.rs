use anyhow::{bail, Context, Result};
use clap::{Args, Parser, Subcommand};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use three_api::{gateway, mcp, proxy};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, BufReader},
    net::TcpListener,
};

#[derive(Parser)]
#[command(
    name = "3api",
    version,
    about = "Local Rust Responses gateway and Codex MCP server"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Args, Clone)]
struct Files {
    #[arg(long, default_value = "providers.toml")]
    config: PathBuf,
    /// Separate runtime data; the copied original history remains in data/.
    #[arg(long, default_value = "data/rust")]
    data_dir: PathBuf,
}

#[derive(Subcommand)]
enum Command {
    /// Start the Rust proxy, local browser gateway and private Python worker.
    Serve {
        #[command(flatten)]
        files: Files,
        #[arg(long, default_value_t = 4100)]
        proxy_port: u16,
        #[arg(long, default_value_t = 4101)]
        panel_port: u16,
        #[arg(long)]
        project_dir: Option<PathBuf>,
        #[arg(long)]
        python: Option<PathBuf>,
        /// Deadline for the private worker readiness message and health check.
        #[arg(long, default_value_t = 30, value_parser = clap::value_parser!(u64).range(1..=300))]
        worker_startup_timeout_seconds: u64,
    },
    /// Start only the Rust Responses API proxy.
    Proxy {
        #[command(flatten)]
        files: Files,
        #[arg(long, default_value_t = 4100)]
        port: u16,
    },
    /// Run the read-only MCP server over newline-delimited JSON-RPC on stdio.
    Mcp {
        #[command(flatten)]
        files: Files,
        #[arg(long, default_value = "http://127.0.0.1:4100")]
        proxy_url: String,
    },
    /// Validate configuration without contacting any provider or displaying credentials.
    Check {
        #[arg(long, default_value = "providers.toml")]
        config: PathBuf,
    },
    /// Display the internal API token in your terminal. The panel does not require it.
    Token {
        #[arg(long, default_value = "providers.toml")]
        config: PathBuf,
    },
}

fn local_token(config: &Path) -> Result<String> {
    let settings = proxy::load_settings(config)?;
    proxy::get_secret("local-proxy-token", &settings.proxy_token_env)?
        .filter(|s| !s.is_empty()).context("Local token is missing. Run .venv/Scripts/python.exe manage.py init on Windows, or set LOCAL_RESPONSES_PROXY_TOKEN.")
}

fn worker_root(project_dir: Option<PathBuf>) -> Result<PathBuf> {
    let root = if let Some(root) = project_dir {
        root
    } else {
        let current = std::env::current_dir()?;
        if current.join("dashboard/sidecar.py").is_file() {
            current
        } else {
            // A packaged EXE is accompanied by the Python worker. It may be
            // started from Explorer, a shortcut, or an unrelated directory.
            std::env::current_exe()?
                .parent()
                .context("Executable directory is unavailable")?
                .to_path_buf()
        }
    };
    let root = root
        .canonicalize()
        .context("Project directory does not exist")?;
    if !root.join("dashboard/sidecar.py").is_file() {
        bail!(
            "Python worker sources are missing; use the complete release or supply --project-dir"
        );
    }
    Ok(root)
}

fn relative_to(root: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    }
}

fn prepare_config(config: &Path, root: &Path) -> Result<()> {
    if config.is_file() {
        return Ok(());
    }
    // create_new preserves another startup's configuration, including edits.
    let template = std::fs::read(root.join("providers.example.toml"))
        .context("Configuration is missing, and providers.example.toml is unavailable")?;
    use std::io::Write;
    match std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(config)
    {
        Ok(mut file) => file
            .write_all(&template)
            .context("Cannot create the local provider configuration"),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(error).context("Cannot create the local provider configuration"),
    }
}

#[cfg(windows)]
mod worker_job {
    use anyhow::{bail, Context, Result};
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::{
        Foundation::{HANDLE, INVALID_HANDLE_VALUE},
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD,
                THREADENTRY32,
            },
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME},
        },
    };

    fn own(handle: HANDLE, context: &'static str) -> Result<OwnedHandle> {
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            return Err(std::io::Error::last_os_error()).context(context);
        }
        // SAFETY: a successful Windows creation call transferred this handle
        // to us. OwnedHandle closes it exactly once, including error paths.
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }

    pub struct WorkerJob(OwnedHandle);

    impl WorkerJob {
        pub fn new() -> Result<Self> {
            // An unnamed, non-inheritable Job belongs to this supervisor only.
            let job = Self(own(
                unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) },
                "Cannot create the private worker process group",
            )?);
            // SAFETY: this Windows POD structure accepts zero for unused limits.
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // SAFETY: limits points to a correctly sized, live structure.
            if unsafe {
                SetInformationJobObject(
                    job.0.as_raw_handle(),
                    JobObjectExtendedLimitInformation,
                    (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                    std::mem::size_of_val(&limits) as u32,
                )
            } == 0
            {
                return Err(std::io::Error::last_os_error())
                    .context("Cannot configure the private worker process group");
            }
            Ok(job)
        }

        pub fn attach_and_resume(&self, child: &tokio::process::Child) -> Result<()> {
            let process = child
                .raw_handle()
                .context("Worker process handle is unavailable")?;
            let process_id = child.id().context("Worker process is unavailable")?;
            // The child was created suspended: none of its descendants can
            // escape between process creation and assignment to this Job.
            if unsafe { AssignProcessToJobObject(self.0.as_raw_handle(), process) } == 0 {
                return Err(std::io::Error::last_os_error())
                    .context("Cannot isolate the private worker process group");
            }
            let snapshot = own(
                unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) },
                "Cannot inspect the suspended private worker",
            )?;
            // SAFETY: dwSize identifies a fully allocated Windows POD structure.
            let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
            entry.dwSize = std::mem::size_of_val(&entry) as u32;
            let mut found = unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } != 0;
            while found {
                if entry.th32OwnerProcessID == process_id {
                    let thread = own(
                        unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) },
                        "Cannot open the suspended private worker thread",
                    )?;
                    if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                        return Err(std::io::Error::last_os_error())
                            .context("Cannot resume the private worker");
                    }
                    return Ok(());
                }
                entry.dwSize = std::mem::size_of_val(&entry) as u32;
                found = unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } != 0;
            }
            bail!("The suspended private worker thread is unavailable")
        }
    }
}

async fn bind(port: u16) -> Result<TcpListener> {
    if port == 0 {
        bail!("Choose an explicit nonzero public port");
    }
    TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port))
        .await
        .context("Cannot bind loopback port; another installation may already be running")
}

async fn serve(
    files: Files,
    proxy_port: u16,
    panel_port: u16,
    project_dir: Option<PathBuf>,
    python: Option<PathBuf>,
    worker_startup_timeout_seconds: u64,
) -> Result<()> {
    if proxy_port == panel_port {
        bail!("Proxy and panel need different ports");
    }
    let root = worker_root(project_dir)?;
    let config = relative_to(&root, &files.config);
    let data_dir = relative_to(&root, &files.data_dir);
    // Bind public ports before touching runtime data or spawning the worker.
    let api_listener = bind(proxy_port).await?;
    let panel_listener = bind(panel_port).await?;
    prepare_config(&config, &root)?;
    let proxy_token = proxy::ensure_local_token(&config)?;
    let api = gateway::protected(
        proxy::router(config.clone(), data_dir.clone()).await?,
        proxy_port,
    );
    // Worker startup releases routes from saved runs through the API. Serve
    // those authenticated requests before waiting for worker health, otherwise
    // each side waits for the other. JoinSet also aborts the API on any startup
    // error so its listener never outlives this supervisor's serve attempt.
    let (shutdown, _) = tokio::sync::watch::channel(false);
    let mut servers = tokio::task::JoinSet::new();
    let mut api_stop = shutdown.subscribe();
    servers.spawn(async move {
        axum::serve(api_listener, api)
            .with_graceful_shutdown(async move {
                let _ = api_stop.changed().await;
            })
            .await
    });
    let sidecar_token =
        uuid::Uuid::new_v4().simple().to_string() + &uuid::Uuid::new_v4().simple().to_string();
    let executable = python
        .map(|path| relative_to(&root, &path))
        .unwrap_or_else(|| {
            root.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            })
        });
    if !executable.is_file() {
        bail!("Python worker environment is missing; run bootstrap.bat (Windows) or create .venv and install requirements.txt");
    }
    #[cfg(windows)]
    let worker_job = worker_job::WorkerJob::new()?;
    let mut command = tokio::process::Command::new(executable);
    command
        .args(["-B", "-m", "dashboard.sidecar", "--port", "0", "--config"])
        .arg(&config)
        .arg("--data-dir")
        .arg(&data_dir)
        .arg("--proxy-url")
        .arg(format!("http://127.0.0.1:{proxy_port}"))
        .current_dir(&root)
        .env("THREE_API_SIDECAR_TOKEN", &sidecar_token)
        .env("LOCAL_RESPONSES_PROXY_TOKEN", &proxy_token)
        .env("PYTHONUTF8", "1")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000004); // CREATE_NO_WINDOW | CREATE_SUSPENDED
    let mut worker = command
        .spawn()
        .context("Could not start private Python worker")?;
    #[cfg(windows)]
    worker_job.attach_and_resume(&worker)?;
    let startup_deadline =
        tokio::time::Instant::now() + Duration::from_secs(worker_startup_timeout_seconds);
    let stdout = worker.stdout.take().context("Worker pipe unavailable")?;
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    {
        // Bound before reading: a broken child cannot fill memory while the
        // supervisor waits for its single readiness line.
        let mut readiness = BufReader::new((&mut reader).take(1025));
        tokio::time::timeout_at(startup_deadline, readiness.read_line(&mut line))
            .await
            .context("Worker startup timed out")??;
    }
    if line.len() > 1024 {
        bail!("Invalid worker readiness message");
    }
    let ready: serde_json::Value =
        serde_json::from_str(&line).context("Worker did not report a private port")?;
    let worker_port = ready
        .get("port")
        .and_then(|n| n.as_u64())
        .filter(|n| *n > 0 && *n <= 65535)
        .context("Invalid private worker port")? as u16;
    // Drain only trusted worker output, never mirror its contents into public logs.
    let drain = tokio::spawn(async move {
        let mut sink = tokio::io::sink();
        let _ = tokio::io::copy(&mut reader, &mut sink).await;
    });
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()?;
    let mut healthy = false;
    while tokio::time::Instant::now() < startup_deadline {
        if worker.try_wait()?.is_some() {
            bail!("Python worker exited during startup");
        }
        if let Ok(Ok(response)) = tokio::time::timeout_at(
            startup_deadline,
            client
                .get(format!("http://127.0.0.1:{worker_port}/health"))
                .header("x-3api-sidecar-token", &sidecar_token)
                .send(),
        )
        .await
        {
            if response.status().is_success() {
                healthy = true;
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    if !healthy {
        bail!("Private worker health check failed");
    }
    let panel = gateway::protected(
        gateway::router(worker_port, sidecar_token.clone(), proxy_token)?,
        panel_port,
    );
    let mut panel_stop = shutdown.subscribe();
    servers.spawn(async move {
        axum::serve(panel_listener, panel)
            .with_graceful_shutdown(async move {
                let _ = panel_stop.changed().await;
            })
            .await
    });
    eprintln!("3api v{} | Rust proxy http://127.0.0.1:{proxy_port}/v1 | Panel http://127.0.0.1:{panel_port}/ui/", env!("CARGO_PKG_VERSION"));
    eprintln!(
        "MCP: 3api mcp --config <config> --data-dir <runtime data> | Ctrl+C to stop this instance"
    );
    let result = tokio::select! {
        signal = tokio::signal::ctrl_c() => signal.context("Cannot listen for shutdown signal"),
        status = worker.wait() => { let status = status?; if status.success() { Ok(()) } else { Err(anyhow::anyhow!("Private worker stopped unexpectedly")) } },
    };
    let _ = shutdown.send(true);
    // Owned child only; never search by port/PID or terminate another installation.
    if worker.try_wait()?.is_none() {
        let _ = client
            .post(format!("http://127.0.0.1:{worker_port}/internal/shutdown"))
            .header("x-3api-sidecar-token", &sidecar_token)
            .send()
            .await;
        if tokio::time::timeout(Duration::from_secs(20), worker.wait())
            .await
            .is_err()
        {
            let _ = worker.kill().await;
        }
    }
    drain.abort();
    while !servers.is_empty() {
        if tokio::time::timeout(Duration::from_secs(5), servers.join_next())
            .await
            .is_err()
        {
            servers.abort_all();
            break;
        }
    }
    result
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter("three_api=info")
        .without_time()
        .init();
    match Cli::parse().command {
        Command::Serve {
            files,
            proxy_port,
            panel_port,
            project_dir,
            python,
            worker_startup_timeout_seconds,
        } => {
            serve(
                files,
                proxy_port,
                panel_port,
                project_dir,
                python,
                worker_startup_timeout_seconds,
            )
            .await
        }
        Command::Proxy { files, port } => {
            let listener = bind(port).await?;
            prepare_config(&files.config, &std::env::current_dir()?)?;
            proxy::ensure_local_token(&files.config)?;
            let router =
                gateway::protected(proxy::router(files.config, files.data_dir).await?, port);
            eprintln!("3api Rust proxy: http://127.0.0.1:{port}/v1");
            axum::serve(listener, router)
                .with_graceful_shutdown(async {
                    let _ = tokio::signal::ctrl_c().await;
                })
                .await?;
            Ok(())
        }
        Command::Mcp { files, proxy_url } => {
            mcp::run(files.config, files.data_dir, proxy_url).await
        }
        Command::Check { config } => {
            let settings = proxy::load_settings(&config)?;
            println!("Configuration OK; public model: {}", settings.public_model);
            Ok(())
        }
        Command::Token { config } => {
            println!("{}", local_token(&config)?);
            Ok(())
        }
    }
}
