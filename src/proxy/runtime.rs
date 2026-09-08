use super::{
    config::valid_id,
    credentials::get_secret,
    network,
    sse::{ResponseStreamObserver, StreamFailure},
    telemetry::{normalize_usage, RouteContext, TelemetryStore, Usage},
    token_budget::{self, BudgetEntry, BudgetStatus, TokenWindow},
    Provider, Settings,
};
use anyhow::Result;
use axum::{
    body::{to_bytes, Body},
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use bytes::Bytes;
use futures_util::StreamExt;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    path::PathBuf,
    sync::{Arc, Mutex, MutexGuard},
    time::{Duration, Instant, SystemTime},
};
use subtle::ConstantTimeEq;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use zeroize::Zeroizing;

const EVENT_LIMIT: usize = 1024 * 1024;
const MAX_ATTEMPTS: usize = 64;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // A poisoned accounting lock must not panic an independent network request.
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

#[derive(Clone, Default)]
struct Counters {
    attempts: u64,
    in_flight: u64,
    completed: u64,
    transport_errors: u64,
    stream_interruptions: u64,
    cancellations: u64,
    cooldown_skips: u64,
    cooldown_events: u64,
    rate_limit_events: u64,
    cooldown_until: Option<Instant>,
    cooldown_reason: String,
    cooldown_status: u16,
    reconnect_pending: bool,
    statuses: BTreeMap<String, u64>,
}

struct Backend {
    provider: Provider,
    key: Option<Zeroizing<String>>,
    counters: Arc<Mutex<Counters>>,
}

impl Backend {
    fn usable(&self) -> bool {
        self.provider.enabled && self.key.is_some()
    }
}

#[derive(Default)]
struct Stats {
    requests: u64,
    attempts: u64,
    failovers: u64,
    in_flight: u64,
    responses_completed: u64,
    exhausted: u64,
    stream_interruptions: u64,
    client_disconnects: u64,
    bytes_forwarded: u64,
    rate_limit_events: u64,
    reconnect_failovers: u64,
    waiting_requests: u64,
    cooldown_waits: u64,
    token_budget_waits: u64,
    token_budget_skips: u64,
}

#[derive(Clone, Default, Deserialize)]
struct RouteSpec {
    id: String,
    providers: Vec<String>,
    #[serde(default = "priority")]
    strategy: String,
    wait_seconds: Option<f64>,
    #[serde(default)]
    max_output_tokens: u64,
    #[serde(default)]
    project_id: String,
    #[serde(default)]
    task_id: String,
    #[serde(default)]
    run_id: String,
    #[serde(default)]
    thread_id: String,
    #[serde(default)]
    role: String,
}
fn priority() -> String {
    "priority".into()
}

#[derive(Clone)]
struct TaskRoute {
    spec: RouteSpec,
    preferred: String,
    last_provider: Option<String>,
    attempts: BTreeMap<String, u64>,
    assigned: Option<String>,
}

impl TaskRoute {
    fn value(&self) -> Value {
        json!({"id":self.spec.id,"providers":self.spec.providers,"strategy":self.spec.strategy,
            "preferred":self.preferred,"wait_seconds":self.spec.wait_seconds.unwrap_or(0.0),
            "max_output_tokens":self.spec.max_output_tokens,"last_provider":self.last_provider,"attempts":self.attempts,
            "project_id":self.spec.project_id,"task_id":self.spec.task_id,"run_id":self.spec.run_id,
            "thread_id":self.spec.thread_id,"role":self.spec.role,"assigned_provider":self.assigned})
    }

    fn context(&self) -> RouteContext {
        RouteContext {
            route_id: self.spec.id.clone(),
            project_id: self.spec.project_id.clone(),
            task_id: self.spec.task_id.clone(),
            run_id: self.spec.run_id.clone(),
            thread_id: self.spec.thread_id.clone(),
            role: self.spec.role.clone(),
        }
    }

    fn assignment(&self) -> Value {
        json!({"route_id":self.spec.id,"project_id":self.spec.project_id,"task_id":self.spec.task_id,
            "run_id":self.spec.run_id,"thread_id":self.spec.thread_id,"role":self.spec.role,
            "provider_id":self.assigned})
    }
}

struct LiveConfig {
    settings: Settings,
    token: Option<Zeroizing<String>>,
    providers: Vec<Arc<Backend>>,
    client: reqwest::Client,
    permits: Arc<Semaphore>,
}

impl LiveConfig {
    fn load(settings: Settings, previous: Option<&LiveConfig>) -> Result<Self> {
        if previous.is_some_and(|old| old.settings.max_connections != settings.max_connections) {
            anyhow::bail!("Changing max_connections requires restarting the proxy");
        }
        let token = get_secret("local-proxy-token", &settings.proxy_token_env)?.map(Zeroizing::new);
        let mut providers = Vec::new();
        for provider in &settings.providers {
            let key = if provider.enabled {
                get_secret(&provider.id, &provider.api_key_env)?.map(Zeroizing::new)
            } else {
                None
            };
            let counters = previous
                .and_then(|old| {
                    old.providers
                        .iter()
                        .find(|item| item.provider.id == provider.id)
                })
                .map(|item| item.counters.clone())
                .unwrap_or_default();
            providers.push(Arc::new(Backend {
                provider: provider.clone(),
                key,
                counters,
            }));
        }
        let client = network::client(&settings)?;
        let permits = previous
            .filter(|old| old.settings.max_connections == settings.max_connections)
            .map(|old| old.permits.clone())
            .unwrap_or_else(|| Arc::new(Semaphore::new(settings.max_connections)));
        Ok(Self {
            settings,
            token,
            providers,
            client,
            permits,
        })
    }
}

struct Inner {
    config: Arc<LiveConfig>,
    routes: HashMap<String, TaskRoute>,
    released_runs: HashSet<String>,
    token_budgets: HashMap<String, TokenWindow>,
    stats: Stats,
    preferred: String,
}

impl Inner {
    fn budget_policy<'a>(&'a self, provider: &'a Provider) -> &'a Provider {
        self.config
            .providers
            .iter()
            .find(|current| current.provider.id == provider.id)
            .map(|current| &current.provider)
            .unwrap_or(provider)
    }

    fn budget_status(&self, provider: &Provider, at: f64) -> BudgetStatus {
        let provider = self.budget_policy(provider);
        self.token_budgets
            .get(&provider.id)
            .map(|budget| budget.status(provider, at))
            .unwrap_or_else(|| TokenWindow::default().status(provider, at))
    }

    fn can_admit(&self, provider: &Provider, estimate: u64, at: f64) -> bool {
        let provider = self.budget_policy(provider);
        self.token_budgets
            .get(&provider.id)
            .map(|budget| budget.can_admit(provider, estimate, at))
            .unwrap_or(estimate <= provider.hard_tokens_per_minute)
    }

    fn budget_retry_after(&self, provider: &Provider, estimate: u64, at: f64) -> f64 {
        let provider = self.budget_policy(provider);
        self.token_budgets
            .get(&provider.id)
            .map(|budget| budget.retry_after(provider, estimate, at))
            .unwrap_or(0.0)
    }
}

struct Runtime {
    inner: Mutex<Inner>,
    telemetry: Option<TelemetryStore>,
    config_path: PathBuf,
    started: Instant,
    reload_lock: tokio::sync::Mutex<()>,
    routes_changed: tokio::sync::Notify,
}

pub(super) async fn router(config_path: PathBuf, data_dir: PathBuf) -> Result<Router> {
    let settings = super::load_settings(&config_path)?;
    let config = Arc::new(LiveConfig::load(settings, None)?);
    let telemetry = match TelemetryStore::open(data_dir.join("telemetry.sqlite3")) {
        Ok(store) => {
            let _ = store.recover_interrupted();
            Some(store)
        }
        Err(_) => {
            tracing::warn!("event=telemetry_unavailable operation=open");
            None
        }
    };
    let mut token_budgets: HashMap<String, TokenWindow> = HashMap::new();
    if let Some(store) = &telemetry {
        match store.restore_token_budgets(token_budget::now()) {
            Ok(entries) => {
                for entry in entries {
                    token_budgets
                        .entry(entry.provider.clone())
                        .or_default()
                        .restore(entry);
                }
            }
            Err(_) => tracing::warn!("event=telemetry_unavailable operation=restore_token_budget"),
        }
    }
    let preferred = config.providers[0].provider.id.clone();
    let runtime = Arc::new(Runtime {
        inner: Mutex::new(Inner {
            config,
            routes: HashMap::new(),
            released_runs: HashSet::new(),
            token_budgets,
            stats: Stats::default(),
            preferred,
        }),
        telemetry,
        config_path,
        started: Instant::now(),
        reload_lock: tokio::sync::Mutex::new(()),
        routes_changed: tokio::sync::Notify::new(),
    });
    Ok(Router::new()
        .route("/health", get(health))
        .route("/status", get(status))
        .route("/admin/routes", post(set_route))
        .route("/admin/routes/release", post(release_routes))
        .route("/admin/reload", post(reload))
        .route("/v1/models", get(models))
        .route("/r/{route_id}/v1/models", get(models))
        .route("/v1/responses", post(responses))
        .route("/v1/responses/compact", post(responses))
        .route("/r/{route_id}/v1/responses", post(responses))
        .route("/r/{route_id}/v1/responses/compact", post(responses))
        .with_state(runtime))
}

fn api_error(status: StatusCode, code: &str, message: &str, request_id: &str) -> Response {
    let mut response = (
        status,
        Json(json!({"error":{"message":message,"type":"proxy_error","param":null,"code":code}})),
    )
        .into_response();
    response
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static("no-store"));
    if let Ok(value) = HeaderValue::from_str(request_id) {
        response.headers_mut().insert("x-request-id", value);
    }
    response
}

fn safe_json(value: Value) -> Response {
    let mut response = Json(value).into_response();
    response
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static("no-store"));
    response
}

impl Runtime {
    fn config(&self) -> Arc<LiveConfig> {
        lock(&self.inner).config.clone()
    }

    fn authenticate(&self, headers: &HeaderMap) -> Option<Response> {
        let config = self.config();
        let Some(token) = &config.token else {
            return Some(api_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "proxy_not_configured",
                "Configure the local proxy token first.",
                "",
            ));
        };
        let expected = Zeroizing::new(format!("Bearer {}", token.as_str()));
        let supplied = headers
            .get("authorization")
            .map(|value| value.as_bytes())
            .unwrap_or_default();
        if headers.get_all("authorization").iter().count() != 1
            || !bool::from(supplied.ct_eq(expected.as_bytes()))
        {
            return Some(api_error(
                StatusCode::UNAUTHORIZED,
                "invalid_api_key",
                "A valid local proxy token is required.",
                "",
            ));
        }
        None
    }

    fn status(&self) -> Value {
        let inner = lock(&self.inner);
        let config = &inner.config;
        let now = Instant::now();
        let at = token_budget::now();
        let providers: Vec<Value> = config.providers.iter().map(|backend| {
            let counts = lock(&backend.counters);
            let remaining = counts.cooldown_until.map(|until| until.saturating_duration_since(now).as_secs_f64()).unwrap_or(0.0);
            let mut assignments: Vec<Value> = inner.routes.values()
                .filter(|route| route.assigned.as_deref() == Some(backend.provider.id.as_str()))
                .map(TaskRoute::assignment).collect();
            assignments.sort_by(|a,b| (a["route_id"].as_str(), a["thread_id"].as_str()).cmp(&(b["route_id"].as_str(), b["thread_id"].as_str())));
            let main_count = assignments.iter().filter(|assignment| assignment["role"] == "main").count();
            let auxiliary_count = assignments.iter().filter(|assignment| assignment["role"] == "auxiliary").count();
            let budget = inner.budget_status(&backend.provider, at);
            json!({"id":backend.provider.id,"enabled":backend.provider.enabled,"configured":backend.key.is_some(),
                "available":backend.usable() && remaining == 0.0 && !budget.hard_reached,"cooldown_remaining_seconds":remaining,
                "token_budget":budget,
                "main_count":main_count,"auxiliary_count":auxiliary_count,"assignments":assignments,
                "attempts":counts.attempts,"in_flight":counts.in_flight,"responses_completed":counts.completed,
                "http_statuses":counts.statuses,"transport_errors":counts.transport_errors,
                "stream_interruptions":counts.stream_interruptions,"client_disconnects":counts.cancellations,
                "cooldown_skips":counts.cooldown_skips,"cooldown_events":counts.cooldown_events,
                "cooldown_reason":if remaining > 0.0 {counts.cooldown_reason.as_str()} else {""},"rate_limit_events":counts.rate_limit_events})
        }).collect();
        let ready =
            config.token.is_some() && providers.iter().any(|value| value["available"] == true);
        let stats = &inner.stats;
        let mut routes: Vec<_> = inner.routes.values().map(TaskRoute::value).collect();
        routes.sort_by(|a, b| {
            (a["id"].as_str(), a["thread_id"].as_str())
                .cmp(&(b["id"].as_str(), b["thread_id"].as_str()))
        });
        json!({"status":if ready {"ready"} else {"unavailable"},"uptime_seconds":self.started.elapsed().as_secs_f64(),
            "telemetry_enabled":self.telemetry.is_some(),"reconnect_failover":config.settings.reconnect_failover,
            "cooldown_wait_seconds":config.settings.cooldown_wait_seconds,"rotate_on_failure":config.settings.rotate_on_failure,
            "routing_version":2,"routes":routes,"preferred_provider":if config.settings.rotate_on_failure {Some(&inner.preferred)} else {None},
            "stats":{"requests":stats.requests,"attempts":stats.attempts,"failovers":stats.failovers,"in_flight":stats.in_flight,
                "responses_completed":stats.responses_completed,"exhausted":stats.exhausted,"stream_interruptions":stats.stream_interruptions,
                "client_disconnects":stats.client_disconnects,"bytes_forwarded":stats.bytes_forwarded,"rate_limit_events":stats.rate_limit_events,
                "reconnect_failovers":stats.reconnect_failovers,"waiting_requests":stats.waiting_requests,"cooldown_waits":stats.cooldown_waits,
                "token_budget_waits":stats.token_budget_waits,"token_budget_skips":stats.token_budget_skips},
            "providers":providers})
    }

    async fn resolve_route(
        &self,
        route_id: &str,
        headers: &HeaderMap,
    ) -> Option<(String, TaskRoute)> {
        let thread_id = match headers.get("thread-id") {
            Some(value) if headers.get_all("thread-id").iter().count() == 1 => {
                let value = value.to_str().ok()?;
                if !valid_id(value, 100) {
                    return None;
                }
                value
            }
            None => "",
            _ => return None,
        };
        let key = route_key(route_id, thread_id);
        // The app-server's verified subAgentActivity notification can arrive
        // just after a child's first HTTP request. Never infer that child's
        // role from request order, session-id, or parent headers.
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            {
                let inner = lock(&self.inner);
                if let Some(route) = inner.routes.get(&key) {
                    return Some((key, route.clone()));
                }
                if let Some(route) = inner.routes.get(route_id) {
                    if route.spec.role.is_empty() {
                        return Some((route_id.to_owned(), route.clone()));
                    }
                }
                if thread_id.is_empty()
                    || !inner.routes.values().any(|route| route.spec.id == route_id)
                {
                    return None;
                }
            }
            if Instant::now() >= deadline {
                return None;
            }
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_millis(50)) => {},
                _ = self.routes_changed.notified() => {},
            }
        }
    }

    fn order(&self, config: &LiveConfig, route_id: Option<&str>) -> Vec<Arc<Backend>> {
        let inner = lock(&self.inner);
        let route = route_id.and_then(|id| inner.routes.get(id));
        // Reload can invalidate a route while an earlier request waits for a
        // provider. It must never turn that task into the unrestricted route.
        if route_id.is_some() && route.is_none() {
            return Vec::new();
        }
        let mut order = if let Some(route) = route {
            route
                .spec
                .providers
                .iter()
                .filter_map(|id| {
                    config
                        .providers
                        .iter()
                        .find(|item| item.provider.id == *id)
                        .cloned()
                })
                .collect::<Vec<_>>()
        } else {
            config.providers.clone()
        };
        if let Some(route) = route {
            if route.spec.strategy == "balanced" {
                order.sort_by_key(|item| {
                    (
                        lock(&item.counters).in_flight,
                        route.attempts.get(&item.provider.id).copied().unwrap_or(0),
                    )
                });
            } else if let Some(pivot) = order
                .iter()
                .position(|item| item.provider.id == route.preferred)
            {
                order.rotate_left(pivot);
            }
        } else if config.settings.rotate_on_failure {
            if let Some(pivot) = order
                .iter()
                .position(|item| item.provider.id == inner.preferred)
            {
                order.rotate_left(pivot);
            }
        }
        order
    }

    /// Selection and reservation share the global routing lock. In particular,
    /// two project starts cannot both observe the same API as free.
    fn reserve(
        &self,
        route_id: &str,
        candidates: &[Arc<Backend>],
        estimate: u64,
    ) -> Option<Arc<Backend>> {
        let mut inner = lock(&self.inner);
        let route = inner.routes.get(route_id)?;
        let selected = choose_assignment(&inner, route, candidates, estimate)?;
        let route = inner.routes.get_mut(route_id)?;
        route.assigned = Some(selected.provider.id.clone());
        route.preferred = selected.provider.id.clone();
        Some(selected)
    }

    fn event(&self, kind: &str, provider: &str, peer: &str, reason: &str, request_id: &str) {
        if let Some(store) = &self.telemetry {
            if store
                .event(kind, provider, peer, reason, request_id, None)
                .is_err()
            {
                tracing::warn!("event=telemetry_unavailable operation=event");
            }
        }
    }

    fn budget_event(&self, kind: &str, reason: &str, request_id: &str, route: Option<&TaskRoute>) {
        if let Some(store) = &self.telemetry {
            let context = route.map(TaskRoute::context).unwrap_or_default();
            if store
                .event_with_context(kind, "", "", reason, request_id, None, &context)
                .is_err()
            {
                tracing::warn!("event=telemetry_unavailable operation=token_budget_event");
            }
        }
    }

    fn rotate(
        &self,
        backend: &Backend,
        config: &LiveConfig,
        route_id: Option<&str>,
        reason: &str,
        request_id: &str,
    ) {
        if route_id.is_none() && !config.settings.rotate_on_failure {
            return;
        }
        if let Some(route_id) = route_id {
            let mut inner = lock(&self.inner);
            if let Some(mut route) = inner
                .routes
                .get(route_id)
                .cloned()
                .filter(|route| !route.spec.role.is_empty())
            {
                route.assigned = None;
                let candidates: Vec<_> = config
                    .providers
                    .iter()
                    .filter(|item| item.provider.id != backend.provider.id)
                    .cloned()
                    .collect();
                let next = choose_assignment(&inner, &route, &candidates, 0)
                    .map(|item| item.provider.id.clone());
                if let Some(live) = inner.routes.get_mut(route_id) {
                    live.assigned = next.clone();
                    if let Some(next) = &next {
                        live.preferred = next.clone();
                    }
                }
                drop(inner);
                if let Some(next) = next {
                    self.event("rotation", &backend.provider.id, &next, reason, request_id);
                }
                return;
            }
        }
        let order = self.order(config, route_id);
        let position = order
            .iter()
            .position(|item| item.provider.id == backend.provider.id)
            .unwrap_or(0);
        let next = order
            .iter()
            .cycle()
            .skip(position + 1)
            .take(order.len().saturating_sub(1))
            .find(|item| {
                item.usable()
                    && lock(&item.counters)
                        .cooldown_until
                        .is_none_or(|until| until <= Instant::now())
            })
            .map(|item| item.provider.id.clone());
        if let Some(next) = next {
            let mut inner = lock(&self.inner);
            let changed = if let Some(route) = route_id.and_then(|id| inner.routes.get_mut(id)) {
                if route.preferred == backend.provider.id {
                    route.preferred = next.clone();
                    true
                } else {
                    false
                }
            } else if inner.preferred == backend.provider.id {
                inner.preferred = next.clone();
                true
            } else {
                false
            };
            drop(inner);
            if changed {
                self.event("rotation", &backend.provider.id, &next, reason, request_id);
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn cooldown(
        &self,
        backend: &Backend,
        config: &LiveConfig,
        failure: &StreamFailure,
        headers: &HeaderMap,
        reconnect: bool,
        route_id: Option<&str>,
        request_id: &str,
    ) {
        let mut combined = headers.clone();
        for (name, value) in &failure.retry_headers {
            if let (Ok(name), Ok(value)) = (
                axum::http::HeaderName::from_bytes(name.as_bytes()),
                HeaderValue::from_str(value),
            ) {
                combined.entry(name).or_insert(value);
            }
        }
        let fallback = if failure.status == 429 {
            backend.provider.cooldown_seconds
        } else {
            config.settings.reconnect_cooldown_seconds
        };
        let seconds = retry_after_seconds(&combined, fallback)
            .max(if reconnect {
                config.settings.reconnect_cooldown_seconds
            } else {
                0.0
            })
            .min(86400.0);
        let until = Instant::now() + Duration::from_secs_f64(seconds);
        {
            let mut inner = lock(&self.inner);
            let mut counts = lock(&backend.counters);
            counts.cooldown_until = Some(
                counts
                    .cooldown_until
                    .map(|old| old.max(until))
                    .unwrap_or(until),
            );
            counts.cooldown_reason = failure.reason.clone();
            counts.cooldown_status = failure.status;
            counts.cooldown_events += 1;
            counts.reconnect_pending |= reconnect;
            if failure.status == 429 {
                counts.rate_limit_events += 1;
                inner.stats.rate_limit_events += 1;
            }
        }
        self.event(
            "cooldown",
            &backend.provider.id,
            "",
            &failure.reason,
            request_id,
        );
        self.rotate(backend, config, route_id, &failure.reason, request_id);
    }
}

async fn health(State(runtime): State<Arc<Runtime>>) -> Response {
    let ready = runtime.status()["status"] == "ready";
    safe_json(json!({"status":"ok","ready":ready}))
}
async fn status(State(runtime): State<Arc<Runtime>>, headers: HeaderMap) -> Response {
    if let Some(denied) = runtime.authenticate(&headers) {
        return denied;
    }
    safe_json(runtime.status())
}
async fn models(State(runtime): State<Arc<Runtime>>, headers: HeaderMap) -> Response {
    if let Some(denied) = runtime.authenticate(&headers) {
        return denied;
    }
    safe_json(
        json!({"object":"list","data":[{"id":runtime.config().settings.public_model,"object":"model","created":0,"owned_by":"local-proxy"}],"models":[]}),
    )
}

async fn reload(State(runtime): State<Arc<Runtime>>, headers: HeaderMap) -> Response {
    if let Some(denied) = runtime.authenticate(&headers) {
        return denied;
    }
    let _reload = runtime.reload_lock.lock().await;
    let old = runtime.config();
    let path = runtime.config_path.clone();
    let result = tokio::task::spawn_blocking(move || {
        let settings = super::load_settings(&path)?;
        LiveConfig::load(settings, Some(&old))
    })
    .await;
    match result {
        Ok(Ok(config)) => {
            let mut inner = lock(&runtime.inner);
            if !config
                .providers
                .iter()
                .any(|backend| backend.provider.id == inner.preferred)
            {
                inner.preferred = config.providers[0].provider.id.clone();
            }
            // A route cannot silently escape its original set after reload.
            inner
                .routes
                .retain(|_, route| route_compatible(&route.spec, &config));
            inner.config = Arc::new(config);
            drop(inner);
            runtime.routes_changed.notify_waiters();
            safe_json(runtime.status())
        }
        _ => api_error(
            StatusCode::BAD_REQUEST,
            "configuration_error",
            "Configuration reload failed. Check syntax, provider addresses and credentials.",
            "",
        ),
    }
}

fn route_compatible(spec: &RouteSpec, config: &LiveConfig) -> bool {
    let backends: Vec<_> = spec
        .providers
        .iter()
        .filter_map(|id| {
            config
                .providers
                .iter()
                .find(|backend| backend.provider.id == *id && backend.usable())
        })
        .collect();
    backends.len() == spec.providers.len()
        && !backends.is_empty()
        && backends
            .iter()
            .all(|backend| backend.provider.deployment == backends[0].provider.deployment)
}

fn choose_assignment(
    inner: &Inner,
    route: &TaskRoute,
    candidates: &[Arc<Backend>],
    estimate: u64,
) -> Option<Arc<Backend>> {
    let now = Instant::now();
    let at = token_budget::now();
    candidates
        .iter()
        .filter(|backend| {
            backend.usable()
                && route.spec.providers.contains(&backend.provider.id)
                && inner.can_admit(&backend.provider, estimate, at)
                && lock(&backend.counters)
                    .cooldown_until
                    .is_none_or(|until| until <= now)
        })
        .min_by_key(|backend| {
            let mut main = 0_u64;
            let mut auxiliary = 0_u64;
            for other in inner.routes.values().filter(|other| {
                (other.spec.id != route.spec.id || other.spec.thread_id != route.spec.thread_id)
                    && other.assigned.as_deref() == Some(backend.provider.id.as_str())
            }) {
                if other.spec.role == "main" {
                    main += 1;
                } else if other.spec.role == "auxiliary" {
                    auxiliary += 1;
                }
            }
            let in_flight = lock(&backend.counters).in_flight;
            let free = main + auxiliary + in_flight == 0;
            let class = if route.spec.role == "auxiliary" {
                if auxiliary > 0 {
                    0
                } else if free {
                    1
                } else {
                    2
                }
            } else if free {
                0
            } else if main == 0 && auxiliary > 0 {
                1
            } else {
                2
            };
            (
                inner.budget_status(&backend.provider, at).soft_reached,
                route.assigned.as_deref() != Some(backend.provider.id.as_str()),
                class,
                main + auxiliary + in_flight,
                in_flight,
                route
                    .spec
                    .providers
                    .iter()
                    .position(|id| id == &backend.provider.id)
                    .unwrap_or(usize::MAX),
            )
        })
        .cloned()
}

fn valid_route_context(spec: &RouteSpec) -> bool {
    [
        &spec.project_id,
        &spec.task_id,
        &spec.run_id,
        &spec.thread_id,
    ]
    .into_iter()
    .all(|id| id.is_empty() || valid_id(id, 100))
        && (spec.role.is_empty()
            || (["main", "auxiliary"].contains(&spec.role.as_str())
                && !spec.project_id.is_empty()
                && !spec.task_id.is_empty()
                && !spec.run_id.is_empty()))
}

fn route_key(route_id: &str, thread_id: &str) -> String {
    if thread_id.is_empty() {
        route_id.to_owned()
    } else {
        format!("{route_id}\0{thread_id}")
    }
}

#[derive(Deserialize)]
struct ReleaseSpec {
    run_id: String,
}

async fn release_routes(State(runtime): State<Arc<Runtime>>, request: Request) -> Response {
    if let Some(denied) = runtime.authenticate(request.headers()) {
        return denied;
    }
    let bytes =
        tokio::time::timeout(Duration::from_secs(10), to_bytes(request.into_body(), 1024)).await;
    let spec = match bytes {
        Ok(Ok(bytes)) => serde_json::from_slice::<ReleaseSpec>(&bytes).ok(),
        _ => None,
    };
    let Some(spec) = spec.filter(|spec| valid_id(&spec.run_id, 100)) else {
        return api_error(
            StatusCode::BAD_REQUEST,
            "configuration_error",
            "Expected a valid run_id.",
            "",
        );
    };
    let mut inner = lock(&runtime.inner);
    let before = inner.routes.len();
    inner
        .routes
        .retain(|_, route| route.spec.run_id != spec.run_id);
    inner.released_runs.insert(spec.run_id.clone());
    let released = before - inner.routes.len();
    drop(inner);
    runtime.routes_changed.notify_waiters();
    safe_json(json!({"run_id":spec.run_id,"released":released}))
}

async fn set_route(State(runtime): State<Arc<Runtime>>, request: Request) -> Response {
    if let Some(denied) = runtime.authenticate(request.headers()) {
        return denied;
    }
    let bytes = match tokio::time::timeout(
        Duration::from_secs(10),
        to_bytes(request.into_body(), 20_000),
    )
    .await
    {
        Ok(Ok(bytes)) => bytes,
        _ => {
            return api_error(
                StatusCode::BAD_REQUEST,
                "configuration_error",
                "Route definition is too large or incomplete.",
                "",
            )
        }
    };
    let Ok(mut spec) = serde_json::from_slice::<RouteSpec>(&bytes) else {
        return api_error(
            StatusCode::BAD_REQUEST,
            "configuration_error",
            "Expected a valid route object.",
            "",
        );
    };
    let mut inner = lock(&runtime.inner);
    let key = if spec.role.is_empty() {
        spec.id.clone()
    } else {
        route_key(&spec.id, &spec.thread_id)
    };
    let placeholder = inner
        .routes
        .get(&spec.id)
        .filter(|route| route.spec.thread_id.is_empty() && route.spec.role == spec.role);
    let mut ids = spec.providers.clone();
    ids.sort();
    ids.dedup();
    if !valid_id(&spec.id, 100)
        || !valid_route_context(&spec)
        || ids.len() != spec.providers.len()
        || !["priority", "balanced"].contains(&spec.strategy.as_str())
        || spec.max_output_tokens > 200_000
        || !route_compatible(&spec, &inner.config)
        || spec
            .wait_seconds
            .is_some_and(|wait| !wait.is_finite() || !(0.0..=900.0).contains(&wait))
        || (inner.routes.len() >= 2048 && !inner.routes.contains_key(&key) && placeholder.is_none())
    {
        return api_error(
            StatusCode::BAD_REQUEST,
            "configuration_error",
            "Invalid route, limits or provider selection; failover deployments must match.",
            "",
        );
    }
    spec.wait_seconds = Some(
        spec.wait_seconds
            .unwrap_or(inner.config.settings.cooldown_wait_seconds),
    );
    let old = inner.routes.get(&key).or(placeholder);
    if !spec.run_id.is_empty() && inner.released_runs.contains(&spec.run_id) {
        return api_error(
            StatusCode::CONFLICT,
            "run_released",
            "The run has already released its routes.",
            "",
        );
    }
    if inner.routes.values().any(|route| {
        route.spec.id == spec.id
            && (route.spec.run_id != spec.run_id
                || route.spec.task_id != spec.task_id
                || route.spec.project_id != spec.project_id)
    }) {
        return api_error(
            StatusCode::CONFLICT,
            "route_conflict",
            "An existing route belongs to another run.",
            "",
        );
    }
    if old.is_some_and(|old| {
        old.spec.run_id != spec.run_id
            || old.spec.role != spec.role
            || old.spec.project_id != spec.project_id
            || old.spec.task_id != spec.task_id
    }) {
        return api_error(
            StatusCode::CONFLICT,
            "route_conflict",
            "An existing route belongs to another run or role.",
            "",
        );
    }
    let mut route = TaskRoute {
        preferred: old
            .map(|route| route.preferred.clone())
            .unwrap_or_else(|| spec.providers[0].clone()),
        last_provider: old.and_then(|route| route.last_provider.clone()),
        attempts: old.map(|route| route.attempts.clone()).unwrap_or_default(),
        assigned: old.and_then(|route| route.assigned.clone()),
        spec,
    };
    if !route.spec.role.is_empty() {
        route.assigned = choose_assignment(&inner, &route, &inner.config.providers, 0)
            .map(|backend| backend.provider.id.clone());
        if let Some(provider) = &route.assigned {
            route.preferred = provider.clone();
        }
    }
    let value = route.value();
    if key != route.spec.id && placeholder.is_some() {
        inner.routes.remove(&route.spec.id);
    }
    inner.routes.insert(key, route);
    drop(inner);
    runtime.routes_changed.notify_waiters();
    safe_json(value)
}

struct AttemptGuard {
    runtime: Arc<Runtime>,
    backend: Arc<Backend>,
    id: Option<String>,
    request_id: String,
    status: Option<u16>,
    budget_id: String,
    reserved_tokens: u64,
    finished: bool,
}

#[derive(Debug)]
enum AttemptRejected {
    RouteUnavailable,
    TokenBudget,
}

impl AttemptGuard {
    fn begin(
        runtime: Arc<Runtime>,
        backend: Arc<Backend>,
        request_id: &str,
        compact: bool,
        route_id: Option<&str>,
        reserved_tokens: u64,
    ) -> Result<Self, AttemptRejected> {
        let budget_id = uuid::Uuid::new_v4().simple().to_string();
        let at = token_budget::now();
        let context = {
            let mut inner = lock(&runtime.inner);
            if route_id.is_some_and(|id| !inner.routes.contains_key(id)) {
                return Err(AttemptRejected::RouteUnavailable);
            }
            // Check and reserve under the same global lock. A concurrent
            // request cannot spend the capacity observed during selection.
            let current_provider = inner
                .config
                .providers
                .iter()
                .find(|candidate| candidate.provider.id == backend.provider.id)
                .map(|candidate| &candidate.provider)
                .unwrap_or(&backend.provider);
            if !inner.can_admit(current_provider, reserved_tokens, at) {
                inner.stats.token_budget_skips += 1;
                return Err(AttemptRejected::TokenBudget);
            }
            inner
                .token_budgets
                .entry(backend.provider.id.clone())
                .or_default()
                .reserve(budget_id.clone(), reserved_tokens, at);
            let context = route_id
                .and_then(|id| inner.routes.get(id))
                .map(TaskRoute::context)
                .unwrap_or_default();
            let mut counts = lock(&backend.counters);
            counts.attempts += 1;
            counts.in_flight += 1;
            inner.stats.attempts += 1;
            inner.stats.in_flight += 1;
            if let Some(route) = route_id.and_then(|id| inner.routes.get_mut(id)) {
                route.last_provider = Some(backend.provider.id.clone());
                *route
                    .attempts
                    .entry(backend.provider.id.clone())
                    .or_default() += 1;
            }
            context
        };
        let id = runtime.telemetry.as_ref().and_then(|store| {
            store
                .begin_with_context(
                    &backend.provider.id,
                    request_id,
                    if compact { "compact" } else { "response" },
                    &context,
                )
                .ok()
        });
        if let Some(store) = &runtime.telemetry {
            if store
                .reserve_token_budget(&BudgetEntry {
                    id: budget_id.clone(),
                    provider: backend.provider.id.clone(),
                    time: at,
                    tokens: reserved_tokens,
                    estimated: true,
                })
                .is_err()
            {
                tracing::warn!("event=telemetry_unavailable operation=reserve_token_budget");
            }
        }
        Ok(Self {
            runtime,
            backend,
            id,
            request_id: request_id.into(),
            status: None,
            budget_id,
            reserved_tokens,
            finished: false,
        })
    }
    fn status(&mut self, status: u16) {
        self.status = Some(status);
        *lock(&self.backend.counters)
            .statuses
            .entry(status.to_string())
            .or_default() += 1;
    }
    fn finish(&mut self, outcome: &str, reason: &str, usage: Option<&Usage>) {
        if self.finished {
            return;
        }
        self.finished = true;
        let at = token_budget::now();
        let known_rejection = self
            .status
            .is_some_and(|status| !(200..300).contains(&status));
        let entry = BudgetEntry {
            id: self.budget_id.clone(),
            provider: self.backend.provider.id.clone(),
            time: at,
            tokens: usage
                .map(|usage| {
                    usage
                        .total_tokens
                        .max(usage.input_tokens.saturating_add(usage.output_tokens))
                })
                .unwrap_or(if known_rejection {
                    0
                } else {
                    self.reserved_tokens
                }),
            estimated: usage.is_none(),
        };
        {
            let mut inner = lock(&self.runtime.inner);
            inner
                .token_budgets
                .entry(self.backend.provider.id.clone())
                .or_default()
                .finish(&self.budget_id, Some(entry.clone()), at);
            let mut counts = lock(&self.backend.counters);
            counts.in_flight = counts.in_flight.saturating_sub(1);
            inner.stats.in_flight = inner.stats.in_flight.saturating_sub(1);
            match outcome {
                "completed" => {
                    counts.completed += 1;
                    inner.stats.responses_completed += 1;
                }
                "client_disconnected" => {
                    counts.cancellations += 1;
                    inner.stats.client_disconnects += 1;
                }
                "upstream_interrupted" => {
                    counts.stream_interruptions += 1;
                    inner.stats.stream_interruptions += 1;
                }
                _ => {}
            }
        }
        if let Some(store) = &self.runtime.telemetry {
            if store.finish_token_budget(&entry).is_err() {
                tracing::warn!("event=telemetry_unavailable operation=finish_token_budget");
            }
        }
        if let (Some(store), Some(id)) = (&self.runtime.telemetry, &self.id) {
            if store
                .finish(id, self.status, outcome, reason, usage)
                .is_err()
            {
                tracing::warn!("event=telemetry_unavailable operation=finish");
            }
        }
        self.runtime.routes_changed.notify_waiters();
        tracing::info!(event="request_finished",provider=%self.backend.provider.id,request_id=%self.request_id,outcome,reason);
    }
}
impl Drop for AttemptGuard {
    fn drop(&mut self) {
        if !self.finished {
            self.finish("client_disconnected", "client_disconnect", None);
        }
    }
}

struct WaitGuard(Arc<Runtime>);
impl Drop for WaitGuard {
    fn drop(&mut self) {
        let mut inner = lock(&self.0.inner);
        inner.stats.waiting_requests = inner.stats.waiting_requests.saturating_sub(1);
    }
}

async fn responses(State(runtime): State<Arc<Runtime>>, request: Request) -> Response {
    if let Some(denied) = runtime.authenticate(request.headers()) {
        return denied;
    }
    let request_id = uuid::Uuid::new_v4().simple().to_string();
    let config = runtime.config();
    if request
        .headers()
        .get("content-encoding")
        .is_some_and(|value| value != "identity")
    {
        return api_error(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "unsupported_content_encoding",
            "Send uncompressed JSON.",
            &request_id,
        );
    }
    let compact = request.uri().path().ends_with("/compact");
    let public_route_id = request
        .uri()
        .path()
        .strip_prefix("/r/")
        .and_then(|path| path.split('/').next())
        .map(str::to_owned);
    let resolved = if let Some(id) = &public_route_id {
        runtime.resolve_route(id, request.headers()).await
    } else {
        None
    };
    if public_route_id.is_some() && resolved.is_none() {
        return api_error(
            StatusCode::CONFLICT,
            "route_unavailable",
            "Task route is unavailable. Resume the task from the panel.",
            &request_id,
        );
    }
    let (route_id, route) = resolved
        .map(|(id, route)| (Some(id), Some(route)))
        .unwrap_or((None, None));
    let beta = request
        .headers()
        .get("openai-beta")
        .filter(|value| value.as_bytes().len() <= 512)
        .cloned();
    let permit = match tokio::time::timeout(
        Duration::from_secs_f64(config.settings.pool_timeout_seconds),
        config.permits.clone().acquire_owned(),
    )
    .await
    {
        Ok(Ok(permit)) => permit,
        _ => {
            return api_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "proxy_busy",
                "The proxy has reached its concurrent request limit.",
                &request_id,
            )
        }
    };
    let bytes = match tokio::time::timeout(
        Duration::from_secs_f64(config.settings.write_timeout_seconds),
        to_bytes(request.into_body(), config.settings.max_request_bytes),
    )
    .await
    {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(_)) => {
            return api_error(
                StatusCode::PAYLOAD_TOO_LARGE,
                "request_too_large",
                "Request body exceeds the configured limit.",
                &request_id,
            )
        }
        Err(_) => {
            return api_error(
                StatusCode::REQUEST_TIMEOUT,
                "request_timeout",
                "Request body was not received in time.",
                &request_id,
            )
        }
    };
    let payload = match serde_json::from_slice::<Value>(&bytes) {
        Ok(value)
            if value.is_object()
                && value.get("stream").is_none_or(Value::is_boolean)
                && value.get("max_output_tokens").is_none_or(|number| {
                    number
                        .as_u64()
                        .is_some_and(|number| number > 0 && number <= 200_000)
                }) =>
        {
            value
        }
        _ => {
            return api_error(
                StatusCode::BAD_REQUEST,
                "invalid_json",
                "Expected a JSON object, boolean stream flag and valid output token limit.",
                &request_id,
            )
        }
    };
    drop(bytes);
    lock(&runtime.inner).stats.requests += 1;
    forward(
        runtime, config, payload, compact, request_id, beta, route_id, route, permit,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn forward(
    runtime: Arc<Runtime>,
    config: Arc<LiveConfig>,
    payload: Value,
    compact: bool,
    request_id: String,
    beta: Option<HeaderValue>,
    route_id: Option<String>,
    route: Option<TaskRoute>,
    permit: OwnedSemaphorePermit,
) -> Response {
    let mut wait_remaining = route
        .as_ref()
        .and_then(|route| route.spec.wait_seconds)
        .unwrap_or(config.settings.cooldown_wait_seconds);
    let mut last_status = StatusCode::SERVICE_UNAVAILABLE;
    let mut attempts = 0;
    let estimated_tokens = token_budget::estimate_request(
        &payload,
        route
            .as_ref()
            .map(|route| route.spec.max_output_tokens)
            .unwrap_or(0),
        compact,
    );
    let mut budget_wait_recorded = false;
    let mut previous: Option<String> = config
        .providers
        .iter()
        .filter(|backend| {
            route
                .as_ref()
                .is_none_or(|route| route.spec.providers.contains(&backend.provider.id))
        })
        .find(|backend| lock(&backend.counters).reconnect_pending)
        .map(|backend| backend.provider.id.clone());
    loop {
        let mut order = runtime.order(&config, route_id.as_deref());
        // A concurrent route edit also cannot enlarge an already running
        // request's provider cohort. New requests pick up the edited route.
        if let Some(snapshot) = &route {
            order.retain(|backend| snapshot.spec.providers.contains(&backend.provider.id));
        }
        {
            let inner = lock(&runtime.inner);
            let at = token_budget::now();
            // Soft pressure outweighs sticky routing for the next request only.
            order.sort_by_key(|backend| inner.budget_status(&backend.provider, at).soft_reached);
        }
        let role_routed = route
            .as_ref()
            .is_some_and(|route| !route.spec.role.is_empty());
        let mut attempted = Vec::new();
        for candidate in &order {
            let reserved;
            let backend = if role_routed {
                let candidates: Vec<_> = order
                    .iter()
                    .filter(|backend| !attempted.contains(&backend.provider.id))
                    .cloned()
                    .collect();
                reserved = runtime.reserve(
                    route_id.as_deref().unwrap_or_default(),
                    &candidates,
                    estimated_tokens,
                );
                let Some(backend) = &reserved else { break };
                backend
            } else {
                candidate
            };
            attempted.push(backend.provider.id.clone());
            if !backend.usable() {
                continue;
            }
            {
                let mut inner = lock(&runtime.inner);
                if !inner.can_admit(&backend.provider, estimated_tokens, token_budget::now()) {
                    inner.stats.token_budget_skips += 1;
                    continue;
                }
            }
            let reconnect_pending;
            {
                let mut counts = lock(&backend.counters);
                if counts
                    .cooldown_until
                    .is_some_and(|until| until > Instant::now())
                {
                    counts.cooldown_skips += 1;
                    if counts.reconnect_pending && previous.is_none() {
                        previous = Some(backend.provider.id.clone());
                    }
                    continue;
                }
                reconnect_pending = counts.reconnect_pending;
                counts.reconnect_pending = false;
            }
            if attempts >= MAX_ATTEMPTS {
                break;
            }
            attempts += 1;
            if let Some(old) = &previous {
                if *old != backend.provider.id {
                    let reconnect = reconnect_pending
                        || config.providers.iter().any(|item| {
                            item.provider.id == *old && lock(&item.counters).reconnect_pending
                        });
                    let mut inner = lock(&runtime.inner);
                    inner.stats.failovers += 1;
                    if reconnect {
                        inner.stats.reconnect_failovers += 1;
                    }
                    drop(inner);
                    for item in &config.providers {
                        if item.provider.id == *old {
                            lock(&item.counters).reconnect_pending = false;
                        }
                    }
                    runtime.event(
                        if reconnect {
                            "reconnect_failover"
                        } else {
                            "failover"
                        },
                        old,
                        &backend.provider.id,
                        "retry",
                        &request_id,
                    );
                }
            }
            previous = Some(backend.provider.id.clone());
            if config.settings.rotate_on_failure && route_id.is_none() {
                lock(&runtime.inner).preferred = backend.provider.id.clone();
            }
            let mut body = payload.clone();
            body["model"] = Value::String(backend.provider.deployment.clone());
            if !compact {
                if let Some(route) = &route {
                    if route.spec.max_output_tokens > 0 {
                        body["max_output_tokens"] = json!(body
                            .get("max_output_tokens")
                            .and_then(Value::as_u64)
                            .unwrap_or(route.spec.max_output_tokens)
                            .min(route.spec.max_output_tokens));
                    }
                }
            }
            let mut url = match network::validate_url(
                &backend.provider.base_url,
                config.settings.allow_loopback_upstreams,
            ) {
                Ok(url) => url,
                Err(_) => continue,
            };
            url.set_path(&format!(
                "{}/responses{}",
                url.path().trim_end_matches('/'),
                if compact { "/compact" } else { "" }
            ));
            if !backend.provider.api_version.is_empty() {
                url.query_pairs_mut()
                    .append_pair("api-version", &backend.provider.api_version);
            }
            let mut upstream = config
                .client
                .post(url)
                .header("accept-encoding", "identity")
                .header(
                    "accept",
                    if payload["stream"] == true && !compact {
                        "text/event-stream"
                    } else {
                        "application/json"
                    },
                )
                .json(&body);
            if let Some(beta) = &beta {
                upstream = upstream.header("openai-beta", beta);
            }
            // Header values containing credentials are marked sensitive before entering reqwest.
            let Some(key) = &backend.key else {
                continue;
            };
            let secret = Zeroizing::new(if backend.provider.auth_type == "bearer" {
                format!("Bearer {}", key.as_str())
            } else {
                key.to_string()
            });
            let mut credential = match HeaderValue::from_str(&secret) {
                Ok(value) => value,
                Err(_) => continue,
            };
            credential.set_sensitive(true);
            upstream = upstream.header(
                if backend.provider.auth_type == "bearer" {
                    "authorization"
                } else {
                    "api-key"
                },
                credential,
            );
            let mut guard = match AttemptGuard::begin(
                runtime.clone(),
                backend.clone(),
                &request_id,
                compact,
                route_id.as_deref(),
                estimated_tokens,
            ) {
                Ok(guard) => guard,
                Err(AttemptRejected::TokenBudget) => continue,
                Err(AttemptRejected::RouteUnavailable) => {
                    return api_error(
                        StatusCode::CONFLICT,
                        "route_unavailable",
                        "Task route has been released.",
                        &request_id,
                    )
                }
            };
            // reqwest's inactivity limit covers reads; this also bounds request upload/header wait.
            let header_budget = Duration::from_secs_f64(
                config.settings.connect_timeout_seconds
                    + config.settings.write_timeout_seconds
                    + config.settings.read_timeout_seconds,
            );
            let mut response = match tokio::time::timeout(header_budget, upstream.send()).await {
                Ok(Ok(response)) => response,
                error => {
                    let timeout =
                        error.is_err() || matches!(&error,Ok(Err(error)) if error.is_timeout());
                    last_status = if timeout {
                        StatusCode::GATEWAY_TIMEOUT
                    } else {
                        StatusCode::BAD_GATEWAY
                    };
                    let reason = if timeout {
                        "upstream_timeout"
                    } else {
                        "upstream_connection_error"
                    };
                    lock(&backend.counters).transport_errors += 1;
                    runtime.cooldown(
                        backend,
                        &config,
                        &failure(reason, last_status.as_u16()),
                        &HeaderMap::new(),
                        false,
                        route_id.as_deref(),
                        &request_id,
                    );
                    guard.finish("failed", reason, None);
                    continue;
                }
            };
            let status = response.status();
            guard.status(status.as_u16());
            if matches!(status.as_u16(), 408 | 429 | 500 | 502 | 503 | 504) {
                last_status = status;
                let reason = format!("http_{}", status.as_u16());
                runtime.cooldown(
                    backend,
                    &config,
                    &failure(&reason, status.as_u16()),
                    response.headers(),
                    false,
                    route_id.as_deref(),
                    &request_id,
                );
                guard.finish("failed", &reason, None);
                continue;
            }
            if !status.is_success() {
                guard.finish("failed", "upstream_rejected_request", None);
                // Redirect locations, upstream text, headers and error details are never returned.
                let downstream = if status.is_client_error() {
                    status
                } else {
                    StatusCode::BAD_GATEWAY
                };
                return api_error(
                    downstream,
                    "upstream_rejected_request",
                    "The upstream rejected this request.",
                    &request_id,
                );
            }
            if response
                .headers()
                .get("content-encoding")
                .is_some_and(|value| value != "identity")
            {
                guard.finish("failed", "unsupported_upstream_encoding", None);
                return api_error(
                    StatusCode::BAD_GATEWAY,
                    "invalid_upstream_response",
                    "The upstream returned an unsupported response encoding.",
                    &request_id,
                );
            }
            let is_sse = response
                .headers()
                .get("content-type")
                .and_then(|value| value.to_str().ok())
                .is_some_and(|value| {
                    value
                        .split(';')
                        .next()
                        .is_some_and(|mime| mime.trim().eq_ignore_ascii_case("text/event-stream"))
                });
            if is_sse {
                let first = match response.chunk().await {
                    Ok(Some(first)) => first,
                    Ok(None) => {
                        last_status = StatusCode::BAD_GATEWAY;
                        guard.finish("failed", "empty_upstream_stream", None);
                        continue;
                    }
                    Err(error) => {
                        last_status = if error.is_timeout() {
                            StatusCode::GATEWAY_TIMEOUT
                        } else {
                            StatusCode::BAD_GATEWAY
                        };
                        lock(&backend.counters).transport_errors += 1;
                        guard.finish("failed", "upstream_first_byte_error", None);
                        runtime.rotate(
                            backend,
                            &config,
                            route_id.as_deref(),
                            "upstream_first_byte_error",
                            &request_id,
                        );
                        continue;
                    }
                };
                let mut preflight = ResponseStreamObserver::new(EVENT_LIMIT);
                if config.settings.reconnect_failover {
                    if let Some(failure) = preflight.feed(&first) {
                        last_status =
                            StatusCode::from_u16(failure.status).unwrap_or(StatusCode::BAD_GATEWAY);
                        runtime.cooldown(
                            backend,
                            &config,
                            &failure,
                            response.headers(),
                            false,
                            route_id.as_deref(),
                            &request_id,
                        );
                        guard.finish("failed", &failure.reason, preflight.usage.as_ref());
                        continue;
                    }
                }
                return stream_response(
                    runtime,
                    config,
                    backend.clone(),
                    response,
                    first,
                    guard,
                    permit,
                    route_id,
                    request_id,
                );
            }
            let mut data = Vec::new();
            let result = tokio::time::timeout(
                Duration::from_secs_f64(config.settings.max_stream_seconds),
                async {
                    while let Some(chunk) =
                        response.chunk().await.map_err(|_| "upstream_body_error")?
                    {
                        if data.len().saturating_add(chunk.len())
                            > config.settings.max_response_bytes
                        {
                            return Err("upstream_response_too_large");
                        }
                        data.extend_from_slice(&chunk);
                    }
                    Ok(())
                },
            )
            .await;
            if !matches!(result, Ok(Ok(()))) {
                let reason = match result {
                    Ok(Err(reason)) => reason,
                    _ => "upstream_body_timeout",
                };
                guard.finish("failed", reason, None);
                return api_error(
                    StatusCode::BAD_GATEWAY,
                    "invalid_upstream_response",
                    "The upstream response could not be read within configured limits.",
                    &request_id,
                );
            }
            let value = match serde_json::from_slice::<Value>(&data) {
                Ok(value) if value.is_object() => value,
                _ => {
                    guard.finish("failed", "invalid_upstream_json", None);
                    return api_error(
                        StatusCode::BAD_GATEWAY,
                        "invalid_upstream_response",
                        "The upstream returned invalid JSON.",
                        &request_id,
                    );
                }
            };
            let usage = value.get("usage").and_then(normalize_usage);
            if value.get("error").is_some_and(|error| !error.is_null())
                || value["status"] == "failed"
            {
                guard.finish("failed", "upstream_response_failed", usage.as_ref());
                return api_error(
                    StatusCode::BAD_GATEWAY,
                    "upstream_response_failed",
                    "The upstream could not complete this request.",
                    &request_id,
                );
            }
            guard.finish("completed", "upstream_eof", usage.as_ref());
            lock(&runtime.inner).stats.bytes_forwarded += data.len() as u64;
            let mut result = Response::new(Body::from(data));
            *result.status_mut() = status;
            result
                .headers_mut()
                .insert("content-type", HeaderValue::from_static("application/json"));
            result
                .headers_mut()
                .insert("cache-control", HeaderValue::from_static("no-store"));
            if let Ok(value) = HeaderValue::from_str(&request_id) {
                result.headers_mut().insert("x-request-id", value);
            }
            return result;
        }
        let eligible: Vec<_> = order.iter().filter(|backend| backend.usable()).collect();
        let now = Instant::now();
        let (all_blocked, budget_blocked, oversized, delay) = {
            let inner = lock(&runtime.inner);
            let at = token_budget::now();
            let states: Vec<_> = eligible
                .iter()
                .map(|backend| {
                    let cooling = lock(&backend.counters)
                        .cooldown_until
                        .map(|until| until.saturating_duration_since(now).as_secs_f64())
                        .unwrap_or(0.0);
                    let budget_blocked = !inner.can_admit(&backend.provider, estimated_tokens, at);
                    let oversized =
                        estimated_tokens > inner.budget_status(&backend.provider, at).hard_limit;
                    let budget_delay = if budget_blocked {
                        inner
                            .budget_retry_after(&backend.provider, estimated_tokens, at)
                            .max(0.001)
                    } else {
                        0.0
                    };
                    (
                        cooling > 0.0 || budget_blocked,
                        budget_blocked,
                        oversized,
                        cooling.max(budget_delay),
                    )
                })
                .collect();
            (
                !states.is_empty() && states.iter().all(|state| state.0),
                states.iter().any(|state| state.1),
                !states.is_empty() && states.iter().all(|state| state.2),
                states
                    .iter()
                    .filter(|state| !state.2)
                    .map(|state| state.3)
                    .min_by(f64::total_cmp)
                    .unwrap_or(0.0),
            )
        };
        if all_blocked {
            if oversized {
                lock(&runtime.inner).stats.exhausted += 1;
                return api_error(StatusCode::TOO_MANY_REQUESTS, "token_budget_exceeded",
                    "The request estimate exceeds every selected API's hard token limit. Reduce the context or output limit, or adjust the API limits.", &request_id);
            }
            if wait_remaining > 0.0 && attempts < MAX_ATTEMPTS {
                let delay = delay.min(wait_remaining).max(0.001);
                {
                    let mut inner = lock(&runtime.inner);
                    inner.stats.waiting_requests += 1;
                    if budget_blocked {
                        inner.stats.token_budget_waits += 1;
                    } else {
                        inner.stats.cooldown_waits += 1;
                    }
                }
                if budget_blocked && !budget_wait_recorded {
                    budget_wait_recorded = true;
                    runtime.budget_event(
                        "token_budget_wait",
                        "rolling_minute",
                        &request_id,
                        route.as_ref(),
                    );
                }
                let waiting = WaitGuard(runtime.clone());
                let start = Instant::now();
                // A terminal run releases its waiting request promptly too.
                // The small cap also closes the notify registration race.
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs_f64(delay.min(1.0))) => {},
                    _ = runtime.routes_changed.notified() => {},
                }
                drop(waiting);
                wait_remaining = (wait_remaining - start.elapsed().as_secs_f64()).max(0.0);
                continue;
            }
            lock(&runtime.inner).stats.exhausted += 1;
            if budget_blocked {
                runtime.budget_event(
                    "token_budget_blocked",
                    "rolling_minute",
                    &request_id,
                    route.as_ref(),
                );
            }
            let cooling_status = if budget_blocked
                || eligible
                    .iter()
                    .any(|backend| lock(&backend.counters).cooldown_status == 429)
            {
                StatusCode::TOO_MANY_REQUESTS
            } else if last_status == StatusCode::GATEWAY_TIMEOUT {
                StatusCode::GATEWAY_TIMEOUT
            } else {
                StatusCode::SERVICE_UNAVAILABLE
            };
            let mut result = api_error(
                cooling_status,
                if budget_blocked {
                    "token_budget_exhausted"
                } else {
                    "upstream_unavailable"
                },
                if budget_blocked {
                    "Selected APIs have reached their rolling token budget. Retry after the indicated delay."
                } else {
                    "All configured upstreams are cooling down."
                },
                &request_id,
            );
            if let Ok(value) = HeaderValue::from_str(&delay.ceil().max(1.0).to_string()) {
                result.headers_mut().insert("retry-after", value);
            }
            return result;
        }
        lock(&runtime.inner).stats.exhausted += 1;
        return api_error(
            last_status,
            "upstream_unavailable",
            "No upstream is available for this request.",
            &request_id,
        );
    }
}

fn failure(reason: &str, status: u16) -> StreamFailure {
    StreamFailure {
        reason: reason.into(),
        status,
        retry_headers: BTreeMap::new(),
    }
}

pub(super) fn retry_after_seconds(headers: &HeaderMap, fallback: f64) -> f64 {
    if let Some(value) = headers
        .get("retry-after")
        .and_then(|value| value.to_str().ok())
    {
        if let Ok(seconds) = value.trim().parse::<f64>() {
            if seconds.is_finite() && seconds >= 0.0 {
                return seconds.min(86400.0);
            }
        }
        if let Ok(date) = httpdate::parse_http_date(value) {
            return date
                .duration_since(SystemTime::now())
                .unwrap_or_default()
                .as_secs_f64()
                .min(86400.0);
        }
    }
    for header in ["retry-after-ms", "x-ms-retry-after-ms"] {
        if let Some(value) = headers
            .get(header)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.trim().parse::<f64>().ok())
        {
            if value.is_finite() && value >= 0.0 {
                return (value / 1000.0).min(86400.0);
            }
        }
    }
    fallback
}

// SSE events are bounded and inspected before forwarding, so explicit error events
// can never leak upstream diagnostics split across network chunks. Content deltas
// are forwarded unchanged, including text that happens to mention an error code.
#[derive(Default)]
struct EventGate {
    frame: Vec<u8>,
    line_length: usize,
    previous_cr: bool,
}

impl EventGate {
    fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Bytes>, ()> {
        let mut frames: Vec<Bytes> = Vec::new();
        for &byte in chunk {
            if frames.len() >= 4096 {
                return Err(());
            }
            // Delay dispatch of CR until the next byte disambiguates CRLF.
            if self.previous_cr {
                if byte == b'\n' {
                    self.frame.push(byte);
                    if self.frame.len() > EVENT_LIMIT {
                        return Err(());
                    }
                }
                self.previous_cr = false;
                if let Some(frame) = self.newline() {
                    frames.push(frame);
                }
                if byte == b'\n' {
                    continue;
                }
            }
            self.frame.push(byte);
            if self.frame.len() > EVENT_LIMIT {
                return Err(());
            }
            if byte == b'\r' {
                self.previous_cr = true;
            } else if byte == b'\n' {
                if let Some(frame) = self.newline() {
                    frames.push(frame);
                }
            } else {
                self.line_length += 1;
            }
        }
        Ok(frames)
    }
    fn newline(&mut self) -> Option<Bytes> {
        let empty = self.line_length == 0;
        self.line_length = 0;
        empty.then(|| Bytes::from(std::mem::take(&mut self.frame)))
    }
    fn finish(&mut self) -> Vec<Bytes> {
        if self.previous_cr {
            self.previous_cr = false;
            self.newline().into_iter().collect()
        } else {
            Vec::new()
        }
    }
}

fn error_event() -> Bytes {
    Bytes::from_static(b"event: error\ndata: {\"type\":\"error\",\"code\":\"upstream_stream_interrupted\",\"message\":\"The upstream stream could not complete. This response was not retried.\"}\n\n")
}

fn explicit_error(frame: &[u8]) -> bool {
    let text = String::from_utf8_lossy(frame);
    let mut event = String::new();
    let mut data = String::new();
    for line in text.split(['\n', '\r']) {
        if let Some(value) = line.strip_prefix("event:") {
            event = value.trim().into();
        }
        if let Some(value) = line.strip_prefix("data:") {
            data.push_str(value.trim_start_matches(' '));
            data.push('\n');
        }
    }
    // The SSE event field is authoritative even when an inconsistent JSON type
    // tries to disguise an explicit diagnostic as an ordinary content delta.
    if event == "error" || event == "response.failed" {
        return true;
    }
    if let Ok(value) = serde_json::from_str::<Value>(&data) {
        let kind = value.get("type").and_then(Value::as_str).unwrap_or(&event);
        kind == "error"
            || kind == "response.failed"
            || value.get("error").is_some_and(|error| !error.is_null())
            || value
                .get("response")
                .and_then(|response| response.get("error"))
                .is_some_and(|error| !error.is_null())
    } else {
        event == "error" || event == "response.failed"
    }
}

#[allow(clippy::too_many_arguments)]
fn stream_response(
    runtime: Arc<Runtime>,
    config: Arc<LiveConfig>,
    backend: Arc<Backend>,
    response: reqwest::Response,
    first: Bytes,
    mut guard: AttemptGuard,
    permit: OwnedSemaphorePermit,
    route_id: Option<String>,
    request_id: String,
) -> Response {
    let status = response.status();
    let headers = response.headers().clone();
    let downstream_id = request_id.clone();
    let stream = async_stream::stream! {
        // The permit and guard are dropped even if the downstream closes before its first poll.
        let _permit=permit;
        let mut upstream=response.bytes_stream();
        let mut pending=Some(first);
        let mut observer=ResponseStreamObserver::new(EVENT_LIMIT);
        let mut gate=EventGate::default();
        let mut total=0usize;
        let deadline=tokio::time::Instant::now()+Duration::from_secs_f64(config.settings.max_stream_seconds);
        let mut interrupted:Option<&str>=None;
        loop {
            let item=if let Some(first)=pending.take() {Some(Ok(first))} else {
                match tokio::time::timeout_at(deadline,upstream.next()).await {
                    Ok(value)=>value,
                    Err(_)=>{interrupted=Some("stream_duration_limit");break;}
                }
            };
            let (chunk,eof)=match item {
                Some(Ok(chunk))=>(chunk,false),
                Some(Err(_))=>{interrupted=Some("stream_connection_error");break;}
                None=>(Bytes::new(),true),
            };
            total=total.saturating_add(chunk.len());
            if total>config.settings.max_stream_bytes {interrupted=Some("stream_size_limit");break;}
            let frames=if eof {gate.finish()} else {match gate.feed(&chunk) {Ok(frames)=>frames,Err(())=>{interrupted=Some("stream_event_limit");break;}}};
            for frame in frames {
                let event_failure=observer.feed(&frame);
                if let Some(failure)=event_failure {
                    runtime.cooldown(&backend,&config,&failure,&headers,true,route_id.as_deref(),&request_id);
                    guard.finish("upstream_interrupted",&failure.reason,observer.usage.as_ref());
                    if !config.settings.reconnect_failover {yield Ok::<Bytes,std::io::Error>(error_event());}
                    return;
                }
                if explicit_error(&frame) {
                    guard.finish("failed","upstream_stream_failed",observer.usage.as_ref());
                    yield Ok::<Bytes,std::io::Error>(error_event());
                    return;
                }
                lock(&runtime.inner).stats.bytes_forwarded+=frame.len() as u64;
                if observer.terminal {
                    guard.finish("completed",&observer.terminal_event,observer.usage.as_ref());
                    yield Ok::<Bytes,std::io::Error>(frame);
                    // A terminal response ends the stream; a malicious upstream cannot
                    // keep the local connection alive forever or append a second answer.
                    return;
                }
                yield Ok::<Bytes,std::io::Error>(frame);
            }
            if eof {break;}
        }
        let reason=interrupted.unwrap_or("stream_closed_before_completion");
        runtime.cooldown(&backend,&config,&failure(reason,502),&headers,true,route_id.as_deref(),&request_id);
        guard.finish("upstream_interrupted",reason,observer.usage.as_ref());
        if !config.settings.reconnect_failover {yield Ok::<Bytes,std::io::Error>(error_event());}
    };
    let mut result = Response::new(Body::from_stream(stream));
    *result.status_mut() = status;
    result.headers_mut().insert(
        "content-type",
        HeaderValue::from_static("text/event-stream"),
    );
    result
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static("no-store"));
    result
        .headers_mut()
        .insert("x-accel-buffering", HeaderValue::from_static("no"));
    if let Ok(value) = HeaderValue::from_str(&downstream_id) {
        result.headers_mut().insert("x-request-id", value);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn retry_after_is_bounded_and_nonfinite_falls_back() {
        let mut headers = HeaderMap::new();
        headers.insert("retry-after", HeaderValue::from_static("NaN"));
        assert_eq!(retry_after_seconds(&headers, 60.0), 60.0);
        headers.insert("retry-after", HeaderValue::from_static("999999999999"));
        assert_eq!(retry_after_seconds(&headers, 60.0), 86400.0);
        headers.insert("retry-after", HeaderValue::from_static("0"));
        assert_eq!(retry_after_seconds(&headers, 60.0), 0.0);
    }
    #[test]
    fn gate_fragments_frames_without_rewriting_content() {
        for newline in ["\n", "\r\n", "\r"] {
            let frame=format!("event: response.output_text.delta{newline}data: {{\"delta\":\"zażółć\"}}{newline}{newline}");
            let mut gate = EventGate::default();
            let mut output = Vec::new();
            for byte in frame.as_bytes() {
                output.extend(gate.feed(&[*byte]).unwrap().into_iter().flatten());
            }
            output.extend(gate.finish().into_iter().flatten());
            assert_eq!(output, frame.as_bytes());
        }
    }
    #[test]
    fn gate_rejects_oversized_frames_and_only_sanitizes_error_events() {
        assert!(EventGate::default()
            .feed(&vec![b'x'; EVENT_LIMIT + 1])
            .is_err());
        assert!(explicit_error(
            b"event: error\ndata: {\"message\":\"private-diagnostic\"}\n\n"
        ));
        assert!(explicit_error(
            b"event: error\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"private-diagnostic\"}\n\n"
        ));
        assert!(explicit_error(
            b"event: response.failed\r\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"private-diagnostic\"}\r\n\r\n"
        ));
        assert!(!explicit_error(
            b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"error rate limit\"}\n\n"
        ));
    }

    struct MockServer {
        base_url: String,
        job: tokio::task::JoinHandle<()>,
    }
    impl Drop for MockServer {
        fn drop(&mut self) {
            self.job.abort();
        }
    }
    impl MockServer {
        async fn start(app: Router) -> Self {
            let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
                .await
                .unwrap();
            let base_url = format!("http://{}", listener.local_addr().unwrap());
            let job = tokio::spawn(async move {
                let _ = axum::serve(listener, app).await;
            });
            Self { base_url, job }
        }
    }
    fn test_runtime(urls: &[String]) -> Arc<Runtime> {
        let mut settings = Settings {
            allow_loopback_upstreams: true,
            reconnect_failover: true,
            max_connections: 1,
            read_timeout_seconds: 2.0,
            ..Settings::default()
        };
        settings.providers = urls
            .iter()
            .enumerate()
            .map(|(i, url)| Provider {
                id: format!("mock{i}"),
                base_url: url.clone(),
                deployment: "deployment-under-test".into(),
                ..Provider::default()
            })
            .collect();
        let providers = settings
            .providers
            .iter()
            .map(|provider| {
                Arc::new(Backend {
                    provider: provider.clone(),
                    key: Some(Zeroizing::new("upstream-test-key".into())),
                    counters: Arc::new(Mutex::new(Counters::default())),
                })
            })
            .collect();
        let client = network::client(&settings).unwrap();
        let config = Arc::new(LiveConfig {
            settings,
            providers,
            client,
            token: Some(Zeroizing::new("local-test-token".into())),
            permits: Arc::new(Semaphore::new(1)),
        });
        Arc::new(Runtime {
            inner: Mutex::new(Inner {
                config,
                routes: HashMap::new(),
                released_runs: HashSet::new(),
                token_budgets: HashMap::new(),
                stats: Stats::default(),
                preferred: "mock0".into(),
            }),
            telemetry: None,
            config_path: PathBuf::new(),
            started: Instant::now(),
            reload_lock: tokio::sync::Mutex::new(()),
            routes_changed: tokio::sync::Notify::new(),
        })
    }
    fn request(stream: bool) -> Request {
        Request::builder()
            .method("POST")
            .uri("/v1/responses")
            .header("authorization", "Bearer local-test-token")
            .header("content-type", "application/json")
            .body(Body::from(
                json!({"stream":stream,"input":"local test only"}).to_string(),
            ))
            .unwrap()
    }
    const CREATED:&[u8]=b"event: response.created\ndata: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_test\"}}\n\n";
    const COMPLETE:&[u8]=b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_test\",\"usage\":{\"input_tokens\":2,\"output_tokens\":3}}}\n\n";

    #[tokio::test]
    async fn dropping_an_unpolled_response_releases_request_slot_and_accounting() {
        let mock = MockServer::start(Router::new().route(
            "/responses",
            post(|| async {
                let stream = async_stream::stream! {
                    yield Ok::<Bytes,std::io::Error>(Bytes::from_static(CREATED));
                    std::future::pending::<()>().await;
                };
                (
                    [("content-type", "text/event-stream")],
                    Body::from_stream(stream),
                )
            }),
        ))
        .await;
        let runtime = test_runtime(std::slice::from_ref(&mock.base_url));
        let response = responses(State(runtime.clone()), request(true)).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(runtime.status()["stats"]["in_flight"], 1);
        assert_eq!(runtime.config().permits.available_permits(), 0);
        drop(response);
        assert_eq!(runtime.status()["stats"]["in_flight"], 0);
        assert_eq!(runtime.status()["stats"]["client_disconnects"], 1);
        assert_eq!(runtime.config().permits.available_permits(), 1);
    }

    #[tokio::test]
    async fn mid_stream_failure_never_splices_another_provider_and_next_request_fails_over() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let release = Arc::new(tokio::sync::Notify::new());
        let first_calls = Arc::new(AtomicUsize::new(0));
        let release_server = release.clone();
        let first_counter = first_calls.clone();
        let first=MockServer::start(Router::new().route("/responses",post(move || {
            let release=release_server.clone();let counter=first_counter.clone();
            async move {
                counter.fetch_add(1,Ordering::SeqCst);
                let stream=async_stream::stream! {
                    yield Ok::<Bytes,std::io::Error>(Bytes::from_static(CREATED));
                    release.notified().await;
                    yield Ok::<Bytes,std::io::Error>(Bytes::from_static(b"event: error\r\ndata: {\"type\":\"error\",\"code\":\"rate_limit_exceeded\",\"message\":\"UPSTREAM_PRIVATE_DETAIL\"}\r\n\r\n"));
                };
                ([("content-type","text/event-stream")],Body::from_stream(stream))
            }
        }))).await;
        let second_calls = Arc::new(AtomicUsize::new(0));
        let second_counter = second_calls.clone();
        let second = MockServer::start(Router::new().route(
            "/responses",
            post(move || {
                let counter = second_counter.clone();
                async move {
                    counter.fetch_add(1, Ordering::SeqCst);
                    (
                        [("content-type", "text/event-stream")],
                        Bytes::from_static(COMPLETE),
                    )
                }
            }),
        ))
        .await;
        let runtime = test_runtime(&[first.base_url.clone(), second.base_url.clone()]);
        let response = responses(State(runtime.clone()), request(true)).await;
        let mut body = response.into_body().into_data_stream();
        assert_eq!(body.next().await.unwrap().unwrap().as_ref(), CREATED);
        release.notify_one();
        assert!(body.next().await.is_none());
        assert_eq!(first_calls.load(Ordering::SeqCst), 1);
        assert_eq!(second_calls.load(Ordering::SeqCst), 0);
        drop(body);
        let second_response = responses(State(runtime.clone()), request(true)).await;
        let bytes = to_bytes(second_response.into_body(), EVENT_LIMIT)
            .await
            .unwrap();
        assert_eq!(bytes.as_ref(), COMPLETE);
        assert_eq!(second_calls.load(Ordering::SeqCst), 1);
        assert_eq!(runtime.status()["stats"]["stream_interruptions"], 1);
        assert_eq!(runtime.status()["stats"]["responses_completed"], 1);
        assert_eq!(runtime.status()["stats"]["in_flight"], 0);
    }

    #[tokio::test]
    async fn auth_is_required_before_body_or_upstream_work() {
        let runtime = test_runtime(&["http://127.0.0.1:1".into()]);
        let mut req = request(false);
        req.headers_mut().insert(
            "authorization",
            HeaderValue::from_static("Bearer wrong-local-token"),
        );
        let response = responses(State(runtime.clone()), req).await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(runtime.status()["stats"]["attempts"], 0);
        assert_eq!(runtime.config().permits.available_permits(), 1);
    }

    #[tokio::test]
    async fn malformed_output_limit_is_rejected_before_upstream_work() {
        let runtime = test_runtime(&["http://127.0.0.1:1".into()]);
        for limit in [
            json!({"unexpected":"object"}),
            json!(true),
            json!(-1),
            json!(0),
            json!(200_001),
        ] {
            let mut req = request(false);
            *req.body_mut() = Body::from(json!({"max_output_tokens":limit}).to_string());
            let response = responses(State(runtime.clone()), req).await;
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        }
        assert_eq!(runtime.status()["stats"]["attempts"], 0);
        assert_eq!(runtime.config().permits.available_permits(), 1);
    }

    #[tokio::test]
    async fn token_reservations_are_atomic_between_concurrent_requests() {
        let runtime = test_runtime(&["http://127.0.0.1:1".into()]);
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let mut jobs = Vec::new();
        for index in 0..2 {
            let runtime = runtime.clone();
            let barrier = barrier.clone();
            jobs.push(std::thread::spawn(move || {
                let backend = runtime.config().providers[0].clone();
                barrier.wait();
                AttemptGuard::begin(
                    runtime,
                    backend,
                    &format!("concurrent-{index}"),
                    false,
                    None,
                    600_000,
                )
            }));
        }
        let mut admitted = Vec::new();
        for job in jobs {
            if let Ok(guard) = job.join().unwrap() {
                admitted.push(guard);
            }
        }
        assert_eq!(admitted.len(), 1);
        assert_eq!(
            runtime.status()["providers"][0]["token_budget"]["reserved_tokens"],
            600_000
        );
        let usage = normalize_usage(&json!({"input_tokens":120,"output_tokens":30})).unwrap();
        admitted[0].finish("completed", "upstream_eof", Some(&usage));
        let status = runtime.status();
        assert_eq!(status["providers"][0]["token_budget"]["reserved_tokens"], 0);
        assert_eq!(status["providers"][0]["token_budget"]["used_tokens"], 150);
        assert_eq!(status["stats"]["attempts"], 1);
    }

    #[tokio::test]
    async fn soft_token_pressure_outweighs_sticky_roles_but_allows_safe_fallback() {
        let runtime = test_runtime(&["http://127.0.0.1:1".into(), "http://127.0.0.1:2".into()]);
        let mut inner = lock(&runtime.inner);
        let route = TaskRoute {
            spec: RouteSpec {
                id: "task".into(),
                providers: vec!["mock0".into(), "mock1".into()],
                role: "main".into(),
                ..RouteSpec::default()
            },
            preferred: "mock0".into(),
            assigned: Some("mock0".into()),
            last_provider: None,
            attempts: BTreeMap::new(),
        };
        inner
            .token_budgets
            .entry("mock0".into())
            .or_default()
            .restore(BudgetEntry {
                id: "usage-a".into(),
                provider: "mock0".into(),
                time: token_budget::now(),
                tokens: 900_000,
                estimated: false,
            });
        assert_eq!(
            choose_assignment(&inner, &route, &inner.config.providers, 8192)
                .unwrap()
                .provider
                .id,
            "mock1"
        );
        inner
            .token_budgets
            .entry("mock1".into())
            .or_default()
            .restore(BudgetEntry {
                id: "usage-b".into(),
                provider: "mock1".into(),
                time: token_budget::now(),
                tokens: 950_000,
                estimated: false,
            });
        assert_eq!(
            choose_assignment(&inner, &route, &inner.config.providers, 8192)
                .unwrap()
                .provider
                .id,
            "mock0"
        );
        assert!(choose_assignment(&inner, &route, &inner.config.providers, 50_001).is_none());
    }

    #[tokio::test]
    async fn dropping_an_unreported_attempt_preserves_only_a_temporary_estimate() {
        let runtime = test_runtime(&["http://127.0.0.1:1".into()]);
        let backend = runtime.config().providers[0].clone();
        let guard = AttemptGuard::begin(
            runtime.clone(),
            backend.clone(),
            "cancelled",
            false,
            None,
            8192,
        )
        .unwrap();
        drop(guard);
        assert_eq!(
            runtime.status()["providers"][0]["token_budget"]["reserved_tokens"],
            0
        );
        assert_eq!(
            runtime.status()["providers"][0]["token_budget"]["estimated_tokens"],
            8192
        );
        let mut rejected =
            AttemptGuard::begin(runtime.clone(), backend, "rejected", false, None, 4096).unwrap();
        rejected.status(429);
        rejected.finish("failed", "http_429", None);
        assert_eq!(
            runtime.status()["providers"][0]["token_budget"]["estimated_tokens"],
            8192
        );
    }

    #[tokio::test]
    async fn removed_or_expanded_route_cannot_escape_an_existing_requests_provider_cohort() {
        for replace in [false, true] {
            let runtime = test_runtime(&["http://127.0.0.1:1".into(), "http://127.0.0.1:2".into()]);
            let snapshot = TaskRoute {
                spec: RouteSpec {
                    id: "task".into(),
                    providers: vec!["mock0".into()],
                    strategy: "priority".into(),
                    wait_seconds: Some(0.0),
                    max_output_tokens: 0,
                    ..RouteSpec::default()
                },
                preferred: "mock0".into(),
                last_provider: None,
                attempts: BTreeMap::new(),
                assigned: None,
            };
            if replace {
                // Simulate replacing a previously captured route during reload/wait.
                let mut replacement = snapshot.clone();
                replacement.spec.providers = vec!["mock1".into()];
                replacement.preferred = "mock1".into();
                lock(&runtime.inner)
                    .routes
                    .insert("task".into(), replacement);
            }
            let config = runtime.config();
            let permit = config.permits.clone().acquire_owned().await.unwrap();
            let response = forward(
                runtime.clone(),
                config,
                json!({"input":"local regression test"}),
                false,
                "test-route-reload".into(),
                None,
                Some("task".into()),
                Some(snapshot),
                permit,
            )
            .await;
            assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(runtime.status()["stats"]["attempts"], 0);
        }
    }
}
