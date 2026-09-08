//! Rolling admission accounting. Estimates reserve capacity, never become usage.
use super::Provider;
use serde::Serialize;
use serde_json::Value;
use std::{
    collections::HashMap,
    time::{SystemTime, UNIX_EPOCH},
};

pub(super) const WINDOW_SECONDS: f64 = 60.0;
const DEFAULT_OUTPUT_RESERVATION: u64 = 8192;

pub(super) fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

#[derive(Clone)]
pub(super) struct BudgetEntry {
    pub id: String,
    pub provider: String,
    pub time: f64,
    pub tokens: u64,
    pub estimated: bool,
}

#[derive(Default)]
pub(super) struct TokenWindow {
    entries: Vec<BudgetEntry>,
    reservations: HashMap<String, u64>,
}

#[derive(Serialize)]
pub(super) struct BudgetStatus {
    pub limit: u64,
    pub soft_limit: u64,
    pub hard_limit: u64,
    pub used_tokens: u64,
    pub reserved_tokens: u64,
    pub estimated_tokens: u64,
    pub total_tokens: u64,
    pub soft_reached: bool,
    pub hard_reached: bool,
    pub retry_after_seconds: f64,
    pub window_seconds: u64,
}

impl TokenWindow {
    pub fn restore(&mut self, entry: BudgetEntry) {
        self.entries.push(entry);
    }

    pub fn status(&self, provider: &Provider, at: f64) -> BudgetStatus {
        let mut used_tokens: u64 = 0;
        let mut estimated_tokens: u64 = 0;
        for entry in self
            .entries
            .iter()
            .filter(|entry| entry.time > at - WINDOW_SECONDS)
        {
            if entry.estimated {
                estimated_tokens = estimated_tokens.saturating_add(entry.tokens);
            } else {
                used_tokens = used_tokens.saturating_add(entry.tokens);
            }
        }
        let reserved_tokens = self
            .reservations
            .values()
            .fold(0_u64, |sum, value| sum.saturating_add(*value));
        let total_tokens = used_tokens
            .saturating_add(estimated_tokens)
            .saturating_add(reserved_tokens);
        BudgetStatus {
            limit: provider.tokens_per_minute,
            soft_limit: provider.soft_tokens_per_minute,
            hard_limit: provider.hard_tokens_per_minute,
            used_tokens,
            reserved_tokens,
            estimated_tokens,
            total_tokens,
            soft_reached: total_tokens >= provider.soft_tokens_per_minute,
            hard_reached: total_tokens >= provider.hard_tokens_per_minute,
            retry_after_seconds: self.delay_for(provider, 0, at, total_tokens),
            window_seconds: WINDOW_SECONDS as u64,
        }
    }

    pub fn can_admit(&self, provider: &Provider, estimate: u64, at: f64) -> bool {
        let total = self.status(provider, at).total_tokens;
        total < provider.hard_tokens_per_minute
            && total.saturating_add(estimate) <= provider.hard_tokens_per_minute
    }

    pub fn retry_after(&self, provider: &Provider, estimate: u64, at: f64) -> f64 {
        self.delay_for(
            provider,
            estimate,
            at,
            self.status(provider, at).total_tokens,
        )
    }

    fn delay_for(&self, provider: &Provider, estimate: u64, at: f64, mut total: u64) -> f64 {
        let fits = |total: u64| {
            total < provider.hard_tokens_per_minute
                && total.saturating_add(estimate) <= provider.hard_tokens_per_minute
        };
        if fits(total) {
            return 0.0;
        }
        let mut entries: Vec<_> = self
            .entries
            .iter()
            .filter(|entry| entry.time > at - WINDOW_SECONDS)
            .collect();
        entries.sort_by(|a, b| a.time.total_cmp(&b.time));
        for entry in entries {
            total = total.saturating_sub(entry.tokens);
            if fits(total) {
                // An active request may reconcile earlier than historical usage expires.
                let delay = (entry.time + WINDOW_SECONDS - at).max(0.001);
                return if self.reservations.is_empty() {
                    delay
                } else {
                    delay.min(1.0)
                };
            }
        }
        // Active requests do not expire mid-stream. Recheck after completion or a short wait.
        1.0
    }

    pub fn reserve(&mut self, id: String, tokens: u64, at: f64) {
        self.entries
            .retain(|entry| entry.time > at - WINDOW_SECONDS);
        self.reservations.insert(id, tokens);
    }

    pub fn finish(&mut self, id: &str, entry: Option<BudgetEntry>, at: f64) {
        self.reservations.remove(id);
        self.entries
            .retain(|entry| entry.time > at - WINDOW_SECONDS);
        if let Some(entry) = entry.filter(|entry| entry.tokens > 0) {
            self.entries.push(entry);
        }
    }
}

/// Count only sizes; request text is never retained by accounting. UTF-8 / 3 is
/// an estimate, not a tokenizer. The explicit output cap (or 8192 tokens) is
/// reserved in addition to input/tool/schema sizes, then reconciled with usage.
pub(super) fn estimate_request(payload: &Value, output_cap: u64, compact: bool) -> u64 {
    fn size(value: &Value) -> u64 {
        match value {
            Value::String(text) => text.len() as u64,
            Value::Array(items) => items.iter().fold(0_u64, |sum, item| {
                sum.saturating_add(size(item)).saturating_add(8)
            }),
            Value::Object(fields) => fields.iter().fold(0_u64, |sum, (key, value)| {
                sum.saturating_add(key.len() as u64)
                    .saturating_add(size(value))
                    .saturating_add(8)
            }),
            _ => 8,
        }
    }
    let input = size(payload).div_ceil(3);
    let output = if compact {
        DEFAULT_OUTPUT_RESERVATION
    } else {
        match (
            payload.get("max_output_tokens").and_then(Value::as_u64),
            output_cap,
        ) {
            (Some(requested), 0) => requested,
            (Some(requested), cap) => requested.min(cap),
            (None, 0) => DEFAULT_OUTPUT_RESERVATION,
            (None, cap) => cap,
        }
    };
    input.saturating_add(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(id: &str, time: f64, tokens: u64, estimated: bool) -> BudgetEntry {
        BudgetEntry {
            id: id.into(),
            provider: "mock".into(),
            time,
            tokens,
            estimated,
        }
    }

    #[test]
    fn rolling_window_expires_each_usage_sample_separately() {
        let provider = Provider::default();
        let mut budget = TokenWindow::default();
        budget.restore(entry("a", 100.0, 900_000, false));
        budget.restore(entry("b", 115.0, 50_000, false));
        assert!(budget.status(&provider, 159.0).hard_reached);
        assert_eq!(budget.retry_after(&provider, 1000, 159.0), 1.0);
        assert_eq!(budget.status(&provider, 160.0).used_tokens, 50_000);
        assert!(budget.can_admit(&provider, 1000, 160.0));
        assert_eq!(budget.status(&provider, 175.0).used_tokens, 0);
    }

    #[test]
    fn active_reservations_do_not_expire_and_reconcile_without_double_counting() {
        let provider = Provider::default();
        let mut budget = TokenWindow::default();
        budget.reserve("a".into(), 950_000, 100.0);
        assert!(!budget.can_admit(&provider, 1, 500.0));
        budget.finish("a", Some(entry("a", 500.0, 120, false)), 500.0);
        let status = budget.status(&provider, 500.0);
        assert_eq!(status.reserved_tokens, 0);
        assert_eq!(status.used_tokens, 120);
        assert_eq!(status.estimated_tokens, 0);
        budget.reserve("b".into(), 8192, 500.0);
        budget.finish("b", Some(entry("b", 501.0, 8192, true)), 501.0);
        assert_eq!(budget.status(&provider, 501.0).estimated_tokens, 8192);
        assert_eq!(budget.status(&provider, 561.0).total_tokens, 0);
    }

    #[test]
    fn admission_checks_the_whole_next_reservation_at_the_boundary() {
        let provider = Provider::default();
        let mut budget = TokenWindow::default();
        budget.restore(entry("a", 100.0, 949_000, false));
        assert!(budget.can_admit(&provider, 1000, 100.0));
        assert!(!budget.can_admit(&provider, 1001, 100.0));
        assert!(!TokenWindow::default().can_admit(&provider, 950_001, 100.0));
    }

    #[test]
    fn estimator_respects_output_limits_without_retaining_prompt_text() {
        let payload = serde_json::json!({"input":"test", "max_output_tokens":1000});
        assert_eq!(
            estimate_request(&payload, 200, false) + 800,
            estimate_request(&payload, 0, false)
        );
        assert!(estimate_request(&serde_json::json!({}), 0, false) >= DEFAULT_OUTPUT_RESERVATION);
    }
}
