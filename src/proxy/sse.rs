//! Bounded inspection of Responses SSE, independent of byte-for-byte forwarding.
//! The observer never emits upstream text or retains a complete response body.

use super::telemetry::{normalize_usage, Usage};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StreamFailure {
    pub reason: String,
    pub status: u16,
    pub retry_headers: BTreeMap<String, String>,
}

fn normalized(value: Option<&Value>) -> String {
    let value = match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Number(value)) => value.to_string(),
        _ => return String::new(),
    };
    value
        .chars()
        .flat_map(char::to_lowercase)
        .filter(|c| c.is_alphanumeric())
        .collect()
}

/// Inspect structured error events only. Model output mentioning a quota or a
/// rate limit must never trigger retries or a provider rotation.
pub fn classify_error(event: &Value, event_name: &str) -> Option<StreamFailure> {
    let object = event.as_object()?;
    let name = object
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or(event_name);
    if !matches!(name, "error" | "response.failed")
        && !matches!(event_name, "error" | "response.failed")
        && !object.contains_key("error")
    {
        return None;
    }
    let error = match object.get("response").and_then(Value::as_object) {
        Some(response) => response.get("error")?,
        None => object.get("error").unwrap_or(event),
    }
    .as_object()?;
    let codes: Vec<_> = ["code", "type", "status", "status_code"]
        .iter()
        .map(|key| normalized(error.get(*key)))
        .collect();
    let contains = |options: &[&str]| codes.iter().any(|code| options.contains(&code.as_str()));
    if contains(&[
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
        "invalidrequesterror",
        "invalidapikey",
        "authenticationerror",
    ]) {
        return None;
    }
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    let rate_limit = contains(&[
        "429",
        "ratelimitexceeded",
        "ratelimiterror",
        "ratelimit",
        "ratelimitreached",
        "toomanyrequests",
        "quotaexceeded",
        "insufficientquota",
        "tokenlimitexceeded",
    ]) || [
        "rate limit",
        "rate_limit",
        "too many requests",
        "quota exceeded",
        "exceeded your current quota",
        "token rate",
        "tokens per minute",
        "tokens-per-minute",
    ]
    .iter()
    .any(|term| message.contains(term));
    if rate_limit {
        let mut retry_headers = BTreeMap::new();
        for source in [object, error] {
            for (key, header) in [
                ("retry_after", "retry-after"),
                ("retry_after_seconds", "retry-after"),
                ("retry_after_ms", "retry-after-ms"),
            ] {
                let value = match source.get(key) {
                    Some(Value::String(value)) => value.clone(),
                    Some(Value::Number(value)) => value.to_string(),
                    _ => continue,
                };
                // Hints become HTTP header values elsewhere; reject injection
                // and excessive metadata before it leaves the observer.
                if value.len() <= 128
                    && value.bytes().all(|c| c.is_ascii() && !c.is_ascii_control())
                {
                    retry_headers.insert(header.to_owned(), value);
                }
            }
        }
        return Some(StreamFailure {
            reason: "sse_rate_limit".into(),
            status: 429,
            retry_headers,
        });
    }
    if contains(&[
        "408",
        "500",
        "502",
        "503",
        "504",
        "servererror",
        "internalservererror",
        "serviceunavailable",
        "timeout",
        "requesttimeout",
        "gatewaytimeout",
        "overloadederror",
    ]) {
        return Some(StreamFailure {
            reason: "sse_server_error".into(),
            status: 503,
            retry_headers: BTreeMap::new(),
        });
    }
    None
}

pub struct ResponseStreamObserver {
    max_event_bytes: usize,
    buffer: Vec<u8>,
    data: Vec<u8>,
    event_name: String,
    oversized: bool,
    dropping_line: bool,
    skip_lf: bool,
    pub terminal: bool,
    pub terminal_event: String,
    pub usage: Option<Usage>,
}

impl Default for ResponseStreamObserver {
    fn default() -> Self {
        Self::new(1024 * 1024)
    }
}

impl ResponseStreamObserver {
    pub fn new(max_event_bytes: usize) -> Self {
        Self {
            max_event_bytes: max_event_bytes.max(1),
            buffer: Vec::new(),
            data: Vec::new(),
            event_name: String::new(),
            oversized: false,
            dropping_line: false,
            skip_lf: false,
            terminal: false,
            terminal_event: String::new(),
            usage: None,
        }
    }

    fn dispatch(&mut self) -> Option<StreamFailure> {
        let event = if !self.oversized && !self.data.is_empty() {
            serde_json::from_slice::<Value>(&self.data).ok()
        } else {
            None
        };
        let name = event
            .as_ref()
            .and_then(|value| value.get("type"))
            .and_then(Value::as_str)
            .unwrap_or(&self.event_name)
            .to_owned();
        // A conflicting JSON `type` cannot downgrade an explicit SSE error.
        let name = if matches!(self.event_name.as_str(), "error" | "response.failed") {
            self.event_name.clone()
        } else {
            name
        };
        let mut failure = None;
        if !self.terminal {
            if let Some(ref event) = event {
                failure = classify_error(event, &self.event_name);
            }
            if matches!(
                name.as_str(),
                "response.completed" | "response.incomplete" | "response.failed" | "error"
            ) {
                self.terminal = true;
                self.terminal_event = name;
                self.usage = event
                    .as_ref()
                    .and_then(|value| value.get("response"))
                    .and_then(|value| value.get("usage"))
                    .and_then(normalize_usage);
            } else if self.data.trim_ascii() == b"[DONE]" {
                self.terminal = true;
                self.terminal_event = "done".into();
            }
        }
        self.event_name.clear();
        self.data.clear();
        self.oversized = false;
        failure
    }

    fn line(&mut self) -> Option<StreamFailure> {
        if self.dropping_line {
            self.dropping_line = false;
            self.buffer.clear();
            return None;
        }
        let result = if self.buffer.is_empty() {
            self.dispatch()
        } else {
            if let Some(value) = self.buffer.strip_prefix(b"event:") {
                let value = value.trim_ascii();
                self.event_name =
                    String::from_utf8_lossy(&value[..value.len().min(100)]).into_owned();
            } else if let Some(value) = self.buffer.strip_prefix(b"data:") {
                if !self.oversized {
                    let value = value.strip_prefix(b" ").unwrap_or(value);
                    if self
                        .data
                        .len()
                        .saturating_add(value.len())
                        .saturating_add(1)
                        > self.max_event_bytes
                    {
                        self.oversized = true;
                        self.data.clear();
                    } else {
                        self.data.extend_from_slice(value);
                        self.data.push(b'\n');
                    }
                }
            }
            None
        };
        self.buffer.clear();
        result
    }

    /// Process input without buffering its chunk wholesale. LF, CR and split
    /// CRLF delimiters are accepted; UTF-8 is decoded only after a full event.
    pub fn feed(&mut self, chunk: &[u8]) -> Option<StreamFailure> {
        if self.terminal {
            return None;
        }
        let mut failure = None;
        for &byte in chunk {
            if self.skip_lf {
                self.skip_lf = false;
                if byte == b'\n' {
                    continue;
                }
            }
            if byte == b'\r' || byte == b'\n' {
                self.skip_lf = byte == b'\r';
                if let Some(found) = self.line() {
                    failure = Some(found);
                }
                if self.terminal {
                    // A repeated terminal frame cannot overwrite the first
                    // accounting record or turn completed text into a retry.
                    break;
                }
            } else if !self.dropping_line {
                if self.buffer.len() >= self.max_event_bytes {
                    self.buffer.clear();
                    self.data.clear();
                    self.oversized = true;
                    self.dropping_line = true;
                } else {
                    self.buffer.push(byte);
                }
            }
        }
        failure
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn frame(value: Value) -> Vec<u8> {
        format!("data: {value}\n\n").into_bytes()
    }

    #[test]
    fn recognizes_structured_limits_and_retry_hints() {
        for value in [
            json!({"type":"error","error":{"code":"rate_limit_exceeded"}}),
            json!({"type":"response.failed","response":{"error":{"type":"RateLimitError"}}}),
            json!({"error":{"status_code":429}}),
            json!({"type":"error","message":"Tokens per minute exceeded"}),
        ] {
            let failure = classify_error(&value, "").unwrap();
            assert_eq!(failure.status, 429);
            assert_eq!(failure.reason, "sse_rate_limit");
        }
        let failure = classify_error(
            &json!({"type":"error","retry_after":9,
            "error":{"code":429,"retry_after_seconds":0.25,"retry_after_ms":"500"}}),
            "",
        )
        .unwrap();
        assert_eq!(failure.retry_headers["retry-after"], "0.25");
        assert_eq!(failure.retry_headers["retry-after-ms"], "500");
        let failure =
            classify_error(&json!({"type":"error","error":{"code":"server_error"}}), "").unwrap();
        assert_eq!(failure.status, 503);
    }

    #[test]
    fn ignores_model_text_request_errors_and_malformed_types() {
        for value in [
            json!([]),
            json!(null),
            json!("rate limit"),
            json!({"type":[],"response":[],"error":[]}),
            json!({"type":"response.output_text.delta","delta":"rate limit quota exceeded"}),
            json!({"type":"response.failed","response":{"error":{"code":400,"message":"quota exceeded"}}}),
            json!({"type":"error","error":{"code":"invalid_api_key","message":"rate limit"}}),
            json!({"type":"error","error":{"status_code":422,"message":"quota exceeded"}}),
        ] {
            assert!(classify_error(&value, "").is_none());
        }
        let mut observer = ResponseStreamObserver::default();
        assert!(observer
            .feed(b"data: {\"type\":[],\"response\":[]}\n\n")
            .is_none());
        assert!(!observer.terminal);
    }

    #[test]
    fn handles_all_fragment_boundaries_and_utf8() {
        let payload = json!({"type":"response.failed","response":{"error":{"code":"rate_limit_exceeded","message":"Zażółć gęślą jaźń"}}});
        for ending in ["\n", "\r", "\r\n"] {
            let bytes = format!("event: response.failed{ending}data: {payload}{ending}{ending}")
                .into_bytes();
            for split in 0..=bytes.len() {
                let mut observer = ResponseStreamObserver::default();
                let first = observer.feed(&bytes[..split]);
                let second = observer.feed(&bytes[split..]);
                assert_eq!(first.or(second).unwrap().status, 429, "split {split}");
                assert!(observer.terminal);
            }
            let mut observer = ResponseStreamObserver::default();
            let failures: Vec<_> = bytes
                .chunks(1)
                .filter_map(|chunk| observer.feed(chunk))
                .collect();
            assert_eq!(failures.len(), 1);
        }
    }

    #[test]
    fn captures_terminal_usage_once_and_supports_multiline_events() {
        let mut observer = ResponseStreamObserver::default();
        let content = b": keep-alive\nevent: response.completed\ndata: {\"response\":\ndata: {\"usage\":{\"input_tokens\":10,\"output_tokens\":5}}}\n\n";
        for chunk in content.chunks(3) {
            assert!(observer.feed(chunk).is_none());
        }
        assert!(observer.terminal);
        assert_eq!(observer.terminal_event, "response.completed");
        assert_eq!(observer.usage.as_ref().unwrap().total_tokens, 15);
        assert!(observer.feed(&frame(json!({"type":"response.failed","response":{"error":{"code":429},"usage":{"input_tokens":90,"output_tokens":10}}}))).is_none());
        assert_eq!(observer.usage.as_ref().unwrap().total_tokens, 15);
        assert_eq!(observer.terminal_event, "response.completed");
    }

    #[test]
    fn memory_is_bounded_and_recovers_after_oversized_frames() {
        let mut observer = ResponseStreamObserver::new(256);
        observer.feed(b"data: ");
        let large = vec![b'x'; 1024 * 1024];
        observer.feed(&large);
        assert!(observer.buffer.len() <= 256 && observer.data.len() <= 256);
        observer.feed(b"\n\n");
        for _ in 0..1000 {
            observer.feed(b"data: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n");
            assert!(observer.buffer.len() <= 256 && observer.data.len() <= 256);
        }
        observer.feed(b"\n");
        let failure = observer
            .feed(&frame(json!({"type":"error","error":{"code":429}})))
            .unwrap();
        assert_eq!(failure.status, 429);
    }

    #[test]
    fn accepts_done_incomplete_and_malformed_json_without_panicking() {
        for content in [
            b"data: [DONE]\n\n".as_slice(),
            b"data: {\"type\":\"response.incomplete\"}\n\n".as_slice(),
        ] {
            let mut observer = ResponseStreamObserver::default();
            assert!(observer.feed(content).is_none());
            assert!(observer.terminal);
            assert!(observer.usage.is_none());
        }
        let mut observer = ResponseStreamObserver::default();
        assert!(observer
            .feed(b"data: {invalid}\n\ndata: []\n\ndata: \xff\n\n")
            .is_none());
        assert!(!observer.terminal);
        assert!(observer.feed(b"data: [DONE]\n\n").is_none());
        assert!(observer.terminal);
    }

    #[test]
    fn retry_hints_reject_header_injection_booleans_and_oversize_values() {
        let value = json!({"type":"error","retry_after":true,
            "error":{"code":429,"retry_after_seconds":"1\r\nx-secret: private","retry_after_ms":"x".repeat(129)}});
        let failure = classify_error(&value, "").unwrap();
        assert!(failure.retry_headers.is_empty());
        assert_eq!(failure.reason, "sse_rate_limit");
    }

    #[test]
    fn explicit_error_event_cannot_be_downgraded_by_json_type() {
        for event_name in ["error", "response.failed"] {
            let mut observer = ResponseStreamObserver::default();
            let frame = format!("event: {event_name}\r\ndata: {{\"type\":\"response.output_text.delta\",\"code\":429}}\r\n\r\n");
            let failure = frame
                .as_bytes()
                .chunks(1)
                .find_map(|chunk| observer.feed(chunk))
                .unwrap();
            assert_eq!(failure.status, 429);
            assert!(observer.terminal);
            assert_eq!(observer.terminal_event, event_name);
        }
    }
}
