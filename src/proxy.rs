//! Rust Responses proxy. Authentication, routing and upstream I/O stay in this process.
mod config;
mod credentials;
mod network;
mod runtime;
mod sse;
mod telemetry;
mod token_budget;

pub use config::{load_settings, Provider, Settings};
pub use credentials::{ensure_local_token, get_secret};
pub use telemetry::{RouteContext, TelemetryStore, Usage};

use std::path::PathBuf;

/// Builds the loopback HTTP API. The caller is responsible for binding only to loopback.
pub async fn router(config_path: PathBuf, data_dir: PathBuf) -> anyhow::Result<axum::Router> {
    runtime::router(config_path, data_dir).await
}
