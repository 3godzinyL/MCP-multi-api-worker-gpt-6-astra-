//! Credential-free accounting compatible with the Python dashboard database.
//!
//! Only identifiers, status codes, timestamps and normalized token counts enter
//! this module. Request bodies, response text and upstream error messages do not.

use super::token_budget::{BudgetEntry, WINDOW_SECONDS};
use anyhow::{anyhow, Result};
use rusqlite::{params, Connection, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::path::Path;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use uuid::Uuid;

const MAX_TOKEN_COUNT: u64 = 1_000_000_000_000_000;
const SNAPSHOT_EVENTS: i64 = 50;
const DATABASE_BUSY_TIMEOUT: Duration = Duration::from_secs(10);

/// Trusted routing identifiers only. The runtime supplies the actual thread's
/// assignment; telemetry never infers a role from request order or message text.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteContext {
    pub route_id: String,
    pub project_id: String,
    pub task_id: String,
    pub run_id: String,
    pub thread_id: String,
    pub role: String,
}

impl RouteContext {
    fn sanitized(&self) -> Self {
        Self {
            route_id: identifier(&self.route_id, 100).into(),
            project_id: identifier(&self.project_id, 100).into(),
            task_id: identifier(&self.task_id, 100).into(),
            run_id: identifier(&self.run_id, 100).into(),
            thread_id: identifier(&self.thread_id, 100).into(),
            role: identifier(&self.role, 30).into(),
        }
    }

    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            route_id: row.get("route_id")?,
            project_id: row.get("project_id")?,
            task_id: row.get("task_id")?,
            run_id: row.get("run_id")?,
            thread_id: row.get("thread_id")?,
            role: row.get("role")?,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub total_tokens: u64,
    pub cached_tokens: Option<u64>,
    pub reasoning_tokens: Option<u64>,
}

/// Reject negative, fractional, boolean and unreasonably large counts. Cache is
/// part of input, and reasoning is part of output; neither is added twice.
pub fn normalize_usage(value: &Value) -> Option<Usage> {
    let object = value.as_object()?;
    let count = |value: Option<&Value>| {
        value
            .and_then(Value::as_u64)
            .filter(|v| *v <= MAX_TOKEN_COUNT)
    };
    let input_tokens = count(object.get("input_tokens"))?;
    let output_tokens = count(object.get("output_tokens"))?;
    Some(Usage {
        input_tokens,
        output_tokens,
        total_tokens: count(object.get("total_tokens"))
            .unwrap_or(0)
            .max(input_tokens + output_tokens),
        cached_tokens: count(
            value
                .get("input_tokens_details")
                .and_then(|v| v.get("cached_tokens")),
        )
        .map(|v| v.min(input_tokens)),
        reasoning_tokens: count(
            value
                .get("output_tokens_details")
                .and_then(|v| v.get("reasoning_tokens")),
        )
        .map(|v| v.min(output_tokens)),
    })
}

fn timestamp() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

/// Free-form descriptions and control characters must not become telemetry.
fn identifier(value: &str, limit: usize) -> &str {
    if !value
        .bytes()
        .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c))
    {
        return "invalid_identifier";
    }
    &value[..value.len().min(limit)]
}

fn validate_budget_entry(entry: &BudgetEntry) -> Result<()> {
    if entry.id.is_empty()
        || identifier(&entry.id, 100) != entry.id
        || entry.provider.is_empty()
        || identifier(&entry.provider, 40) != entry.provider
        || !entry.time.is_finite()
        || entry.time < 0.0
        || entry.tokens > 2 * MAX_TOKEN_COUNT
    {
        return Err(anyhow!("Invalid token budget metadata"));
    }
    Ok(())
}

fn enable_wal(db: &Connection) -> Result<()> {
    // SQLite may skip the busy handler when changing journal mode, because
    // waiting for a lock upgrade can otherwise deadlock two opening processes.
    // Retry this idempotent pragma only; committed writes are never replayed.
    let started = Instant::now();
    let mut delay = Duration::from_millis(10);
    loop {
        match db.pragma_update(None, "journal_mode", "WAL") {
            Ok(()) => return Ok(()),
            Err(rusqlite::Error::SqliteFailure(error, _))
                if matches!(
                    error.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                ) && started.elapsed() < DATABASE_BUSY_TIMEOUT =>
            {
                std::thread::sleep(
                    delay.min(DATABASE_BUSY_TIMEOUT.saturating_sub(started.elapsed())),
                );
                delay = (delay * 2).min(Duration::from_millis(250));
            }
            Err(error) => return Err(error.into()),
        }
    }
}

pub struct TelemetryStore {
    db: Mutex<Connection>,
}

impl TelemetryStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let mut db = Connection::open(path)?;
        db.busy_timeout(DATABASE_BUSY_TIMEOUT)?;
        enable_wal(&db)?;
        // Serialize schema discovery and ALTER with the Python worker. A
        // deferred read transaction cannot safely upgrade while another writer
        // holds the database, even when busy_timeout is set.
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        transaction.execute_batch(
            "CREATE TABLE IF NOT EXISTS telemetry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS api_attempts (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, provider TEXT NOT NULL,
                kind TEXT NOT NULL, started REAL NOT NULL, finished REAL,
                status INTEGER, outcome TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER);
             CREATE INDEX IF NOT EXISTS attempts_finished ON api_attempts(finished);
             CREATE TABLE IF NOT EXISTS api_events (
                id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL,
                provider TEXT NOT NULL, peer TEXT NOT NULL, reason TEXT NOT NULL,
                request_id TEXT NOT NULL, tokens INTEGER);
             CREATE INDEX IF NOT EXISTS events_time ON api_events(time);
             CREATE TABLE IF NOT EXISTS api_token_budget (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, time REAL NOT NULL,
                tokens INTEGER NOT NULL CHECK(tokens>=0 AND tokens<=2000000000000000),
                state TEXT NOT NULL CHECK(state IN ('reserved','used','estimated')));
             CREATE INDEX IF NOT EXISTS token_budget_expiry ON api_token_budget(state,time);",
        )?;
        for table in ["api_attempts", "api_events"] {
            let columns = transaction
                .prepare(&format!("PRAGMA table_info({table})"))?
                .query_map([], |row| row.get::<_, String>(1))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for column in [
                "route_id",
                "project_id",
                "task_id",
                "run_id",
                "thread_id",
                "role",
            ] {
                if !columns.iter().any(|existing| existing == column) {
                    transaction.execute(
                        &format!(
                            "ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                        ),
                        [],
                    )?;
                }
            }
        }
        transaction.execute_batch(
            "CREATE INDEX IF NOT EXISTS attempts_request ON api_attempts(request_id,started);
             CREATE INDEX IF NOT EXISTS attempts_run ON api_attempts(run_id,started);
             CREATE INDEX IF NOT EXISTS events_run ON api_events(run_id,id);",
        )?;
        transaction.execute(
            "INSERT OR IGNORE INTO telemetry_meta(key,value) VALUES('created',?)",
            [timestamp().to_string()],
        )?;
        let budget_migrated: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM telemetry_meta WHERE key='token_budget_migrated_v1')",
            [],
            |row| row.get(0),
        )?;
        if !budget_migrated {
            // Only the first upgrade imports historical usage. Later opens
            // already have durable reservations and must not count it twice.
            let recent = transaction
                .prepare(
                    "SELECT provider,finished,total_tokens,input_tokens,output_tokens
                     FROM api_attempts WHERE finished>? AND total_tokens IS NOT NULL",
                )?
                .query_map([timestamp() - WINDOW_SECONDS], |row| {
                    let total = row.get::<_, u64>("total_tokens")?;
                    let input = row.get::<_, Option<u64>>("input_tokens")?.unwrap_or(0);
                    let output = row.get::<_, Option<u64>>("output_tokens")?.unwrap_or(0);
                    Ok(BudgetEntry {
                        // Fresh identifiers avoid copying any legacy free-form
                        // request identifier into admission accounting.
                        id: format!("legacy-{}", Uuid::new_v4().simple()),
                        provider: identifier(&row.get::<_, String>("provider")?, 40).into(),
                        time: row.get("finished")?,
                        tokens: total.max(input.saturating_add(output)),
                        estimated: false,
                    })
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for entry in recent {
                validate_budget_entry(&entry)?;
                if entry.tokens > 0 {
                    transaction.execute(
                        "INSERT INTO api_token_budget(id,provider,time,tokens,state) VALUES(?,?,?,?,'used')",
                        params![entry.id, entry.provider, entry.time, entry.tokens],
                    )?;
                }
            }
            transaction.execute(
                "INSERT INTO telemetry_meta(key,value) VALUES('token_budget_migrated_v1','1')",
                [],
            )?;
        }
        transaction.commit()?;
        Ok(Self { db: Mutex::new(db) })
    }

    fn lock(&self) -> Result<MutexGuard<'_, Connection>> {
        self.db
            .lock()
            .map_err(|_| anyhow!("Telemetry database lock is poisoned"))
    }

    pub(super) fn reserve_token_budget(&self, entry: &BudgetEntry) -> Result<()> {
        validate_budget_entry(entry)?;
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        // Streaming requests keep their reservation until they finish, even
        // when generation itself takes longer than the rolling window.
        transaction.execute(
            "DELETE FROM api_token_budget WHERE state<>'reserved' AND time<=?",
            [entry.time - WINDOW_SECONDS],
        )?;
        transaction.execute(
            "INSERT INTO api_token_budget(id,provider,time,tokens,state) VALUES(?,?,?,?,'reserved')
             ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,time=excluded.time,tokens=excluded.tokens
             WHERE api_token_budget.state='reserved'",
            params![entry.id, entry.provider, entry.time, entry.tokens],
        )?;
        transaction.commit()?;
        Ok(())
    }

    pub(super) fn finish_token_budget(&self, entry: &BudgetEntry) -> Result<()> {
        validate_budget_entry(entry)?;
        let db = self.lock()?;
        if entry.tokens == 0 {
            db.execute("DELETE FROM api_token_budget WHERE id=?", [&entry.id])?;
        } else {
            db.execute(
                "INSERT INTO api_token_budget(id,provider,time,tokens,state) VALUES(?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,time=excluded.time,
                     tokens=excluded.tokens,state=excluded.state",
                params![
                    entry.id,
                    entry.provider,
                    entry.time,
                    entry.tokens,
                    if entry.estimated { "estimated" } else { "used" }
                ],
            )?;
        }
        Ok(())
    }

    pub(super) fn restore_token_budgets(&self, at: f64) -> Result<Vec<BudgetEntry>> {
        if !at.is_finite() || at < 0.0 {
            return Err(anyhow!("Invalid token budget timestamp"));
        }
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        // A crashed request may have consumed its whole reservation. Account
        // for that uncertainty once; later restarts must not extend its expiry.
        transaction.execute(
            "UPDATE api_token_budget SET state='estimated',time=? WHERE state='reserved'",
            [at],
        )?;
        transaction.execute(
            "DELETE FROM api_token_budget WHERE state<>'reserved' AND time<=?",
            [at - WINDOW_SECONDS],
        )?;
        let entries = transaction
            .prepare("SELECT id,provider,time,tokens,state FROM api_token_budget ORDER BY time,id")?
            .query_map([], |row| {
                Ok(BudgetEntry {
                    id: row.get("id")?,
                    provider: row.get("provider")?,
                    time: row.get("time")?,
                    tokens: row.get("tokens")?,
                    estimated: row.get::<_, String>("state")? == "estimated",
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        transaction.commit()?;
        Ok(entries)
    }

    pub fn recover_interrupted(&self) -> Result<()> {
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let interrupted = transaction
            .prepare("SELECT * FROM api_attempts WHERE finished IS NULL")?
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>("provider")?,
                    row.get::<_, String>("request_id")?,
                    RouteContext::from_row(row)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        transaction.execute(
            "UPDATE api_attempts SET outcome='interrupted',reason='proxy_restarted',finished=? WHERE finished IS NULL",
            [timestamp()],
        )?;
        for (provider, request_id, context) in interrupted {
            insert_event(
                &transaction,
                "interrupted",
                &provider,
                "",
                "proxy_restarted",
                &request_id,
                None,
                &context,
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn begin(
        &self,
        provider: &str,
        request_id: &str,
        kind: &str,
        route_id: &str,
    ) -> Result<String> {
        self.begin_with_context(
            provider,
            request_id,
            kind,
            &RouteContext {
                route_id: route_id.into(),
                ..RouteContext::default()
            },
        )
    }

    pub fn begin_with_context(
        &self,
        provider: &str,
        request_id: &str,
        kind: &str,
        context: &RouteContext,
    ) -> Result<String> {
        let attempt = Uuid::new_v4().simple().to_string();
        let (provider, request_id, kind) = (
            identifier(provider, 40),
            identifier(request_id, 100),
            identifier(kind, 50),
        );
        let context = context.sanitized();
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        transaction.execute(
            "INSERT INTO api_attempts(id,request_id,provider,kind,started,outcome,route_id,project_id,task_id,run_id,thread_id,role) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)",
            params![attempt, request_id, provider, kind, timestamp(), context.route_id,
                context.project_id, context.task_id, context.run_id, context.thread_id, context.role],
        )?;
        insert_event(
            &transaction,
            "attempt",
            provider,
            "",
            "",
            request_id,
            None,
            &context,
        )?;
        transaction.commit()?;
        Ok(attempt)
    }

    pub fn finish(
        &self,
        attempt: &str,
        status: Option<u16>,
        outcome: &str,
        reason: &str,
        usage: Option<&Usage>,
    ) -> Result<()> {
        if attempt.is_empty() {
            return Ok(());
        }
        // A caller constructing Usage directly cannot overflow SQLite integers.
        let usage = usage.filter(|u| {
            u.input_tokens <= MAX_TOKEN_COUNT
                && u.output_tokens <= MAX_TOKEN_COUNT
                && u.total_tokens <= 2 * MAX_TOKEN_COUNT
        });
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let unfinished = transaction
            .query_row(
                "SELECT * FROM api_attempts WHERE id=? AND finished IS NULL",
                [attempt],
                |row| {
                    Ok((
                        row.get::<_, String>("provider")?,
                        row.get::<_, String>("request_id")?,
                        RouteContext::from_row(row)?,
                    ))
                },
            )
            .optional()?;
        if let Some((provider, request_id, context)) = unfinished {
            let outcome = identifier(outcome, 50);
            let reason = identifier(reason, 100);
            transaction.execute(
                "UPDATE api_attempts SET finished=?,status=?,outcome=?,reason=?,input_tokens=?,output_tokens=?,cached_tokens=?,reasoning_tokens=?,total_tokens=? WHERE id=?",
                params![timestamp(), status, outcome, reason,
                    usage.map(|u| u.input_tokens), usage.map(|u| u.output_tokens),
                    usage.and_then(|u| u.cached_tokens.map(|v| v.min(u.input_tokens))),
                    usage.and_then(|u| u.reasoning_tokens.map(|v| v.min(u.output_tokens))),
                    usage.map(|u| u.total_tokens), attempt],
            )?;
            insert_event(
                &transaction,
                outcome,
                &provider,
                "",
                reason,
                &request_id,
                usage.map(|u| u.total_tokens),
                &context,
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn event(
        &self,
        kind: &str,
        provider: &str,
        peer: &str,
        reason: &str,
        request_id: &str,
        tokens: Option<u64>,
    ) -> Result<()> {
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        // Compatibility for callers that only know a request ID. Do not attach
        // an event to an arbitrary run when the ID is ambiguous.
        let contexts = transaction
            .prepare(
                "SELECT DISTINCT route_id,project_id,task_id,run_id,thread_id,role
                 FROM api_attempts WHERE request_id=? LIMIT 2",
            )?
            .query_map([identifier(request_id, 100)], RouteContext::from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let context = if contexts.len() == 1 {
            contexts.into_iter().next().unwrap_or_default()
        } else {
            RouteContext::default()
        };
        insert_event(
            &transaction,
            kind,
            provider,
            peer,
            reason,
            request_id,
            tokens,
            &context,
        )?;
        transaction.commit()?;
        Ok(())
    }

    /// Supply routing metadata for events emitted before an attempt exists.
    #[allow(clippy::too_many_arguments)]
    pub fn event_with_context(
        &self,
        kind: &str,
        provider: &str,
        peer: &str,
        reason: &str,
        request_id: &str,
        tokens: Option<u64>,
        context: &RouteContext,
    ) -> Result<()> {
        let mut db = self.lock()?;
        let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        insert_event(
            &transaction,
            kind,
            provider,
            peer,
            reason,
            request_id,
            tokens,
            &context.sanitized(),
        )?;
        transaction.commit()?;
        Ok(())
    }

    pub fn snapshot(&self) -> Result<Value> {
        self.snapshot_at(timestamp())
    }

    fn snapshot_at(&self, now: f64) -> Result<Value> {
        const SUMS: &str = "COUNT(*) AS attempts,
            SUM(CASE WHEN outcome='completed' AND kind='response' THEN 1 ELSE 0 END) AS generations,
            SUM(CASE WHEN finished IS NOT NULL AND total_tokens IS NULL THEN 1 ELSE 0 END) AS unreported,
            SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
            SUM(cached_tokens) AS cached_tokens,SUM(reasoning_tokens) AS reasoning_tokens,
            SUM(total_tokens) AS total_tokens";
        let db = self.lock()?;
        let mut metrics =
            db.query_row(&format!("SELECT {SUMS} FROM api_attempts"), [], aggregate)?;
        let mut providers = db.prepare(&format!(
            "SELECT provider AS id,{SUMS} FROM api_attempts GROUP BY provider"
        ))?;
        let providers = providers
            .query_map([], |row| {
                let mut result = aggregate(row)?;
                result.insert("id".into(), json!(row.get::<_, String>("id")?));
                Ok(Value::Object(result))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut routes = db.prepare(&format!(
            "SELECT route_id AS id,{SUMS} FROM api_attempts WHERE route_id<>'' GROUP BY route_id"
        ))?;
        let routes = routes
            .query_map([], |row| {
                let mut result = aggregate(row)?;
                result.insert("id".into(), json!(row.get::<_, String>("id")?));
                Ok(Value::Object(result))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut runs = db.prepare(&format!(
            "SELECT run_id AS id,run_id,project_id,task_id,{SUMS} FROM api_attempts
             WHERE run_id<>'' GROUP BY run_id,project_id,task_id ORDER BY MIN(started),run_id"
        ))?;
        let runs = runs
            .query_map([], |row| {
                let mut result = aggregate(row)?;
                for name in ["id", "run_id", "project_id", "task_id"] {
                    result.insert(name.into(), json!(row.get::<_, String>(name)?));
                }
                Ok(Value::Object(result))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let since: String = db.query_row(
            "SELECT value FROM telemetry_meta WHERE key='created'",
            [],
            |row| row.get(0),
        )?;
        metrics.insert("since".into(), json!(since.parse::<f64>()?));
        metrics.insert("providers".into(), json!(providers));
        metrics.insert("routes".into(), json!(routes));
        metrics.insert("runs".into(), json!(runs));
        let mut buckets = db.prepare("SELECT CAST(finished/120 AS INTEGER) AS bucket,SUM(total_tokens) AS tokens FROM api_attempts WHERE finished>=? GROUP BY bucket")?;
        let buckets = buckets
            .query_map([now - 3600.0], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, Option<u64>>(1)?.unwrap_or(0),
                ))
            })?
            .collect::<rusqlite::Result<std::collections::BTreeMap<_, _>>>()?;
        let last = (now / 120.0).floor() as i64;
        let timeline: Vec<_> = (last - 29..=last).map(|bucket| json!({"time": bucket * 120, "tokens": buckets.get(&bucket).copied().unwrap_or(0)})).collect();
        let mut events = db.prepare("SELECT * FROM api_events ORDER BY id DESC LIMIT ?")?;
        let events = events.query_map([SNAPSHOT_EVENTS], |row| Ok(json!({
            "id": row.get::<_, i64>("id")?, "time": row.get::<_, f64>("time")?,
            "kind": row.get::<_, String>("kind")?, "provider": row.get::<_, String>("provider")?,
            "peer": row.get::<_, String>("peer")?, "reason": row.get::<_, String>("reason")?,
            "request_id": row.get::<_, String>("request_id")?, "tokens": row.get::<_, Option<u64>>("tokens")?,
            "route_id": row.get::<_, String>("route_id")?, "project_id": row.get::<_, String>("project_id")?,
            "task_id": row.get::<_, String>("task_id")?, "run_id": row.get::<_, String>("run_id")?,
            "thread_id": row.get::<_, String>("thread_id")?, "role": row.get::<_, String>("role")?,
        })))?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(json!({"metrics": metrics, "timeline": timeline, "events": events}))
    }
}

fn aggregate(row: &Row<'_>) -> rusqlite::Result<Map<String, Value>> {
    let mut value = Map::new();
    for name in ["attempts", "generations", "unreported"] {
        value.insert(
            name.into(),
            json!(row.get::<_, Option<u64>>(name)?.unwrap_or(0)),
        );
    }
    for name in [
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "total_tokens",
    ] {
        value.insert(name.into(), json!(row.get::<_, Option<u64>>(name)?));
    }
    Ok(value)
}

#[allow(clippy::too_many_arguments)]
fn insert_event(
    transaction: &Transaction<'_>,
    kind: &str,
    provider: &str,
    peer: &str,
    reason: &str,
    request_id: &str,
    tokens: Option<u64>,
    context: &RouteContext,
) -> Result<()> {
    transaction.execute(
        "INSERT INTO api_events(time,kind,provider,peer,reason,request_id,tokens,route_id,project_id,task_id,run_id,thread_id,role) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![timestamp(), identifier(kind, 50), identifier(provider, 40), identifier(peer, 40), identifier(reason, 100), identifier(request_id, 100), tokens.filter(|v| *v <= 2 * MAX_TOKEN_COUNT),
            context.route_id, context.project_id, context.task_id, context.run_id, context.thread_id, context.role],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget_entry(id: &str, time: f64, tokens: u64, estimated: bool) -> BudgetEntry {
        BudgetEntry {
            id: id.into(),
            provider: "provider1".into(),
            time,
            tokens,
            estimated,
        }
    }

    #[test]
    fn normalizes_subsets_and_rejects_invalid_counts() {
        let usage = normalize_usage(&json!({"input_tokens": 100, "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 900}, "output_tokens_details": {"reasoning_tokens": 99}})).unwrap();
        assert_eq!(usage.total_tokens, 140);
        assert_eq!(usage.cached_tokens, Some(100));
        assert_eq!(usage.reasoning_tokens, Some(40));
        for invalid in [
            json!([]),
            json!(null),
            json!({"input_tokens": true, "output_tokens": 0}),
            json!({"input_tokens": -1, "output_tokens": 0}),
            json!({"input_tokens": 1.5, "output_tokens": 0}),
            json!({"input_tokens": 1000000000000001u64, "output_tokens": 0}),
        ] {
            assert!(normalize_usage(&invalid).is_none());
        }
        let usage = normalize_usage(&json!({"input_tokens": 1, "output_tokens": 2,
            "total_tokens": false, "input_tokens_details": [], "output_tokens_details": "invalid"}))
        .unwrap();
        assert_eq!(usage.total_tokens, 3);
        assert_eq!(usage.cached_tokens, None);
        for (reported, expected) in [(0, 140), (139, 140), (140, 140), (150, 150)] {
            let usage = normalize_usage(&json!({
                "input_tokens": 100, "output_tokens": 40, "total_tokens": reported
            }))
            .unwrap();
            assert_eq!(usage.total_tokens, expected);
        }
    }

    #[test]
    fn token_budget_finish_reconciles_deduplicates_and_persists() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.sqlite3");
        let store = TelemetryStore::open(&path).unwrap();
        let reserved = budget_entry("request1", 100.0, 8192, false);
        store.reserve_token_budget(&reserved).unwrap();
        store.reserve_token_budget(&reserved).unwrap();
        let completed = budget_entry("request1", 101.0, 25, false);
        store.finish_token_budget(&completed).unwrap();
        store.finish_token_budget(&completed).unwrap();
        // A delayed duplicate reservation cannot replace reconciled usage.
        store.reserve_token_budget(&reserved).unwrap();
        store
            .finish_token_budget(&budget_entry("unknown-usage", 101.0, 100, true))
            .unwrap();
        store
            .reserve_token_budget(&budget_entry("cancelled", 101.0, 8192, false))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("cancelled", 101.0, 0, false))
            .unwrap();
        drop(store);

        let store = TelemetryStore::open(&path).unwrap();
        let entries = store.restore_token_budgets(102.0).unwrap();
        assert_eq!(entries.len(), 2);
        let completed = entries.iter().find(|entry| entry.id == "request1").unwrap();
        assert_eq!(completed.tokens, 25);
        assert_eq!(completed.time, 101.0);
        assert!(!completed.estimated);
        let unknown = entries
            .iter()
            .find(|entry| entry.id == "unknown-usage")
            .unwrap();
        assert_eq!(unknown.tokens, 100);
        assert!(unknown.estimated);
        // Admission estimates stay separate from the historical usage totals.
        assert!(store.snapshot().unwrap()["metrics"]["total_tokens"].is_null());
    }

    #[test]
    fn token_budget_restart_recovers_active_requests_once_and_expires_each_sample() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.sqlite3");
        let store = TelemetryStore::open(&path).unwrap();
        store
            .reserve_token_budget(&budget_entry("interrupted-stream", 5.0, 8192, false))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("old-used", 40.0, 10, false))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("old-estimate", 39.0, 20, true))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("recent-used", 80.0, 30, false))
            .unwrap();
        drop(store);

        let store = TelemetryStore::open(&path).unwrap();
        let restored = store.restore_token_budgets(100.0).unwrap();
        assert_eq!(restored.len(), 2);
        let interrupted = restored
            .iter()
            .find(|entry| entry.id == "interrupted-stream")
            .unwrap();
        assert_eq!(interrupted.time, 100.0);
        assert_eq!(interrupted.tokens, 8192);
        assert!(interrupted.estimated);
        drop(store);

        let store = TelemetryStore::open(&path).unwrap();
        let restored = store.restore_token_budgets(110.0).unwrap();
        assert_eq!(restored.len(), 2);
        assert_eq!(
            restored
                .iter()
                .find(|entry| entry.id == "interrupted-stream")
                .unwrap()
                .time,
            100.0
        );
        let restored = store.restore_token_budgets(140.0).unwrap();
        assert_eq!(restored.len(), 1);
        assert_eq!(restored[0].id, "interrupted-stream");
        assert!(store.restore_token_budgets(160.0).unwrap().is_empty());
    }

    #[test]
    fn token_budget_reservation_prunes_finished_samples_but_keeps_active_streams() {
        let dir = tempfile::tempdir().unwrap();
        let store = TelemetryStore::open(dir.path().join("telemetry.sqlite3")).unwrap();
        store
            .reserve_token_budget(&budget_entry("active", 1.0, 200, false))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("expired", 40.0, 20, false))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("expired-estimate", 40.0, 20, true))
            .unwrap();
        store
            .finish_token_budget(&budget_entry("recent", 50.0, 20, false))
            .unwrap();
        store
            .reserve_token_budget(&budget_entry("new", 100.0, 200, false))
            .unwrap();
        let rows = store
            .lock()
            .unwrap()
            .prepare("SELECT id,state FROM api_token_budget ORDER BY id")
            .unwrap()
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(
            rows,
            vec![
                ("active".into(), "reserved".into()),
                ("new".into(), "reserved".into()),
                ("recent".into(), "used".into()),
            ]
        );
    }

    #[test]
    fn token_budget_rejects_free_form_metadata_without_storing_or_echoing_it() {
        let dir = tempfile::tempdir().unwrap();
        let store = TelemetryStore::open(dir.path().join("telemetry.sqlite3")).unwrap();
        let private_text = "Bearer fixture-private-value\nrequest body";
        let baseline = budget_entry("request1", 100.0, 20, false);
        for entry in [
            BudgetEntry {
                id: private_text.into(),
                ..baseline.clone()
            },
            BudgetEntry {
                provider: private_text.into(),
                ..baseline.clone()
            },
            BudgetEntry {
                tokens: u64::MAX,
                ..baseline.clone()
            },
            BudgetEntry {
                time: f64::NAN,
                ..baseline.clone()
            },
        ] {
            for result in [
                store.reserve_token_budget(&entry),
                store.finish_token_budget(&entry),
            ] {
                let error = result.unwrap_err().to_string();
                assert!(!error.contains(private_text));
                assert!(!error.contains("fixture-private-value"));
            }
        }
        assert!(store.restore_token_budgets(f64::NAN).is_err());
        assert!(store.restore_token_budgets(100.0).unwrap().is_empty());
        let columns = store
            .lock()
            .unwrap()
            .prepare("PRAGMA table_info(api_token_budget)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(columns, ["id", "provider", "time", "tokens", "state"]);
    }

    #[test]
    fn token_budget_migrates_recent_reported_usage_only_once() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("legacy.sqlite3");
        let db = Connection::open(&path).unwrap();
        db.execute_batch(
            "CREATE TABLE api_attempts(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,provider TEXT NOT NULL,
                kind TEXT NOT NULL,started REAL NOT NULL,finished REAL,status INTEGER,outcome TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',input_tokens INTEGER,output_tokens INTEGER,
                cached_tokens INTEGER,reasoning_tokens INTEGER,total_tokens INTEGER);",
        ).unwrap();
        let at = timestamp();
        for (id, age, total) in [
            ("recent", 1.0, Some(1)),
            ("old", 61.0, Some(25)),
            ("unknown", 1.0, None),
        ] {
            db.execute(
                "INSERT INTO api_attempts(id,request_id,provider,kind,started,finished,outcome,input_tokens,output_tokens,total_tokens)
                 VALUES(?,?,'provider1','response',?,?,'completed',20,5,?)",
                params![id, id, at - age, at - age, total],
            ).unwrap();
        }
        drop(db);
        let store = TelemetryStore::open(&path).unwrap();
        let restored = store.restore_token_budgets(at).unwrap();
        assert_eq!(restored.len(), 1);
        assert!(restored[0].id.starts_with("legacy-"));
        assert_eq!(restored[0].provider, "provider1");
        assert_eq!(restored[0].tokens, 25);
        assert!(!restored[0].estimated);
        let new_attempt = store
            .begin("provider1", "new-request", "response", "")
            .unwrap();
        let usage = normalize_usage(&json!({"input_tokens": 8, "output_tokens": 3})).unwrap();
        store
            .finish(&new_attempt, Some(200), "completed", "", Some(&usage))
            .unwrap();
        store
            .finish_token_budget(&budget_entry(&new_attempt, at, 11, false))
            .unwrap();
        drop(store);

        let store = TelemetryStore::open(&path).unwrap();
        let restored = store.restore_token_budgets(at + 1.0).unwrap();
        assert_eq!(restored.len(), 2);
        assert_eq!(restored.iter().map(|entry| entry.tokens).sum::<u64>(), 36);
        let marker: String = store
            .lock()
            .unwrap()
            .query_row(
                "SELECT value FROM telemetry_meta WHERE key='token_budget_migrated_v1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(marker, "1");
    }

    #[test]
    fn persists_deduplicates_and_recovers_attempts_without_content() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.sqlite3");
        let store = TelemetryStore::open(&path).unwrap();
        let attempt = store
            .begin("provider1", "req1", "response", "task1")
            .unwrap();
        let usage = normalize_usage(&json!({"input_tokens": 10, "output_tokens": 5})).unwrap();
        store
            .finish(&attempt, Some(200), "completed", "", Some(&usage))
            .unwrap();
        store
            .finish(&attempt, Some(429), "failed", "http_429", None)
            .unwrap();
        store
            .begin("provider2", "req2", "compact", "task1")
            .unwrap();
        drop(store);
        let store = TelemetryStore::open(&path).unwrap();
        store.recover_interrupted().unwrap();
        let snapshot = store.snapshot().unwrap();
        assert_eq!(snapshot["metrics"]["attempts"], 2);
        assert_eq!(snapshot["metrics"]["generations"], 1);
        assert_eq!(snapshot["metrics"]["unreported"], 1);
        assert_eq!(snapshot["metrics"]["total_tokens"], 15);
        assert_eq!(snapshot["metrics"]["routes"][0]["id"], "task1");
        assert_eq!(snapshot["events"].as_array().unwrap().len(), 4);
        assert_eq!(snapshot["events"][0]["kind"], "interrupted");
        assert_eq!(snapshot["events"][0]["route_id"], "task1");
        assert_eq!(snapshot["timeline"].as_array().unwrap().len(), 30);
        assert_eq!(
            snapshot["timeline"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v["tokens"].as_u64().unwrap())
                .sum::<u64>(),
            15
        );
        let db = store.lock().unwrap();
        let recovered: String = db
            .query_row(
                "SELECT reason FROM api_attempts WHERE request_id='req2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(recovered, "proxy_restarted");
        let columns = db
            .prepare("PRAGMA table_info(api_attempts)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert!(!columns
            .iter()
            .any(|column| ["prompt", "input", "body", "response", "message"]
                .contains(&column.as_str())));
    }

    #[test]
    fn retains_all_events_but_bounds_snapshot_and_sanitizes_content() {
        let dir = tempfile::tempdir().unwrap();
        let store = TelemetryStore::open(dir.path().join("telemetry.sqlite3")).unwrap();
        let attempt = store.begin("provider1", "req1", "response", "").unwrap();
        store
            .finish(&attempt, Some(200), "completed", "", None)
            .unwrap();
        for _ in 0..2010 {
            store
                .event(
                    "rotation",
                    "provider1",
                    "provider2",
                    "http_429",
                    "req1",
                    None,
                )
                .unwrap();
        }
        store
            .event(
                "failed",
                "provider1",
                "",
                "upstream says private prompt",
                "req1",
                None,
            )
            .unwrap();
        let count: i64 = store
            .lock()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM api_events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 2013);
        let snapshot = store.snapshot().unwrap();
        assert_eq!(snapshot["events"].as_array().unwrap().len(), 50);
        assert!(snapshot["metrics"]["total_tokens"].is_null());
        assert_eq!(snapshot["metrics"]["unreported"], 1);
        assert_eq!(snapshot["events"][0]["reason"], "invalid_identifier");
        assert!(!snapshot.to_string().contains("private prompt"));
    }

    #[test]
    fn migrates_legacy_database_without_losing_attempts_events_or_creation_time() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("legacy.sqlite3");
        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE telemetry_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO telemetry_meta VALUES('created','1234.5');
            CREATE TABLE api_attempts(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,provider TEXT NOT NULL,kind TEXT NOT NULL,
                started REAL NOT NULL,finished REAL,status INTEGER,outcome TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER,output_tokens INTEGER,cached_tokens INTEGER,reasoning_tokens INTEGER,total_tokens INTEGER);
            INSERT INTO api_attempts(id,request_id,provider,kind,started,finished,status,outcome,input_tokens,output_tokens,total_tokens)
                VALUES('legacy','old-request','provider1','response',1200,1201,200,'completed',12,3,15);
            CREATE TABLE api_events(id INTEGER PRIMARY KEY,time REAL NOT NULL,kind TEXT NOT NULL,provider TEXT NOT NULL,
                peer TEXT NOT NULL,reason TEXT NOT NULL,request_id TEXT NOT NULL,tokens INTEGER);
            INSERT INTO api_events VALUES(77,1201,'completed','provider1','','','old-request',15);").unwrap();
        drop(db);
        let store = TelemetryStore::open(&path).unwrap();
        let before = store.snapshot().unwrap();
        assert_eq!(before["metrics"]["since"], 1234.5);
        assert_eq!(before["metrics"]["attempts"], 1);
        assert_eq!(before["metrics"]["total_tokens"], 15);
        assert_eq!(before["events"][0]["id"], 77);
        assert_eq!(before["events"][0]["request_id"], "old-request");
        assert_eq!(before["events"][0]["tokens"], 15);
        assert_eq!(before["events"][0]["run_id"], "");
        assert!(before["metrics"]["runs"].as_array().unwrap().is_empty());
        drop(store);
        // A second migration must be a no-op and leave historical NULL values.
        let store = TelemetryStore::open(&path).unwrap();
        let cached: Option<u64> = store
            .lock()
            .unwrap()
            .query_row(
                "SELECT cached_tokens FROM api_attempts WHERE id='legacy'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cached, None);
        let attempt = store
            .begin("provider1", "req1", "response", "route1")
            .unwrap();
        store
            .finish(&attempt, Some(200), "completed", "", None)
            .unwrap();
        assert_eq!(
            store.snapshot().unwrap()["metrics"]["routes"][0]["id"],
            "route1"
        );
    }

    #[test]
    fn records_run_and_thread_context_without_reusing_previous_run_usage() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.sqlite3");
        let store = TelemetryStore::open(&path).unwrap();
        let context = RouteContext {
            route_id: "route1".into(),
            project_id: "project1".into(),
            task_id: "chat1".into(),
            run_id: "run1".into(),
            thread_id: "thread1".into(),
            role: "main".into(),
        };
        let first = store
            .begin_with_context("provider1", "req1", "response", &context)
            .unwrap();
        let usage = normalize_usage(&json!({"input_tokens": 10, "output_tokens": 5})).unwrap();
        store
            .finish(&first, Some(200), "completed", "", Some(&usage))
            .unwrap();
        let auxiliary = RouteContext {
            route_id: "route2".into(),
            thread_id: "thread2".into(),
            role: "auxiliary".into(),
            ..context.clone()
        };
        let second = store
            .begin_with_context("provider2", "req2", "response", &auxiliary)
            .unwrap();
        store
            .event("cooldown", "provider2", "", "http_429", "req2", None)
            .unwrap();
        store
            .finish(&second, Some(200), "completed", "", Some(&usage))
            .unwrap();
        let next_run = RouteContext {
            run_id: "run2".into(),
            ..context.clone()
        };
        store
            .begin_with_context("provider1", "req3", "response", &next_run)
            .unwrap();
        drop(store);
        let store = TelemetryStore::open(&path).unwrap();
        store.recover_interrupted().unwrap();
        store.recover_interrupted().unwrap();
        let snapshot = store.snapshot().unwrap();
        let runs = snapshot["metrics"]["runs"].as_array().unwrap();
        let first_run = runs.iter().find(|run| run["id"] == "run1").unwrap();
        assert_eq!(first_run["attempts"], 2);
        assert_eq!(first_run["total_tokens"], 30);
        assert_eq!(first_run["project_id"], "project1");
        assert_eq!(first_run["task_id"], "chat1");
        let new_run = runs.iter().find(|run| run["id"] == "run2").unwrap();
        assert_eq!(new_run["attempts"], 1);
        assert_eq!(new_run["unreported"], 1);
        assert!(new_run["total_tokens"].is_null());
        let events = snapshot["events"].as_array().unwrap();
        assert_eq!(events.len(), 7);
        assert_eq!(events[0]["run_id"], "run2");
        assert_eq!(events[0]["kind"], "interrupted");
        let cooldown = events
            .iter()
            .find(|event| event["kind"] == "cooldown")
            .unwrap();
        assert_eq!(cooldown["run_id"], "run1");
        assert_eq!(cooldown["thread_id"], "thread2");
        assert_eq!(cooldown["role"], "auxiliary");
        let db = store.lock().unwrap();
        let saved_context = db
            .query_row(
                "SELECT * FROM api_attempts WHERE id=?",
                [second],
                RouteContext::from_row,
            )
            .unwrap();
        assert_eq!(saved_context, auxiliary);
    }

    #[test]
    fn explicit_events_keep_context_and_ambiguous_request_ids_do_not_guess_a_run() {
        let dir = tempfile::tempdir().unwrap();
        let store = TelemetryStore::open(dir.path().join("telemetry.sqlite3")).unwrap();
        let first = RouteContext {
            run_id: "run1".into(),
            ..RouteContext::default()
        };
        let second = RouteContext {
            run_id: "run2".into(),
            ..RouteContext::default()
        };
        store
            .begin_with_context("provider1", "shared-id", "response", &first)
            .unwrap();
        store
            .begin_with_context("provider1", "shared-id", "response", &second)
            .unwrap();
        store
            .event("cooldown", "provider1", "", "http_429", "shared-id", None)
            .unwrap();
        store
            .event_with_context("queued", "provider1", "", "", "new-id", None, &second)
            .unwrap();
        let snapshot = store.snapshot().unwrap();
        assert_eq!(snapshot["events"][0]["run_id"], "run2");
        assert_eq!(snapshot["events"][1]["run_id"], "");
    }

    #[test]
    fn waits_for_an_external_sqlite_writer_before_finishing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.sqlite3");
        let store = TelemetryStore::open(&path).unwrap();
        let attempt = store
            .begin("provider1", "req1", "response", "route1")
            .unwrap();
        let mut external = Connection::open(path).unwrap();
        let transaction = external
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .unwrap();
        let (ready, waiting) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            ready.send(()).unwrap();
            store
                .finish(&attempt, Some(200), "completed", "", None)
                .unwrap();
            store.snapshot().unwrap()
        });
        waiting.recv().unwrap();
        std::thread::sleep(Duration::from_millis(100));
        transaction.commit().unwrap();
        let snapshot = worker.join().unwrap();
        assert_eq!(snapshot["metrics"]["generations"], 1);
        assert_eq!(snapshot["events"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn concurrent_opens_serialize_migration_and_preserve_each_writer() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("telemetry.sqlite3");
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
        let workers: Vec<_> = (0..3)
            .map(|index| {
                let path = path.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    let store = TelemetryStore::open(path).unwrap();
                    store
                        .begin("provider1", &format!("req{index}"), "response", "")
                        .unwrap();
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        let snapshot = TelemetryStore::open(&path).unwrap().snapshot().unwrap();
        assert_eq!(snapshot["metrics"]["attempts"], 3);
        assert_eq!(snapshot["events"].as_array().unwrap().len(), 3);
    }
}
