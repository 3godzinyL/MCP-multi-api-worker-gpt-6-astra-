//! A local browser session can reach the private Python worker only through this boundary.
use axum::{
    body::{to_bytes, Body},
    extract::{Request, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use futures_util::StreamExt;
use serde_json::json;
use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
use tokio::sync::Semaphore;

const COOKIE: &str = "three_api_gateway";
const PANEL_LIMIT: usize = 150_000;
const SESSION_LIFE: Duration = Duration::from_secs(8 * 3600);

#[derive(Clone)]
struct Gateway {
    backend: String,
    sidecar_token: Arc<String>,
    login_token: Arc<String>,
    client: reqwest::Client,
    sessions: Arc<Mutex<HashMap<String, Instant>>>,
    failed_logins: Arc<Mutex<VecDeque<Instant>>>,
}

#[derive(Clone)]
struct Boundary {
    port: u16,
    slots: Arc<Semaphore>,
}

pub fn protected(router: Router, port: u16) -> Router {
    router.layer(middleware::from_fn_with_state(
        Boundary {
            port,
            slots: Arc::new(Semaphore::new(128)),
        },
        boundary,
    ))
}

fn safe_headers(response: &mut Response) {
    for (name, value) in [
        ("x-content-type-options", "nosniff"), ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"), ("cross-origin-resource-policy", "same-origin"),
        ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
        ("cache-control", "no-store"),
        ("content-security-policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    ] { response.headers_mut().insert(name, HeaderValue::from_static(value)); }
}

fn error(status: StatusCode, code: &str) -> Response {
    (status, Json(json!({"error":code,"code":code}))).into_response()
}

fn valid_host(host: &str, port: u16) -> bool {
    host == format!("127.0.0.1:{port}") || host == format!("localhost:{port}")
}

async fn boundary(State(state): State<Boundary>, request: Request, next: Next) -> Response {
    let host = request
        .headers()
        .get(header::HOST)
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let origin = request
        .headers()
        .get(header::ORIGIN)
        .and_then(|h| h.to_str().ok());
    let rejected = if request.headers().get_all(header::HOST).iter().count() != 1
        || request.headers().get_all(header::ORIGIN).iter().count() > 1
        || (request.headers().contains_key(header::ORIGIN) && origin.is_none())
        || !valid_host(host, state.port)
        || origin.is_some_and(|o| o != format!("http://{host}"))
        || request
            .headers()
            .get("sec-fetch-site")
            .is_some_and(|v| v == "cross-site")
    {
        Some(error(StatusCode::FORBIDDEN, "invalid_origin"))
    } else if request.uri().path().len() > 4096 || request.headers().len() > 64 {
        Some(error(StatusCode::BAD_REQUEST, "invalid_request"))
    } else if request
        .headers()
        .get(header::CONTENT_ENCODING)
        .is_some_and(|v| v != "identity")
    {
        Some(error(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "unsupported_encoding",
        ))
    } else {
        None
    };
    let mut response = if let Some(r) = rejected {
        r
    } else {
        match state.slots.clone().try_acquire_owned() {
            Err(_) => error(StatusCode::TOO_MANY_REQUESTS, "too_many_requests"),
            Ok(permit) => {
                let result = next.run(request).await;
                let (parts, body) = result.into_parts();
                let stream = async_stream::stream! {
                    let _permit = permit;
                    let mut chunks = body.into_data_stream();
                    while let Some(chunk) = chunks.next().await { yield chunk; }
                };
                Response::from_parts(parts, Body::from_stream(stream))
            }
        }
    };
    safe_headers(&mut response);
    response
}

pub fn router(
    backend_port: u16,
    sidecar_token: String,
    login_token: String,
) -> anyhow::Result<Router> {
    let state = Gateway {
        backend: format!("http://127.0.0.1:{backend_port}"),
        sidecar_token: Arc::new(sidecar_token),
        login_token: Arc::new(login_token),
        client: reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(120))
            .build()?,
        sessions: Arc::new(Mutex::new(HashMap::new())),
        failed_logins: Arc::new(Mutex::new(VecDeque::new())),
    };
    Ok(routes(state))
}

fn routes(state: Gateway) -> Router {
    Router::new()
        .route("/health", get(|| async { Json(json!({"status":"ok","application":"3api-rust-panel","version":env!("CARGO_PKG_VERSION")})) }))
        .route("/auth/login", post(login))
        .route("/auth/logout", post(logout))
        .fallback(relay).with_state(state)
}

fn cookie_session(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(header::COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .find_map(|s| s.trim().strip_prefix(&format!("{COOKIE}=")))
}

fn authenticated(state: &Gateway, headers: &HeaderMap) -> bool {
    let Some(id) = cookie_session(headers) else {
        return false;
    };
    state
        .sessions
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(id)
        .is_some_and(|expiry| *expiry > Instant::now())
}

fn establish_session(state: &Gateway, previous: Option<&str>) -> HeaderValue {
    let now = Instant::now();
    let mut sessions = state.sessions.lock().unwrap_or_else(|e| e.into_inner());
    sessions.retain(|_, expires| *expires > now);
    // Reuse a live session so refreshing one tab does not invalidate another.
    let id = if let Some(id) = previous.filter(|id| sessions.contains_key(*id)) {
        id.to_owned()
    } else {
        if sessions.len() >= 64 {
            if let Some(oldest) = sessions
                .iter()
                .min_by_key(|(_, t)| **t)
                .map(|(k, _)| k.clone())
            {
                sessions.remove(&oldest);
            }
        }
        uuid::Uuid::new_v4().simple().to_string() + &uuid::Uuid::new_v4().simple().to_string()
    };
    sessions.insert(id.clone(), now + SESSION_LIFE);
    HeaderValue::from_str(&format!(
        "{COOKIE}={id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={}",
        SESSION_LIFE.as_secs()
    ))
    .expect("generated ASCII cookie")
}

async fn login(State(state): State<Gateway>, request: Request) -> Response {
    // The global Origin check rejects a mismatching origin; login also requires it to exist.
    if !request.headers().contains_key(header::ORIGIN) {
        return error(StatusCode::FORBIDDEN, "invalid_origin");
    }
    {
        let mut attempts = state
            .failed_logins
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        while attempts
            .front()
            .is_some_and(|i| i.elapsed() > Duration::from_secs(60))
        {
            attempts.pop_front();
        }
        if attempts.len() >= 10 {
            return error(StatusCode::TOO_MANY_REQUESTS, "login_rate_limit");
        }
    }
    let raw =
        match tokio::time::timeout(Duration::from_secs(10), to_bytes(request.into_body(), 8192))
            .await
        {
            Ok(Ok(raw)) => raw,
            _ => return error(StatusCode::BAD_REQUEST, "invalid_request"),
        };
    let payload: serde_json::Value = serde_json::from_slice(&raw).unwrap_or_default();
    let token = payload.get("token").and_then(|v| v.as_str()).unwrap_or("");
    if !bool::from(token.as_bytes().ct_eq(state.login_token.as_bytes())) || token.is_empty() {
        state
            .failed_logins
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push_back(Instant::now());
        return error(StatusCode::UNAUTHORIZED, "invalid_token");
    }
    let mut result = Json(json!({"ok":true})).into_response();
    result
        .headers_mut()
        .insert(header::SET_COOKIE, establish_session(&state, None));
    result
}

async fn logout(State(state): State<Gateway>, request: Request) -> Response {
    if !request.headers().contains_key(header::ORIGIN) {
        return error(StatusCode::FORBIDDEN, "invalid_origin");
    }
    if let Some(id) = cookie_session(request.headers()) {
        state
            .sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(id);
    }
    (
        [(
            header::SET_COOKIE,
            "three_api_gateway=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
        )],
        Json(json!({"ok":true})),
    )
        .into_response()
}

async fn relay(State(state): State<Gateway>, request: Request) -> Response {
    if request.uri().path().starts_with("/internal/") {
        return error(StatusCode::NOT_FOUND, "not_found");
    }
    // Host, Origin and Fetch Metadata were verified by the public boundary. The
    // local landing page also renews expired sessions without exposing a secret.
    // API requests still need both the gateway session and the worker CSRF guard.
    let landing = matches!(request.uri().path(), "/" | "/ui" | "/ui/") && request.method() == "GET";
    if !landing && !authenticated(&state, request.headers()) {
        return error(StatusCode::UNAUTHORIZED, "gateway_session_expired");
    }
    if !matches!(
        request.method().as_str(),
        "GET" | "POST" | "HEAD" | "DELETE"
    ) {
        return error(StatusCode::METHOD_NOT_ALLOWED, "method_not_allowed");
    }
    let (parts, body) = request.into_parts();
    let raw = match tokio::time::timeout(Duration::from_secs(15), to_bytes(body, PANEL_LIMIT)).await
    {
        Ok(Ok(raw)) => raw,
        Ok(Err(_)) => return error(StatusCode::PAYLOAD_TOO_LARGE, "body_too_large"),
        Err(_) => return error(StatusCode::REQUEST_TIMEOUT, "request_timeout"),
    };
    let path = parts
        .uri
        .path_and_query()
        .map(|v| v.as_str())
        .unwrap_or("/");
    // Concatenation onto a fixed authority; no client-controlled scheme, host or port.
    let mut outbound = state
        .client
        .request(parts.method, format!("{}{path}", state.backend))
        .header("x-3api-sidecar-token", state.sidecar_token.as_str())
        .body(raw);
    for name in [
        "host",
        "origin",
        "cookie",
        "content-type",
        "x-panel-csrf",
        "accept",
    ] {
        if let Some(value) = parts.headers.get(name) {
            outbound = outbound.header(name, value);
        }
    }
    let upstream = match outbound.send().await {
        Ok(r) => r,
        Err(_) => return error(StatusCode::BAD_GATEWAY, "worker_unavailable"),
    };
    let status = upstream.status();
    let mut headers = HeaderMap::new();
    for name in ["content-type", "set-cookie", "location", "cache-control"] {
        for value in upstream.headers().get_all(name) {
            headers.append(name, value.clone());
        }
    }
    if landing && (status.is_success() || status.is_redirection()) {
        // Append: the worker response includes its independent session cookie.
        headers.append(
            header::SET_COOKIE,
            establish_session(&state, cookie_session(&parts.headers)),
        );
    }
    // UI responses are bounded too: avoid an unbounded worker response keeping the browser open.
    let stream = async_stream::stream! {
        let mut total = 0usize;
        let mut chunks = upstream.bytes_stream();
        while let Some(item) = chunks.next().await {
            match item {
                Ok(chunk) if total.saturating_add(chunk.len()) <= 32 * 1024 * 1024 => { total += chunk.len(); yield Ok::<_, std::io::Error>(chunk); }
                _ => { yield Err(std::io::Error::other("Worker response interrupted")); break; }
            }
        }
    };
    (status, headers, Body::from_stream(stream)).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::Request;
    use tower::ServiceExt;

    fn app() -> Router {
        protected(
            router(65500, "private-worker".into(), "test-login-token".into()).unwrap(),
            4101,
        )
    }

    async fn worker_app() -> (Router, Gateway, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let worker = Router::new().fallback(|request: Request<Body>| async move {
            assert_eq!(request.headers()["x-3api-sidecar-token"], "private-worker");
            if request.uri().path() == "/ui/" {
                (
                    [(
                        header::SET_COOKIE,
                        "three_api_panel=worker; Path=/ui; HttpOnly; SameSite=Strict",
                    )],
                    axum::response::Html(
                        "<meta name=\"csrf-token\" content=\"private-csrf\"><main>Workspace</main>",
                    ),
                )
                    .into_response()
            } else {
                Json(json!({"ok":true})).into_response()
            }
        });
        let job = tokio::spawn(async move {
            axum::serve(listener, worker).await.unwrap();
        });
        let state = Gateway {
            backend: format!("http://127.0.0.1:{port}"),
            sidecar_token: Arc::new("private-worker".into()),
            login_token: Arc::new("test-login-token".into()),
            client: reqwest::Client::builder().no_proxy().build().unwrap(),
            sessions: Arc::new(Mutex::new(HashMap::new())),
            failed_logins: Arc::new(Mutex::new(VecDeque::new())),
        };
        (protected(routes(state.clone()), 4101), state, job)
    }

    fn local_request(path: &str) -> axum::http::request::Builder {
        Request::builder()
            .uri(path)
            .header("host", "127.0.0.1:4101")
    }

    #[tokio::test]
    async fn landing_page_establishes_both_sessions_without_a_token() {
        let (app, _, worker) = worker_app().await;
        let response = app
            .clone()
            .oneshot(local_request("/ui/").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let cookies = response
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .map(|v| v.to_str().unwrap().to_string())
            .collect::<Vec<_>>();
        assert_eq!(cookies.len(), 2);
        assert!(cookies.iter().any(|v| v.starts_with("three_api_panel=")));
        let gateway_cookie = cookies.iter().find(|v| v.starts_with(COOKIE)).unwrap();
        assert!(gateway_cookie.contains("HttpOnly; SameSite=Strict"));
        assert!(!gateway_cookie.contains("test-login-token"));
        let content =
            String::from_utf8(to_bytes(response.into_body(), 4096).await.unwrap().to_vec())
                .unwrap();
        assert!(content.contains("csrf-token"));
        assert!(content.contains("Workspace"));
        assert!(!content.contains("test-login-token"));
        let response = app
            .oneshot(
                local_request("/ui/api/state")
                    .header(header::COOKIE, gateway_cookie.split(';').next().unwrap())
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        worker.abort();
    }

    #[tokio::test]
    async fn expired_session_renews_on_same_origin_landing_page() {
        let (app, state, worker) = worker_app().await;
        state
            .sessions
            .lock()
            .unwrap()
            .insert("expired".into(), Instant::now() - Duration::from_secs(1));
        let expired = format!("{COOKIE}=expired");
        let response = app
            .clone()
            .oneshot(
                local_request("/ui/api/state")
                    .header(header::COOKIE, &expired)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let response = app
            .clone()
            .oneshot(
                local_request("/ui/")
                    .header(header::COOKIE, &expired)
                    .header(header::ORIGIN, "http://127.0.0.1:4101")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert!(response
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .any(|v| v.to_str().unwrap().starts_with(COOKIE)
                && !v.to_str().unwrap().contains("expired")));
        assert!(!state.sessions.lock().unwrap().contains_key("expired"));
        let response = app
            .oneshot(
                local_request("/ui/")
                    .header(header::ORIGIN, "http://evil.test")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert!(!response.headers().contains_key(header::SET_COOKIE));
        worker.abort();
    }

    #[tokio::test]
    async fn repeated_page_load_preserves_existing_tabs_and_worker_failure_does_not_create_session()
    {
        let (app, state, worker) = worker_app().await;
        state
            .sessions
            .lock()
            .unwrap()
            .insert("live-tab".into(), Instant::now() + SESSION_LIFE);
        let response = app
            .clone()
            .oneshot(
                local_request("/ui/")
                    .header(header::COOKIE, format!("{COOKIE}=live-tab"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(response
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .any(|v| v
                .to_str()
                .unwrap()
                .starts_with(&format!("{COOKIE}=live-tab;"))));
        worker.abort();
        let _ = worker.await;
        let response = app
            .oneshot(local_request("/ui/").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
        assert!(!response.headers().contains_key(header::SET_COOKIE));
        let content =
            String::from_utf8(to_bytes(response.into_body(), 4096).await.unwrap().to_vec())
                .unwrap();
        assert!(content.contains("worker_unavailable"));
        assert!(!content.contains("private-worker"));
    }

    #[tokio::test]
    async fn internal_routes_and_cross_site_landing_never_reach_worker() {
        for (path, cross_site, expected) in [
            ("/internal/shutdown", false, StatusCode::NOT_FOUND),
            ("/ui/", true, StatusCode::FORBIDDEN),
        ] {
            let mut request = local_request(path);
            if cross_site {
                request = request.header("sec-fetch-site", "cross-site");
            }
            let response = app()
                .oneshot(request.body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), expected);
        }
    }
    #[tokio::test]
    async fn host_origin_and_encoding_are_checked_before_handlers() {
        for (host, origin, encoding, expected) in [
            ("evil.test:4101", None, None, 403),
            ("127.0.0.1:4001", None, None, 403),
            ("127.0.0.1:4101", Some("http://evil.test"), None, 403),
            ("127.0.0.1:4101", None, Some("gzip"), 415),
            ("localhost:4101", None, None, 200),
        ] {
            let mut req = Request::builder().uri("/health").header("host", host);
            if let Some(v) = origin {
                req = req.header("origin", v)
            }
            if let Some(v) = encoding {
                req = req.header("content-encoding", v)
            }
            let response = app()
                .oneshot(req.body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status().as_u16(), expected);
            assert_eq!(response.headers()["x-frame-options"], "DENY");
        }
    }
    #[tokio::test]
    async fn ambiguous_host_or_origin_cannot_establish_a_session() {
        for ambiguous in ["host", "origin", "invalid-origin"] {
            let mut request = local_request("/ui/").body(Body::empty()).unwrap();
            if ambiguous == "host" {
                request
                    .headers_mut()
                    .append(header::HOST, HeaderValue::from_static("localhost:4101"));
            } else if ambiguous == "origin" {
                for _ in 0..2 {
                    request.headers_mut().append(
                        header::ORIGIN,
                        HeaderValue::from_static("http://127.0.0.1:4101"),
                    );
                }
            } else {
                request.headers_mut().insert(
                    header::ORIGIN,
                    HeaderValue::from_bytes(b"http://invalid.\xff").unwrap(),
                );
            }
            let response = app().oneshot(request).await.unwrap();
            assert_eq!(response.status(), StatusCode::FORBIDDEN);
            assert!(!response.headers().contains_key(header::SET_COOKIE));
        }
    }
    #[tokio::test]
    async fn private_worker_is_inaccessible_before_login() {
        let response = app()
            .oneshot(
                Request::builder()
                    .uri("/ui/api/state")
                    .header("host", "127.0.0.1:4101")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }
    #[tokio::test]
    async fn login_needs_origin_and_a_valid_token() {
        for (origin, token, expected) in [
            (false, "test-login-token", 403),
            (true, "wrong", 401),
            (true, "test-login-token", 200),
        ] {
            let mut req = Request::builder()
                .method("POST")
                .uri("/auth/login")
                .header("host", "127.0.0.1:4101");
            if origin {
                req = req.header("origin", "http://127.0.0.1:4101")
            }
            let response = app()
                .oneshot(
                    req.body(Body::from(json!({"token":token}).to_string()))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status().as_u16(), expected);
            if expected == 200 {
                let cookie = response.headers()["set-cookie"].to_str().unwrap();
                assert!(cookie.contains("HttpOnly"));
                assert!(cookie.contains("SameSite=Strict"));
                assert!(!cookie.contains(token));
            }
        }
    }
}
