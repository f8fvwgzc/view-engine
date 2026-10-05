//! Trading desk: client for the Python quant sidecar (`quant/`, free market data + indicators + options
//! positioning + ML) and the prediction ledger that scores every trade plan after its horizon.

use std::time::Duration;

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::PgPool;
use uuid::Uuid;

pub struct Quant {
    base: String,
    http: reqwest::Client,
}

impl Quant {
    pub fn from_env(http: reqwest::Client) -> Self {
        let base = std::env::var("QUANT_URL").ok().filter(|url| !url.trim().is_empty()).unwrap_or_else(|| "http://127.0.0.1:8090".into());
        Self { base: base.trim_end_matches('/').to_string(), http }
    }

    async fn get(&self, path: &str, query: &[(&str, String)], timeout_secs: u64) -> Result<Value, String> {
        let response = self
            .http
            .get(format!("{}{path}", self.base))
            .query(query)
            .timeout(Duration::from_secs(timeout_secs))
            .send()
            .await
            .map_err(|error| format!("quant sidecar unreachable at {} ({error}); start it with `make quant`", self.base))?;
        let status = response.status();
        let value: Value = response.json().await.map_err(|error| format!("quant sidecar returned invalid JSON: {error}"))?;
        if status.is_success() { Ok(value) } else { Err(format!("quant {path} {status}: {}", value["detail"].as_str().unwrap_or(&value.to_string()))) }
    }

    pub async fn health(&self) -> Value {
        match self.get("/health", &[], 3).await {
            Ok(value) => json!({"available": true, "url": self.base, "health": value}),
            Err(error) => json!({"available": false, "url": self.base, "detail": error}),
        }
    }

    pub async fn symbols(&self) -> Result<Value, String> {
        self.get("/symbols", &[], 10).await
    }

    /// Finds the first known instrument mentioned in free text ("USD/JPY", "gold", "XAUUSD"...).
    pub async fn detect(&self, text: &str) -> Option<String> {
        let symbols = self.symbols().await.ok()?;
        let list = symbols.as_array().cloned().or_else(|| symbols["symbols"].as_array().cloned())?;
        let haystack = format!(" {} ", normalize(text));
        let mut best: Option<(usize, String)> = None;
        for symbol in list {
            let Some(id) = symbol["id"].as_str() else { continue };
            let mut names = vec![id.to_string()];
            if let Some(name) = symbol["name"].as_str() { names.push(name.to_string()) }
            for alias in symbol["aliases"].as_array().into_iter().flatten().filter_map(Value::as_str) {
                names.push(alias.to_string());
            }
            names.extend(common_names(id).iter().map(|name| name.to_string()));
            for name in names {
                let needle = normalize(&name);
                if needle.len() < 3 { continue }
                if let Some(position) = haystack.find(&format!(" {needle} ")).or_else(|| (needle.len() >= 6).then(|| haystack.find(&needle)).flatten()) {
                    if best.as_ref().map(|(at, _)| position < *at).unwrap_or(true) {
                        best = Some((position, id.to_string()));
                    }
                }
            }
        }
        best.map(|(_, id)| id)
    }

    /// `as_of` (replay mode) restricts every computation to candles closed at or before that moment.
    pub async fn snapshot(&self, symbol: &str, timeframes: &[String], horizon: &str, as_of: Option<DateTime<Utc>>) -> Result<Value, String> {
        let mut query = vec![("symbol", symbol.to_string()), ("timeframes", timeframes.join(",")), ("horizon", horizon.to_string())];
        if let Some(as_of) = as_of { query.push(("as_of", as_of.to_rfc3339())) }
        self.get("/snapshot", &query, 180).await
    }

    pub async fn price(&self, symbol: &str, as_of: Option<DateTime<Utc>>) -> Option<f64> {
        let mut query = vec![("symbol", symbol.to_string())];
        if let Some(as_of) = as_of { query.push(("as_of", as_of.to_rfc3339())) }
        self.get("/price", &query, 30).await.ok()?["price"].as_f64()
    }

    /// Candles (t, high, low, close) between two moments, 15-minute resolution when the sidecar has it.
    pub async fn path(&self, symbol: &str, start: DateTime<Utc>, end: DateTime<Utc>) -> Vec<(DateTime<Utc>, f64, f64, f64)> {
        for interval in ["15m", "1h"] {
            let query = [("symbol", symbol.to_string()), ("interval", interval.to_string()), ("start", start.to_rfc3339()), ("end", end.to_rfc3339()), ("lookback", "5000".to_string())];
            if let Ok(value) = self.get("/ohlc", &query, 60).await {
                let candles = parse_candles(&value);
                let inside: Vec<_> = candles.into_iter().filter(|(time, ..)| *time >= start && *time <= end).collect();
                if !inside.is_empty() {
                    return inside;
                }
            }
        }
        vec![]
    }

    /// Hourly candles (t, high, low, close) for scoring a prediction path.
    pub async fn hourly(&self, symbol: &str, lookback: usize) -> Vec<(DateTime<Utc>, f64, f64, f64)> {
        let Ok(value) = self.get("/ohlc", &[("symbol", symbol.to_string()), ("interval", "1h".into()), ("lookback", lookback.to_string())], 30).await else { return vec![] };
        parse_candles(&value)
    }
}

fn parse_candles(value: &Value) -> Vec<(DateTime<Utc>, f64, f64, f64)> {
    value["candles"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|candle| {
            let time = candle["t"].as_str().and_then(|text| DateTime::parse_from_rfc3339(text).ok()).map(|time| time.with_timezone(&Utc))
                .or_else(|| candle["t"].as_i64().and_then(|seconds| DateTime::from_timestamp(if seconds > 10_000_000_000 { seconds / 1000 } else { seconds }, 0)))?;
            Some((time, candle["h"].as_f64()?, candle["l"].as_f64()?, candle["c"].as_f64()?))
        })
        .collect()
}

/// How charts and people write the instruments the desk trades most ("Gold Spot / U.S. Dollar", "EUR/USD", "cable").
fn common_names(id: &str) -> &'static [&'static str] {
    match id {
        "XAUUSD" => &["GOLD", "XAU"],
        "XAGUSD" => &["SILVER", "XAG"],
        "EURUSD" => &["EUR USD", "EURO U S DOLLAR", "EURO US DOLLAR"],
        "GBPUSD" => &["GBP USD", "BRITISH POUND U S DOLLAR", "BRITISH POUND US DOLLAR", "CABLE"],
        "USDJPY" => &["USD JPY", "U S DOLLAR JAPANESE YEN", "US DOLLAR JAPANESE YEN"],
        "GBPJPY" => &["GBP JPY", "BRITISH POUND JAPANESE YEN"],
        "EURJPY" => &["EUR JPY", "EURO JAPANESE YEN"],
        "AUDUSD" => &["AUD USD", "AUSTRALIAN DOLLAR U S DOLLAR", "AUSTRALIAN DOLLAR US DOLLAR"],
        "USDCAD" => &["USD CAD", "U S DOLLAR CANADIAN DOLLAR", "US DOLLAR CANADIAN DOLLAR"],
        "USDCHF" => &["USD CHF", "U S DOLLAR SWISS FRANC", "US DOLLAR SWISS FRANC"],
        "NZDUSD" => &["NZD USD", "NEW ZEALAND DOLLAR U S DOLLAR", "NEW ZEALAND DOLLAR US DOLLAR"],
        _ => &[],
    }
}

fn normalize(text: &str) -> String {
    text.to_uppercase().chars().map(|character| if character.is_alphanumeric() || character == '&' { character } else { ' ' }).collect::<String>().split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn horizon_hours(horizon: &str) -> i32 {
    match horizon.trim().to_ascii_lowercase().as_str() {
        "1h" => 1,
        "4h" => 4,
        "1d" | "24h" => 24,
        "3d" => 72,
        "1w" | "1wk" => 168,
        "1m" | "1mo" => 720,
        other => other.trim_end_matches('h').parse().unwrap_or(24),
    }
}

/// Stores the head trader's plan as a scored-later prediction.
pub async fn record_prediction(database: &PgPool, quant: &Quant, ids: (Uuid, Uuid, Uuid), symbol: &str, plan: &Value, horizon_hours: i32, model_prob_up: Option<f64>, as_of: Option<DateTime<Utc>>) -> Option<Uuid> {
    let direction = match plan["direction"].as_str().unwrap_or("neutral").to_ascii_lowercase().as_str() {
        "long" | "buy" | "bullish" => "long",
        "short" | "sell" | "bearish" => "short",
        _ => "neutral",
    };
    let reference = quant.price(symbol, as_of).await?;
    // Replay runs are stamped at their as-of moment so they are scored against what actually followed.
    let created_at = as_of.unwrap_or_else(Utc::now);
    let probability = plan["probability"].as_f64().unwrap_or(0.5).clamp(0.0, 1.0);
    let horizon = plan["horizon_hours"].as_i64().map(|hours| hours as i32).filter(|hours| *hours > 0).unwrap_or(horizon_hours).clamp(1, 24 * 90);
    let entry = plan["entry_zone"].as_array().map(|zone| zone.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();
    let targets: Vec<f64> = plan["targets"].as_array().map(|items| items.iter().filter_map(Value::as_f64).collect()).unwrap_or_default();
    let id = Uuid::new_v4();
    let (project_id, task_id, run_id) = ids;
    sqlx::query(
        "INSERT INTO predictions (id, project_id, task_id, run_id, symbol, direction, probability, horizon_hours, reference_price, entry_low, entry_high, stop, targets, plan, model_prob_up, created_at, evaluate_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $16 + make_interval(hours => $8))",
    )
    .bind(id)
    .bind(project_id)
    .bind(task_id)
    .bind(run_id)
    .bind(symbol)
    .bind(direction)
    .bind(probability as f32)
    .bind(horizon)
    .bind(reference)
    .bind(entry.iter().cloned().reduce(f64::min))
    .bind(entry.iter().cloned().reduce(f64::max))
    .bind(plan["stop"].as_f64())
    .bind(&targets)
    .bind(plan)
    .bind(model_prob_up.map(|value| value as f32))
    .bind(created_at)
    .execute(database)
    .await
    .ok()?;
    Some(id)
}

#[derive(Debug, sqlx::FromRow)]
struct DuePrediction {
    id: Uuid,
    project_id: Uuid,
    task_id: Uuid,
    run_id: Uuid,
    symbol: String,
    direction: String,
    probability: f32,
    horizon_hours: i32,
    reference_price: f64,
    entry_low: Option<f64>,
    entry_high: Option<f64>,
    stop: Option<f64>,
    targets: Vec<f64>,
    created_at: DateTime<Utc>,
    evaluate_at: DateTime<Utc>,
}

pub struct Score {
    pub outcome: &'static str,
    pub price: f64,
    pub return_pct: f64,
    pub brier: f64,
}

/// Walks the hourly path after the prediction. `probability` follows the head-trader skill's definition:
/// long/short = P(first target is hit before the stop within the horizon); neutral = P(price ends inside the entry
/// zone). Same-bar stop/target touches count as stop (conservative). If neither level is hit, the outcome records
/// whether the direction was right at the horizon (`correct`/`wrong`), but only `target` counts as a hit for Brier.
pub fn score(direction: &str, probability: f64, reference: f64, entry: (Option<f64>, Option<f64>), stop: Option<f64>, target: Option<f64>, path: &[(f64, f64, f64)]) -> Option<Score> {
    let (_, _, last_close) = *path.last()?;
    let raw_return = (last_close / reference - 1.0) * 100.0;
    let signed = match direction { "long" => raw_return, "short" => -raw_return, _ => -raw_return.abs() };
    let mut outcome = None;
    if direction != "neutral" {
        for (high, low, _) in path {
            let (stop_hit, target_hit) = match direction {
                "long" => (stop.is_some_and(|stop| *low <= stop), target.is_some_and(|target| *high >= target)),
                _ => (stop.is_some_and(|stop| *high >= stop), target.is_some_and(|target| *low <= target)),
            };
            if stop_hit { outcome = Some("stop"); break }
            if target_hit { outcome = Some("target"); break }
        }
    }
    let inside_zone = match entry {
        (Some(low), Some(high)) if high > low => last_close >= low && last_close <= high,
        _ => raw_return.abs() < 0.15,
    };
    let outcome = outcome.unwrap_or(if direction == "neutral" {
        if inside_zone { "flat" } else { "wrong" }
    } else if raw_return.abs() < 0.02 {
        "flat"
    } else if signed > 0.0 {
        "correct"
    } else {
        "wrong"
    });
    let right = if direction == "neutral" { outcome == "flat" } else if target.is_some() { outcome == "target" } else { outcome == "correct" };
    let brier = (probability - if right { 1.0 } else { 0.0 }).powi(2);
    Some(Score { outcome, price: last_close, return_pct: signed, brier })
}

/// Scores every prediction whose horizon has passed; returns how many were scored.
pub async fn score_due(state: &crate::SharedState) -> usize {
    let due: Vec<DuePrediction> = sqlx::query_as("SELECT id, project_id, task_id, run_id, symbol, direction, probability, horizon_hours, reference_price, entry_low, entry_high, stop, targets, created_at, evaluate_at FROM predictions WHERE evaluated_at IS NULL AND evaluate_at <= NOW() LIMIT 20")
        .fetch_all(&state.database)
        .await
        .unwrap_or_default();
    let mut scored = 0;
    for prediction in due {
        let mut candles = state.harness.quant.path(&prediction.symbol, prediction.created_at, prediction.evaluate_at).await;
        if candles.is_empty() {
            candles = state.harness.quant.hourly(&prediction.symbol, (prediction.horizon_hours as usize + 72).min(2000)).await.into_iter().filter(|(time, ..)| *time >= prediction.created_at && *time <= prediction.evaluate_at).collect();
        }
        let path: Vec<(f64, f64, f64)> = candles.into_iter().map(|(_, high, low, close)| (high, low, close)).collect();
        let result = score(&prediction.direction, prediction.probability as f64, prediction.reference_price, (prediction.entry_low, prediction.entry_high), prediction.stop, prediction.targets.first().copied(), &path);
        let Some(result) = result else {
            // No data yet (weekend / feed gap): retry later, give up a week after the horizon.
            if Utc::now() - prediction.evaluate_at > chrono::Duration::days(7) {
                let _ = sqlx::query("UPDATE predictions SET evaluated_at = NOW(), outcome = 'unscored' WHERE id = $1").bind(prediction.id).execute(&state.database).await;
            }
            continue;
        };
        scored += 1;
        let _ = sqlx::query("UPDATE predictions SET evaluated_at = NOW(), outcome = $2, outcome_price = $3, return_pct = $4, brier = $5 WHERE id = $1")
            .bind(prediction.id)
            .bind(result.outcome)
            .bind(result.price)
            .bind(result.return_pct)
            .bind(result.brier)
            .execute(&state.database)
            .await;
        // Credit (or debit) the skills that took part in the run that made this call.
        let hit = matches!((prediction.direction.as_str(), result.outcome), (_, "target") | ("neutral", "flat"));
        let column = if hit { "hits" } else { "misses" };
        let _ = sqlx::query(&format!("UPDATE skill_stats SET {column} = {column} + 1, updated_at = NOW() WHERE skill IN (SELECT DISTINCT unnest(skills) FROM run_agents WHERE run_id = $1)")).bind(prediction.run_id).execute(&state.database).await;
        state.harness.refresh_skill_priors().await;
        let message = format!(
            "Prediction scored: {} {} from {:.5} ({}) with {:.0}% → {} ({:+.2}% in {}h, Brier {:.3})",
            prediction.symbol, prediction.direction, prediction.reference_price, prediction.created_at.format("%Y-%m-%d %H:%M UTC"), prediction.probability * 100.0, result.outcome, result.return_pct, prediction.horizon_hours, result.brier
        );
        crate::events::EventDraft::new(prediction.project_id, prediction.task_id, prediction.run_id, "prediction_scored", message.clone())
            .from("orchestrator")
            .data(json!({"prediction_id": prediction.id, "outcome": result.outcome, "return_pct": result.return_pct, "brier": result.brier}))
            .publish(state)
            .await;
        let scope = super::memory::Scope { project_id: prediction.project_id, task_id: Some(prediction.task_id), run_id: Some(prediction.run_id), agent_key: Some("evaluator".into()) };
        super::memory::retain(&state.harness, &scope, vec![super::memory::NewMemory {
            kind: "experience".into(),
            content: format!("{message} (scored {}).", Utc::now().format("%Y-%m-%d")),
            entities: vec![prediction.symbol.clone()],
            source_url: None,
            importance: 0.8,
            confidence: Some(1.0),
        }]).await;
    }
    scored
}

/// Background loop: scores predictions whose horizon has passed and reports them as events + memory.
pub async fn evaluator(state: crate::SharedState) {
    loop {
        tokio::time::sleep(Duration::from_secs(300)).await;
        score_due(&state).await;
    }
}

pub async fn list_predictions(database: &PgPool, project_id: Option<Uuid>) -> Value {
    let rows: Vec<(Uuid, Uuid, String, String, f32, i32, f64, Option<f64>, Vec<f64>, Option<String>, Option<f64>, Option<f64>, DateTime<Utc>, DateTime<Utc>)> = sqlx::query_as(
        "SELECT id, task_id, symbol, direction, probability, horizon_hours, reference_price, stop, targets, outcome, return_pct, brier, created_at, evaluate_at
         FROM predictions WHERE ($1::uuid IS NULL OR project_id = $1) ORDER BY created_at DESC LIMIT 200",
    )
    .bind(project_id)
    .fetch_all(database)
    .await
    .unwrap_or_default();
    let scored: Vec<_> = rows.iter().filter(|row| row.9.as_deref().is_some_and(|outcome| outcome != "unscored")).collect();
    // hit = the plan's own success definition (target before stop / range held); direction = sign right at the horizon.
    let hits = scored.iter().filter(|row| matches!((row.3.as_str(), row.9.as_deref()), (_, Some("target")) | ("neutral", Some("flat")))).count();
    let direction_right = scored.iter().filter(|row| matches!(row.9.as_deref(), Some("target" | "correct" | "flat"))).count();
    let brier = if scored.is_empty() { None } else { Some(scored.iter().filter_map(|row| row.11).sum::<f64>() / scored.len() as f64) };
    json!({
        "summary": {"total": rows.len(), "scored": scored.len(), "hits": hits, "hit_rate": if scored.is_empty() { None } else { Some(hits as f64 / scored.len() as f64) }, "direction_rate": if scored.is_empty() { None } else { Some(direction_right as f64 / scored.len() as f64) }, "brier": brier},
        "items": rows.iter().map(|row| json!({"id": row.0, "task_id": row.1, "symbol": row.2, "direction": row.3, "probability": row.4, "horizon_hours": row.5, "reference_price": row.6, "stop": row.7, "targets": row.8, "outcome": row.9, "return_pct": row.10, "brier": row.11, "created_at": row.12, "evaluate_at": row.13})).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scores_target_before_stop_and_direction_at_horizon() {
        let path = [(150.2, 149.9, 150.1), (150.9, 150.0, 150.8)];
        let long = score("long", 0.6, 150.0, (None, None), Some(149.5), Some(150.8), &path).unwrap();
        assert_eq!(long.outcome, "target");
        assert!((long.brier - 0.16).abs() < 1e-9);
        let short = score("short", 0.7, 150.0, (None, None), Some(150.5), Some(149.0), &path).unwrap();
        assert_eq!(short.outcome, "stop");
        let no_levels = score("long", 0.55, 150.0, (None, None), None, None, &path).unwrap();
        assert_eq!(no_levels.outcome, "correct");
        assert!((no_levels.brier - 0.2025).abs() < 1e-9);
        // Direction right but target never reached: not a hit under the head-trader definition.
        let missed = score("long", 0.6, 150.0, (None, None), Some(149.0), Some(152.0), &path).unwrap();
        assert_eq!(missed.outcome, "correct");
        assert!((missed.brier - 0.36).abs() < 1e-9);
        let range = score("neutral", 0.7, 150.0, (Some(149.8), Some(151.0)), Some(151.5), None, &path).unwrap();
        assert_eq!(range.outcome, "flat");
        assert_eq!(horizon_hours("1w"), 168);
    }
}

impl Quant {
    /// Retest lab: break → retest → continue entries with fixed-pip stops/targets, wick-depth statistics and a
    /// stop/target grid. Query parameters are passed through to the sidecar.
    pub async fn retest(&self, params: &[(String, String)]) -> Result<Value, String> {
        let query: Vec<(&str, String)> = params.iter().map(|(key, value)| (key.as_str(), value.clone())).collect();
        self.get("/retest", &query, 240).await
    }

    /// Session story: how each recent session behaved, where price is now relative to the body-level lines,
    /// and the next if-then triggers for the fixed-pip day-trade model.
    pub async fn session_story(&self, symbol: &str, interval: &str, pip: Option<f64>, sl_pips: f64, tp_pips: f64, as_of: Option<DateTime<Utc>>) -> Result<Value, String> {
        let mut query = vec![("symbol", symbol.to_string()), ("interval", interval.to_string()), ("days", "3".to_string()), ("sl_pips", sl_pips.to_string()), ("tp_pips", tp_pips.to_string())];
        if let Some(pip) = pip {
            query.push(("pip", pip.to_string()));
        }
        if let Some(as_of) = as_of {
            query.push(("as_of", as_of.to_rfc3339()));
        }
        self.get("/session-story", &query, 180).await
    }

    /// Level reactions: body-level zones across timeframes, every touch classified (rejection, sweep, break,
    /// retest), zone strength, hold-vs-break odds and the M15 → H1 → H4 stack. Parameters pass through.
    pub async fn levels(&self, params: &[(String, String)]) -> Result<Value, String> {
        let query: Vec<(&str, String)> = params.iter().map(|(key, value)| (key.as_str(), value.clone())).collect();
        self.get("/levels", &query, 240).await
    }

    /// Candles plus every drawing on them (swings, boxes, zones, events, patterns, sessions, news, playbook) and a
    /// plain reading of what price is doing, what to wait for and how risky it is.
    pub async fn chart(&self, params: &[(String, String)]) -> Result<Value, String> {
        let query: Vec<(&str, String)> = params.iter().map(|(key, value)| (key.as_str(), value.clone())).collect();
        self.get("/chart", &query, 120).await
    }

    /// Top-down read: per timeframe consolidation box or impulse, wick sweeps that failed to close beyond, and
    /// the playbook that follows (sell zone / buy zone inside a range, retest zone after a close-confirmed break).
    pub async fn mtf(&self, params: &[(String, String)]) -> Result<Value, String> {
        let query: Vec<(&str, String)> = params.iter().map(|(key, value)| (key.as_str(), value.clone())).collect();
        self.get("/mtf", &query, 240).await
    }

    /// Backtest of the body-close structure rules over the available history (can take a few seconds).
    pub async fn backtest(&self, symbol: &str, interval: &str) -> Result<Value, String> {
        self.get("/backtest", &[("symbol", symbol.to_string()), ("interval", interval.to_string())], 180).await
    }

    /// Which FX sessions are open now and when each opened (DST-aware, computed by the sidecar).
    pub async fn sessions(&self) -> Result<Value, String> {
        self.get("/sessions", &[], 10).await
    }

    pub async fn signals(&self, symbol: &str, intervals: &[String]) -> Result<Value, String> {
        self.get("/signals", &[("symbol", symbol.to_string()), ("intervals", intervals.join(","))], 60).await
    }
}

#[derive(Debug, sqlx::FromRow)]
struct Watch {
    id: Uuid,
    project_id: Uuid,
    symbol: String,
    intervals: Vec<String>,
    seen: Vec<String>,
}

/// Human line for one fired signal; tolerant of missing fields.
fn describe_signal(symbol: &str, signal: &Value) -> String {
    let text = |key: &str| signal[key].as_str().map(String::from).or_else(|| signal[key].as_f64().map(|value| value.to_string()));
    let mut parts = vec![format!(
        "{symbol} {} · {}",
        text("interval").unwrap_or_default().to_uppercase(),
        text("type").unwrap_or_else(|| "signal".into()).replace('_', " ")
    )];
    if signal["impulse"].as_bool() == Some(true) { parts.push("impulse".into()) }
    if let Some(price) = text("close").or_else(|| text("price")) { parts.push(format!("closed {price}")) }
    if let Some(session) = text("session_at_close").or_else(|| text("session")) { parts.push(format!("in {session}")) }
    let mtf = &signal["mtf"];
    if let (Some(reference), Some(trend), Some(alignment)) = (mtf["reference_interval"].as_str(), mtf["reference_trend"].as_str(), mtf["alignment"].as_str()) {
        parts.push(format!("{} trend {trend} ({alignment})", reference.to_uppercase()));
    }
    if let Some(probability) = signal["continuation_probability"].as_f64() { parts.push(format!("continuation {:.0}%", probability * 100.0)) }
    if let Some(stop) = text("stop") { parts.push(format!("stop {stop}")) }
    if let Some(targets) = signal["targets"].as_array().filter(|targets| !targets.is_empty()) {
        parts.push(format!("targets {}", targets.iter().filter_map(|target| target.as_f64().map(|value| value.to_string())).collect::<Vec<_>>().join(" / ")));
    }
    parts.join(" · ")
}

/// Background loop: every minute, for each watched instrument, ask the sidecar which signals fired on the latest
/// CLOSED candles and publish new ones (deduplicated by symbol/interval/type/candle close time).
pub async fn watcher(state: crate::SharedState) {
    loop {
        tokio::time::sleep(Duration::from_secs(60)).await;
        let watches: Vec<Watch> = sqlx::query_as("SELECT id, project_id, symbol, intervals, seen FROM watchlist WHERE active").fetch_all(&state.database).await.unwrap_or_default();
        for watch in watches {
            let result = state.harness.quant.signals(&watch.symbol, &watch.intervals).await;
            // Sidecar shape: {intervals: {"1h": {signals: [...], last_closed_candle: {c, ...}}}, ...}
            let signals: Vec<Value> = match result {
                Ok(value) => value["intervals"].as_object().map(|intervals| intervals.iter().flat_map(|(interval, frame)| {
                    let close = frame["last_closed_candle"]["c"].clone();
                    frame["signals"].as_array().cloned().unwrap_or_default().into_iter().map(move |mut signal| {
                        signal["interval"] = json!(interval);
                        if signal.get("close").is_none() { signal["close"] = close.clone() }
                        signal
                    })
                }).collect()).unwrap_or_default(),
                Err(error) => {
                    let _ = sqlx::query("UPDATE watchlist SET last_checked_at = NOW(), last_error = $2 WHERE id = $1").bind(watch.id).bind(&error).execute(&state.database).await;
                    continue;
                }
            };
            let mut seen = watch.seen.clone();
            for signal in signals {
                let key = signal["id"].as_str().map(String::from).unwrap_or_else(|| format!("{}|{}|{}|{}", watch.symbol, signal["interval"].as_str().unwrap_or(""), signal["type"].as_str().unwrap_or(""), signal["candle_close_time"]));
                if seen.contains(&key) { continue }
                seen.push(key);
                let event = crate::events::SwarmEvent {
                    id: Uuid::new_v4(),
                    project_id: watch.project_id,
                    task_id: None,
                    run_id: None,
                    kind: "market_signal".into(),
                    message: describe_signal(&watch.symbol, &signal),
                    from_agent_id: Some("watcher".into()),
                    to_agent_id: None,
                    data: Some(json!({"symbol": watch.symbol, "signal": signal})),
                    created_at: Utc::now(),
                };
                let _ = crate::events::publish(&state, event).await;
            }
            let keep = seen.len().saturating_sub(200);
            let _ = sqlx::query("UPDATE watchlist SET seen = $2, last_checked_at = NOW(), last_error = NULL WHERE id = $1").bind(watch.id).bind(&seen[keep..]).execute(&state.database).await;
        }
    }
}
