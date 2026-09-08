use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::{collections::HashSet, path::Path};

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct Settings {
    #[serde(skip)]
    pub providers: Vec<Provider>,
    pub public_model: String,
    pub proxy_token_env: String,
    pub connect_timeout_seconds: f64,
    pub read_timeout_seconds: f64,
    pub write_timeout_seconds: f64,
    pub pool_timeout_seconds: f64,
    pub max_connections: usize,
    pub max_request_bytes: usize,
    pub max_response_bytes: usize,
    pub max_stream_bytes: usize,
    pub max_stream_seconds: f64,
    pub log_level: String,
    pub reconnect_failover: bool,
    pub reconnect_cooldown_seconds: f64,
    pub cooldown_wait_seconds: f64,
    pub rotate_on_failure: bool,
    pub codex_stream_max_retries: u32,
    pub codex_request_max_retries: u32,
    /// Explicit opt-in for local test servers; never allows private non-loopback networks.
    pub allow_loopback_upstreams: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            providers: Vec::new(),
            public_model: "gpt-6-astra".into(),
            proxy_token_env: "LOCAL_RESPONSES_PROXY_TOKEN".into(),
            connect_timeout_seconds: 10.0,
            read_timeout_seconds: 120.0,
            write_timeout_seconds: 30.0,
            pool_timeout_seconds: 10.0,
            max_connections: 100,
            max_request_bytes: 16 * 1024 * 1024,
            max_response_bytes: 32 * 1024 * 1024,
            max_stream_bytes: 128 * 1024 * 1024,
            max_stream_seconds: 3600.0,
            log_level: "INFO".into(),
            reconnect_failover: false,
            reconnect_cooldown_seconds: 30.0,
            cooldown_wait_seconds: 0.0,
            rotate_on_failure: false,
            codex_stream_max_retries: 10,
            codex_request_max_retries: 4,
            allow_loopback_upstreams: false,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct Provider {
    pub id: String,
    pub base_url: String,
    pub base_url_env: String,
    pub deployment: String,
    pub deployment_env: String,
    pub api_key_env: String,
    pub enabled: bool,
    pub auth_type: String,
    pub api_version: String,
    pub cooldown_seconds: f64,
    pub label: String,
    pub tokens_per_minute: u64,
    pub soft_tokens_per_minute: u64,
    pub hard_tokens_per_minute: u64,
}

impl Default for Provider {
    fn default() -> Self {
        Self {
            id: String::new(),
            base_url: String::new(),
            base_url_env: String::new(),
            deployment: String::new(),
            deployment_env: String::new(),
            api_key_env: String::new(),
            enabled: true,
            auth_type: "api-key".into(),
            api_version: String::new(),
            cooldown_seconds: 60.0,
            label: String::new(),
            tokens_per_minute: 1_000_000,
            soft_tokens_per_minute: 900_000,
            hard_tokens_per_minute: 950_000,
        }
    }
}

#[derive(Deserialize)]
struct Document {
    #[serde(default)]
    proxy: Settings,
    providers: Vec<Provider>,
}

pub(super) fn valid_id(value: &str, limit: usize) -> bool {
    !value.is_empty()
        && value.len() <= limit
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn valid_env(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .as_bytes()
            .first()
            .is_some_and(|b| b.is_ascii_alphabetic() || *b == b'_')
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

fn valid_text(value: &str, limit: usize) -> bool {
    value.len() <= limit && !value.chars().any(char::is_control)
}

fn duration(value: f64, max: f64, zero: bool) -> bool {
    value.is_finite() && value >= 0.0 && (zero || value > 0.0) && value <= max
}

pub fn load_settings(path: &Path) -> Result<Settings> {
    let metadata = std::fs::metadata(path).context("Cannot read configuration file")?;
    if metadata.len() > 1024 * 1024 {
        bail!("Configuration file exceeds 1 MiB");
    }
    let content = std::fs::read_to_string(path).context("Cannot read configuration file")?;
    parse_settings(&content)
}

pub(super) fn parse_settings(content: &str) -> Result<Settings> {
    // TOML parser diagnostics contain source lines, so never expose them (or endpoint env values).
    let document: Document = toml::from_str(content)
        .map_err(|_| anyhow::anyhow!("Invalid configuration TOML or field types"))?;
    let mut settings = document.proxy;
    settings.providers = document.providers;
    if !(1..=32).contains(&settings.providers.len()) {
        bail!("Configuration requires 1 to 32 providers");
    }
    settings.public_model = settings.public_model.trim().into();
    settings.proxy_token_env = settings.proxy_token_env.trim().into();
    if settings.public_model.is_empty() || !valid_text(&settings.public_model, 160) {
        bail!("Invalid public_model");
    }
    if !valid_env(&settings.proxy_token_env) {
        bail!("Invalid proxy_token_env");
    }
    for (name, value, max, zero) in [
        (
            "connect_timeout_seconds",
            settings.connect_timeout_seconds,
            300.0,
            false,
        ),
        (
            "read_timeout_seconds",
            settings.read_timeout_seconds,
            3600.0,
            false,
        ),
        (
            "write_timeout_seconds",
            settings.write_timeout_seconds,
            300.0,
            false,
        ),
        (
            "pool_timeout_seconds",
            settings.pool_timeout_seconds,
            300.0,
            false,
        ),
        (
            "reconnect_cooldown_seconds",
            settings.reconnect_cooldown_seconds,
            86400.0,
            false,
        ),
        (
            "cooldown_wait_seconds",
            settings.cooldown_wait_seconds,
            900.0,
            true,
        ),
        (
            "max_stream_seconds",
            settings.max_stream_seconds,
            86400.0,
            false,
        ),
    ] {
        if !duration(value, max, zero) {
            bail!("Invalid {name}");
        }
    }
    if !(1..=1024).contains(&settings.max_connections) {
        bail!("Invalid max_connections (1..1024)");
    }
    if settings.codex_stream_max_retries > 20 || settings.codex_request_max_retries > 20 {
        bail!("Invalid Codex retry limit (0..20)");
    }
    for (name, value, max) in [
        (
            "max_request_bytes",
            settings.max_request_bytes,
            64 * 1024 * 1024,
        ),
        (
            "max_response_bytes",
            settings.max_response_bytes,
            128 * 1024 * 1024,
        ),
        (
            "max_stream_bytes",
            settings.max_stream_bytes,
            1024 * 1024 * 1024,
        ),
    ] {
        if value == 0 || value > max {
            bail!("Invalid {name}");
        }
    }
    settings.log_level.make_ascii_uppercase();
    if !["DEBUG", "INFO", "WARNING", "ERROR"].contains(&settings.log_level.as_str()) {
        bail!("Invalid log_level");
    }
    let mut ids = HashSet::new();
    for provider in &mut settings.providers {
        if !valid_id(&provider.id, 40) || !ids.insert(provider.id.clone()) {
            bail!("Provider IDs must be unique ASCII identifiers");
        }
        for (env, value) in [
            (&provider.base_url_env, &mut provider.base_url),
            (&provider.deployment_env, &mut provider.deployment),
        ] {
            if !env.is_empty() {
                if !valid_env(env) {
                    bail!("Invalid provider environment variable name");
                }
                if let Ok(replacement) = std::env::var(env) {
                    *value = replacement;
                }
            }
            *value = value.trim().into();
        }
        if provider.api_key_env.is_empty() {
            provider.api_key_env = format!("{}_API_KEY", provider.id.to_ascii_uppercase());
        }
        if !valid_env(&provider.api_key_env) {
            bail!("Invalid API key environment variable name");
        }
        if !["api-key", "bearer"].contains(&provider.auth_type.as_str()) {
            bail!("Invalid auth_type");
        }
        if !duration(provider.cooldown_seconds, 86400.0, true) {
            bail!("Invalid provider cooldown_seconds");
        }
        if provider.soft_tokens_per_minute == 0
            || provider.soft_tokens_per_minute >= provider.hard_tokens_per_minute
            || provider.hard_tokens_per_minute > provider.tokens_per_minute
            || provider.tokens_per_minute > 1_000_000_000
        {
            bail!(
                "Expected 0 < soft_tokens_per_minute < hard_tokens_per_minute <= tokens_per_minute <= 1000000000"
            );
        }
        if !valid_text(&provider.deployment, 256)
            || !valid_text(&provider.api_version, 128)
            || !valid_text(&provider.label, 320)
            || !valid_text(&provider.base_url, 2048)
        {
            bail!("Invalid provider text field");
        }
        if provider.enabled {
            if provider.deployment.is_empty() {
                bail!("Enabled provider requires a deployment");
            }
            super::network::validate_url(&provider.base_url, settings.allow_loopback_upstreams)?;
        }
    }
    Ok(settings)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config(extra: &str) -> String {
        format!("[proxy]\n{extra}\n[[providers]]\nid='p1'\nenabled=false\n")
    }
    #[test]
    fn bounds_and_types_are_enforced() {
        for invalid in [
            "max_request_bytes=0",
            "max_connections=100000",
            "read_timeout_seconds=nan",
            "cooldown_wait_seconds=901",
            "max_stream_seconds=0",
            "proxy_token_env='x x'",
            "reconnect_failover='true'",
            "codex_stream_max_retries=21",
            "codex_request_max_retries=4294967295",
        ] {
            assert!(parse_settings(&config(invalid)).is_err(), "{invalid}");
        }
        assert!(parse_settings(&config("max_request_bytes=4096\ncooldown_wait_seconds=0")).is_ok());
    }
    #[test]
    fn malformed_toml_diagnostics_never_echo_source() {
        let error = parse_settings("password=\"private-value\"\nx=invalid")
            .unwrap_err()
            .to_string();
        assert!(!error.contains("private-value"));
    }
    #[test]
    fn provider_token_limits_default_and_allow_independent_overrides() {
        let settings = parse_settings(&format!(
            "{}\n[[providers]]\nid='p2'\nenabled=false\ntokens_per_minute=1000000000\nsoft_tokens_per_minute=1\nhard_tokens_per_minute=1000000000\n",
            config("")
        ))
        .unwrap();
        assert_eq!(settings.providers[0].tokens_per_minute, 1_000_000);
        assert_eq!(settings.providers[0].soft_tokens_per_minute, 900_000);
        assert_eq!(settings.providers[0].hard_tokens_per_minute, 950_000);
        assert_eq!(settings.providers[1].tokens_per_minute, 1_000_000_000);
        assert_eq!(settings.providers[1].soft_tokens_per_minute, 1);
        assert_eq!(settings.providers[1].hard_tokens_per_minute, 1_000_000_000);
    }
    #[test]
    fn provider_token_limits_enforce_integer_types_ranges_and_order() {
        for name in [
            "tokens_per_minute",
            "soft_tokens_per_minute",
            "hard_tokens_per_minute",
        ] {
            for value in [
                "true",
                "false",
                "1.0",
                "1.5",
                "'1000000'",
                "nan",
                "inf",
                "0",
                "-1",
                "1000000001",
                "18446744073709551616",
            ] {
                assert!(
                    parse_settings(&format!("{}{name}={value}\n", config(""))).is_err(),
                    "{name}={value}"
                );
            }
        }
        for (limit, soft, hard) in [
            (100, 90, 90),
            (100, 95, 90),
            (100, 90, 101),
            (100, 101, 102),
        ] {
            assert!(parse_settings(&format!(
                "{}tokens_per_minute={limit}\nsoft_tokens_per_minute={soft}\nhard_tokens_per_minute={hard}\n",
                config("")
            ))
            .is_err());
        }
    }
    #[test]
    fn empty_or_duplicate_provider_ids_are_rejected() {
        assert!(parse_settings("[[providers]]\nenabled=false").is_err());
        assert!(parse_settings(
            "[[providers]]\nid='a'\nenabled=false\n[[providers]]\nid='a'\nenabled=false"
        )
        .is_err());
    }
}
