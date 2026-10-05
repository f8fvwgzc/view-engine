//! Client for RLCD (`backend/rlcd`): the calibrated decision model. It answers typed questions (choice, score,
//! noul) about a state with probabilities instead of text. View Engine uses it for two things: deciding what a
//! task is asking for (a position or research, day trade or swing) and a fast calibrated buy / sell / hold call
//! for day trades. Everything here degrades gracefully: with RLCD down, routing falls back to keywords and the
//! desk runs without the model's opinion.

use std::time::Duration;

use serde_json::{json, Value};

pub struct Rlcd {
    base: String,
    http: reqwest::Client,
}

/// What a task is asking for, and who decided.
#[derive(Debug, Clone)]
pub struct Routed {
    /// "trading" or "research".
    pub mode: &'static str,
    /// "daytrade" or "swing" (only meaningful for trading).
    pub style: &'static str,
    /// "rlcd" or "keywords".
    pub source: &'static str,
    /// The model's answers (or the keyword evidence) for the event log and the UI.
    pub detail: Value,
}

/// Below this confidence the model's intent answer is not acted on and the keyword rules decide instead.
const INTENT_CONFIDENCE: f64 = 0.5;
/// The style answer needs less: a wrong style still produces a trade plan, only with a different desk.
const STYLE_CONFIDENCE: f64 = 0.3;

impl Rlcd {
    pub fn from_env(http: reqwest::Client) -> Self {
        let base = std::env::var("RLCD_URL").ok().filter(|url| !url.trim().is_empty()).unwrap_or_else(|| "http://127.0.0.1:8095".into());
        Self { base: base.trim_end_matches('/').to_string(), http }
    }

    async fn send(&self, request: reqwest::RequestBuilder, path: &str, timeout_secs: u64) -> Result<Value, String> {
        let response = request
            .timeout(Duration::from_secs(timeout_secs))
            .send()
            .await
            .map_err(|error| format!("RLCD unreachable at {} ({error}); start it with `make rlcd`", self.base))?;
        let status = response.status();
        let value: Value = response.json().await.map_err(|error| format!("RLCD returned invalid JSON: {error}"))?;
        if status.is_success() { Ok(value) } else { Err(format!("rlcd {path} {status}: {}", value["detail"].as_str().map(String::from).unwrap_or_else(|| value.to_string()))) }
    }

    pub async fn get(&self, path: &str, query: &[(String, String)], timeout_secs: u64) -> Result<Value, String> {
        self.send(self.http.get(format!("{}{path}", self.base)).query(query), path, timeout_secs).await
    }

    pub async fn post(&self, path: &str, body: &Value, timeout_secs: u64) -> Result<Value, String> {
        self.send(self.http.post(format!("{}{path}", self.base)).json(body), path, timeout_secs).await
    }

    pub async fn health(&self) -> Value {
        match self.get("/health", &[], 4).await {
            Ok(value) => value,
            Err(error) => json!({"status": "down", "error": error}),
        }
    }

    /// One request, several typed questions about the same state, answered independently.
    pub async fn systemone(&self, state: &Value, questions: &Value) -> Result<Value, String> {
        self.post("/v1/systemone", &json!({"model": "rlcd-latest", "state": state, "questions": questions}), 8).await
    }

    /// Calibrated buy / sell / hold for a day trade with the fixed stop and targets.
    pub async fn decide(&self, body: &Value) -> Result<Value, String> {
        self.post("/v1/decide", body, 60).await
    }

    /// Outcome or correction for the model to learn from at its next training run. Best effort.
    pub async fn feedback(&self, body: &Value) {
        if let Err(error) = self.post("/v1/feedback", body, 6).await {
            tracing::debug!("rlcd feedback skipped: {error}");
        }
    }

    /// Decides what the task is asking for. The model answers first; when it is unavailable or unsure the
    /// keyword rules decide, and the result says which of the two it was.
    pub async fn route(&self, text: &str, attachments: usize, symbol: Option<&str>, chart_timeframes: &[String]) -> Routed {
        let mut timeframes = mentioned_timeframes(text);
        for frame in mentioned_timeframes(&chart_timeframes.join(" ")) {
            if !timeframes.contains(&frame) {
                timeframes.push(frame);
            }
        }
        let fallback = keyword_route(text, attachments, symbol, &timeframes);
        let state = json!({"text": text, "attachments": attachments, "timeframes": timeframes, "symbol": symbol});
        let questions = json!({
            "intent": {"type": "choice", "head": "intent", "instructions": "What is the user asking for?", "criteria": {
                "position": "A trade decision: long, short or wait, an entry, a stop or a target",
                "research": "Research or analysis that ends in a report or a strategic decision",
                "other": "Anything else",
            }},
            "trade_style": {"type": "choice", "head": "trade_style", "instructions": "If this is a trade, which style?", "criteria": {
                "daytrade": "Intraday or scalp: minutes to hours, session timing, small fixed stop",
                "swing": "Days to weeks: fundamentals, macro and positioning matter",
            }},
        });
        let Ok(response) = self.systemone(&state, &questions).await else { return fallback };
        let intent = &response["answers"]["intent"];
        let style = &response["answers"]["trade_style"];
        let detail = json!({"model": response["model"], "intent": intent, "trade_style": style, "keywords": fallback.detail});
        let Some(choice) = intent["choice"].as_str().filter(|_| intent["confidence"].as_f64().unwrap_or(0.0) >= INTENT_CONFIDENCE) else {
            return Routed { detail: json!({"model": response["model"], "intent": intent, "trade_style": style, "keywords": fallback.detail, "note": "model unsure; keyword rules decided"}), ..fallback };
        };
        let mode = if choice == "position" { "trading" } else { "research" };
        let style = match style["choice"].as_str().filter(|_| style["confidence"].as_f64().unwrap_or(0.0) >= STYLE_CONFIDENCE) {
            Some("swing") => "swing",
            Some("daytrade") => "daytrade",
            _ => fallback.style,
        };
        Routed { mode, style, source: "rlcd", detail }
    }
}

/// The rows of a scan response, whatever the service called the list.
pub fn scan_rows(scan: &Value) -> Vec<Value> {
    scan["results"].as_array().or_else(|| scan["decisions"].as_array()).or_else(|| scan["ranked"].as_array()).or_else(|| scan.as_array()).cloned().unwrap_or_default()
}

/// One line per instrument, best first: what the model would do at this session open.
pub fn scan_summary(rows: &[Value]) -> String {
    let actionable: Vec<String> = rows
        .iter()
        .filter(|row| row["action"].as_str().is_some_and(|action| action != "hold"))
        .map(|row| {
            // Each row carries the full decision under "decision"; older shapes had the fields on the row itself.
            let decision = if row["decision"].is_object() { &row["decision"] } else { row };
            format!("{} {} ({}, {:.0}%)", row["symbol"].as_str().unwrap_or("?"), row["action"].as_str().unwrap_or("?").to_uppercase(), row["tier"].as_str().unwrap_or("?"), decision["probabilities"][row["action"].as_str().unwrap_or("hold")].as_f64().unwrap_or(0.0) * 100.0)
        })
        .collect();
    if actionable.is_empty() { format!("no trade on any of {} instruments", rows.len()) } else { actionable.join(" · ") }
}

/// Monday to Friday, at every session open: ask RLCD for its calibrated call on the major pairs plus everything
/// on a watchlist, and post the ranked result to each project that watches the market.
pub async fn session_scanner(state: crate::SharedState) {
    const MAJORS: &[&str] = &["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"];
    // A session counts as "just opened" for this long; one scan per (session, open time).
    const WINDOW_MINUTES: i64 = 20;
    let mut scanned: Vec<String> = Vec::new();
    loop {
        tokio::time::sleep(Duration::from_secs(60)).await;
        let Ok(sessions) = state.harness.quant.sessions().await else { continue };
        if sessions["fx_market_open"].as_bool() != Some(true) {
            continue;
        }
        let now = chrono::Utc::now();
        let opened: Vec<(String, String)> = sessions["sessions"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|session| session["active"].as_bool() == Some(true))
            .filter_map(|session| {
                let open = session["open_utc"].as_str()?;
                let at = chrono::DateTime::parse_from_rfc3339(open).ok()?.with_timezone(&chrono::Utc);
                ((now - at).num_minutes() < WINDOW_MINUTES).then(|| (session["name"].as_str().unwrap_or("session").to_string(), open.to_string()))
            })
            .collect();
        for (name, open) in opened {
            let key = format!("{name}|{open}");
            if scanned.contains(&key) {
                continue;
            }
            scanned.push(key);
            if scanned.len() > 64 {
                scanned.remove(0);
            }
            let watched: Vec<(uuid::Uuid, String)> = sqlx::query_as("SELECT project_id, symbol FROM watchlist WHERE active").fetch_all(&state.database).await.unwrap_or_default();
            if watched.is_empty() {
                continue;
            }
            let mut symbols: Vec<String> = MAJORS.iter().map(|symbol| symbol.to_string()).collect();
            for (_, symbol) in &watched {
                if !symbols.contains(symbol) {
                    symbols.push(symbol.clone());
                }
            }
            let Ok(scan) = state.harness.rlcd.post("/v1/scan", &json!({"symbols": symbols, "interval": "15m", "sl_pips": 20, "tp_pips": 50, "tp2_pips": 100}), 120).await else { continue };
            let rows = scan_rows(&scan);
            let message = format!("{name} session open — RLCD scan: {}.", scan_summary(&rows));
            let mut projects: Vec<uuid::Uuid> = watched.iter().map(|(project, _)| *project).collect();
            projects.sort();
            projects.dedup();
            for project_id in projects {
                let event = crate::events::SwarmEvent {
                    id: uuid::Uuid::new_v4(),
                    project_id,
                    task_id: None,
                    run_id: None,
                    kind: "session_scan".into(),
                    message: message.clone(),
                    from_agent_id: Some("rlcd".into()),
                    to_agent_id: None,
                    data: Some(json!({"session": name, "open_utc": open, "scan": scan})),
                    created_at: now,
                };
                let _ = crate::events::publish(&state, event).await;
            }
        }
    }
}

/// Timeframe words in the request, normalised (m5, 15m, H1, 4h, daily …).
fn mentioned_timeframes(text: &str) -> Vec<&'static str> {
    let lower = text.to_lowercase();
    let has = |needles: &[&str]| needles.iter().any(|needle| lower.split(|c: char| !c.is_alphanumeric()).any(|word| word == *needle));
    let mut frames = Vec::new();
    for (frame, needles) in [
        ("5m", &["m5", "5m", "5min"][..]),
        ("15m", &["m15", "15m", "15min"][..]),
        ("1h", &["h1", "1h", "hourly"][..]),
        ("4h", &["h4", "4h"][..]),
        ("1d", &["d1", "1d", "daily"][..]),
        ("1wk", &["w1", "1w", "weekly"][..]),
    ] {
        if has(needles) {
            frames.push(frame);
        }
    }
    frames
}

/// Keyword rules used when the model is down or unsure. Deliberately simple and explainable.
fn keyword_route(text: &str, attachments: usize, symbol: Option<&str>, timeframes: &[&'static str]) -> Routed {
    let lower = text.to_lowercase();
    let any = |needles: &[&str]| needles.iter().any(|needle| lower.contains(needle));
    let asks_position = any(&["long", "short", "buy", "sell", "entry", "position", "stop loss", " sl ", " tp ", "take profit", "target", "scalp", "trade", "pip"]);
    let asks_research = any(&["research", "report", "compare", "strategy for", "market entry", "should we", "analyse the market for", "analyze the market for"]);
    let trading = (attachments > 0 && !asks_research) || (symbol.is_some() && asks_position) || (asks_position && !asks_research && !timeframes.is_empty());
    let swing_words = any(&["swing", "this week", "next week", "this month", "weeks", "fundamental", "macro", "hold for days", "position trade"]);
    let intraday_words = any(&["today", "now", "scalp", "day trade", "daytrade", "intraday", "session", "london", "new york", "asia", "pip"]);
    let low_frames = timeframes.iter().any(|frame| matches!(*frame, "5m" | "15m" | "1h"));
    let high_frames = timeframes.iter().any(|frame| matches!(*frame, "1d" | "1wk"));
    let style = if !intraday_words && !low_frames && (swing_words || high_frames) { "swing" } else { "daytrade" };
    Routed {
        mode: if trading { "trading" } else { "research" },
        style,
        source: "keywords",
        detail: json!({"attachments": attachments, "symbol": symbol, "timeframes": timeframes, "asks_position": asks_position, "asks_research": asks_research}),
    }
}

/// The model's day-trade call as a data-pack section the head trader can weigh.
pub fn decision_markdown(decision: &Value) -> String {
    let pct = |value: &Value| value.as_f64().map(|p| format!("{:.0}%", p * 100.0)).unwrap_or_else(|| "–".into());
    let number = |value: &Value| value.as_f64().map(|v| format!("{v}")).unwrap_or_else(|| "–".into());
    let probabilities = &decision["probabilities"];
    let mut out = format!(
        "Model {} · action **{}** (tier: {}) · P(buy) {} · P(sell) {} · P(neither reaches its target) {} · confidence {:.2} · breakeven probability {}\nExpected R: buy {:.2}, sell {:.2}. Entry {} · stop {} · targets {}.\n",
        decision["model"].as_str().unwrap_or("rlcd"),
        decision["action"].as_str().unwrap_or("hold").to_uppercase(),
        decision["tier"].as_str().unwrap_or("hold"),
        pct(&probabilities["buy"]),
        pct(&probabilities["sell"]),
        pct(&probabilities["hold"]),
        decision["confidence"].as_f64().unwrap_or(0.0),
        pct(&decision["breakeven_probability"]),
        decision["expected_r"]["buy"].as_f64().unwrap_or(0.0),
        decision["expected_r"]["sell"].as_f64().unwrap_or(0.0),
        number(&decision["entry"]),
        number(&decision["stop"]),
        decision["targets"].as_array().map(|targets| targets.iter().map(number).collect::<Vec<_>>().join(" / ")).unwrap_or_else(|| "–".into()),
    );
    for (title, key) in [("Why", "reasons"), ("Warnings", "warnings")] {
        if let Some(items) = decision[key].as_array().filter(|items| !items.is_empty()) {
            out.push_str(&format!("{title}:\n"));
            for item in items {
                out.push_str(&format!("- {}\n", item.as_str().or_else(|| item["text"].as_str()).map(String::from).unwrap_or_else(|| item.to_string())));
            }
        }
    }
    if let Some(groups) = decision["reason_groups"].as_object().filter(|groups| !groups.is_empty()) {
        let mut shares: Vec<(&String, f64)> = groups.iter().filter_map(|(group, share)| Some((group, share.as_f64()?))).collect();
        shares.sort_by(|a, b| b.1.total_cmp(&a.1));
        out.push_str(&format!("What drove it: {}.\n", shares.iter().map(|(group, share)| format!("{group} {:.0}%", share * 100.0)).collect::<Vec<_>>().join(", ")));
    }
    let posterior = &decision["bernoulli"]["posterior"];
    if let Some(trials) = posterior["n"].as_f64().filter(|trials| *trials > 0.0) {
        out.push_str(&format!("Track record of this probability band on held-back history: {} wins in {trials:.0} trades ({}), chance the true win rate beats breakeven {}.\n", posterior["wins"].as_f64().map(|wins| format!("{wins:.0}")).unwrap_or_else(|| "?".into()), pct(&posterior["mean"]), pct(&posterior["p_above_breakeven"])));
    }
    out.push_str("These probabilities are calibrated on history: treat them as the base rate for this exact chart state. Disagree only with a named reason from the closes, and say so.\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keywords_route_screenshots_and_position_questions_to_the_desk() {
        let frames = mentioned_timeframes("xauusd m15 long or short now?");
        assert_eq!(frames, vec!["15m"]);
        let routed = keyword_route("xauusd m15 long or short now?", 0, Some("XAUUSD"), &frames);
        assert_eq!((routed.mode, routed.style), ("trading", "daytrade"));
        let screenshot = keyword_route("what do you think", 1, None, &[]);
        assert_eq!(screenshot.mode, "trading");
    }

    #[test]
    fn keywords_route_research_and_swing() {
        let research = keyword_route("Research the best vector database and write a report", 0, None, &[]);
        assert_eq!(research.mode, "research");
        let frames = mentioned_timeframes("gold swing for next week on the daily, buy or sell?");
        let swing = keyword_route("gold swing for next week on the daily, buy or sell?", 0, Some("XAUUSD"), &frames);
        assert_eq!((swing.mode, swing.style), ("trading", "swing"));
    }
}
