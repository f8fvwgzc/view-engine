//! The research harness: everything between "a task was dispatched" and "a decision report exists".
//!
//! - `providers` — adapters for Claude Code CLI (tested), Codex/Copilot CLIs, Ollama, OpenAI-compatible servers
//! - `process`   — subprocess runner with process-group timeout handling
//! - `retry`     — backoff and error classification (texc-symphony / Codex)
//! - `ledger`    — token accounting and the response cache
//! - `compress`  — headroom/rtk-style compression of anything entering a prompt
//! - `web`       — search/fetch for providers without native browsing
//! - `skills`    — SKILL.md library with progressive disclosure
//! - `memory`    — long-term project memory with hybrid lexical + vector retrieval
//! - `orchestrator` — the single orchestrator that hires agents, runs the dependency graph and loops

pub mod compress;
pub mod ledger;
pub mod market;
pub mod memory;
pub mod orchestrator;
pub mod process;
pub mod prompts;
pub mod providers;
pub mod retry;
pub mod rlcd;
pub mod router;
pub mod settings;
pub mod simulation;
pub mod skills;
pub mod web;

use sqlx::PgPool;
use uuid::Uuid;

use self::{
    memory::Embedder,
    providers::{LlmError, LlmRequest, LlmResponse, ProviderConfig},
    retry::{call_backoff, ErrorClass},
    router::Route,
    skills::SkillLibrary,
};

/// In-call retries for transient provider errors (Codex request default is 4; we keep CLI calls cheaper).
const CALL_ATTEMPTS: u32 = 3;
const LLM_CACHE_TTL_HOURS: i64 = 72;

pub struct Harness {
    pub database: PgPool,
    pub http: reqwest::Client,
    pub providers: ProviderConfig,
    pub skills: SkillLibrary,
    pub embedder: Embedder,
    /// Quant sidecar client (market data, options positioning, ML) for trading desk runs.
    pub quant: market::Quant,
    /// RLCD client (calibrated decision model): intent routing and the fast day-trade call.
    pub rlcd: rlcd::Rlcd,
    /// Latest subscription-window status per provider (e.g. Claude's five-hour window), shown in the UI.
    pub limits: tokio::sync::RwLock<std::collections::HashMap<String, serde_json::Value>>,
}

/// Who is making an LLM call, for the ledger.
#[derive(Clone, Copy)]
pub struct CallSite {
    pub run_id: Uuid,
    pub agent_id: Option<Uuid>,
    /// Research calls with live web access are not cached: the world changes. Planning/synthesis calls are.
    pub cacheable: bool,
}

impl Harness {
    /// The single gateway every LLM call goes through: cache → provider (with transient retries) → ledger.
    pub async fn llm(&self, request: &LlmRequest, site: CallSite) -> Result<LlmResponse, LlmError> {
        let model = request.model.clone().or_else(|| self.providers.model_for(request.provider)).unwrap_or_default();
        let key = ledger::cache_key(&[request.provider.as_str(), &model, &request.system, &request.prompt, &request.json_schema.as_ref().map(|schema| schema.to_string()).unwrap_or_default()]);
        if site.cacheable {
            if let Some((text, _original_usage)) = ledger::cache_get(&self.database, &key, LLM_CACHE_TTL_HOURS).await {
                ledger::record_usage(&self.database, site.run_id, site.agent_id, &providers::Usage::default(), true).await;
                let structured = request.json_schema.as_ref().and_then(|_| serde_json::from_str(&text).ok());
                return Ok(LlmResponse { text, structured, usage: Default::default(), model: Some(model), duration_ms: 0, cache_hit: true, rate_limit: None });
            }
        }
        let mut attempt = 0;
        loop {
            attempt += 1;
            match providers::complete(&self.providers, &self.http, request).await {
                Ok(mut response) => {
                    if let Some(limit) = response.rate_limit.clone() {
                        self.note_limit(request.provider.as_str(), limit).await;
                    }
                    if response.structured.is_none() && request.json_schema.is_some() {
                        response.structured = extract_json(&response.text);
                    }
                    ledger::record_usage(&self.database, site.run_id, site.agent_id, &response.usage, false).await;
                    if site.cacheable && !response.text.is_empty() {
                        let mut stored = response.clone();
                        if let Some(structured) = &response.structured {
                            stored.text = structured.to_string();
                        }
                        ledger::cache_put(&self.database, &key, request.provider.as_str(), Some(&model), &stored).await;
                    }
                    return Ok(response);
                }
                Err(error) if error.class == ErrorClass::Transient && attempt < CALL_ATTEMPTS => {
                    tracing::warn!("transient provider error (attempt {attempt}/{CALL_ATTEMPTS}): {}", error.message);
                    tokio::time::sleep(call_backoff(attempt).max(std::time::Duration::from_secs(2))).await;
                }
                Err(error) => {
                    if error.class == ErrorClass::Quota {
                        self.note_limit(request.provider.as_str(), serde_json::json!({"status": "limited", "message": error.message})).await;
                    }
                    return Err(error);
                }
            }
        }
    }

    /// Loads skill outcome statistics into the index prior: skills with a good track record rank up to 10% higher,
    /// poor ones up to 10% lower; skills with fewer than three recorded outcomes stay neutral.
    pub async fn refresh_skill_priors(&self) {
        let rows: Vec<(String, i32, i32, i32, i32)> = sqlx::query_as("SELECT skill, accepted, revised, hits, misses FROM skill_stats").fetch_all(&self.database).await.unwrap_or_default();
        let prior = rows
            .into_iter()
            .filter_map(|(skill, accepted, revised, hits, misses)| {
                let outcomes = accepted + revised + hits + misses;
                (outcomes >= 3).then(|| (skill, 0.9 + 0.2 * (accepted + hits) as f64 / outcomes as f64))
            })
            .collect();
        self.skills.set_priors(prior).await;
    }

    pub async fn note_limit(&self, provider: &str, mut info: serde_json::Value) {
        if let Some(object) = info.as_object_mut() {
            object.insert("observed_at".into(), serde_json::json!(chrono::Utc::now()));
        }
        self.limits.write().await.insert(provider.to_string(), info);
    }
}

impl Harness {
    /// Router-aware call: tries each route in order. A route that reports its usage limit or is not configured
    /// hands over to the next one; any other error is returned as-is. Returns the route that answered.
    pub async fn llm_routed(&self, request: &LlmRequest, routes: &[Route], site: CallSite) -> Result<(LlmResponse, Route), LlmError> {
        let mut last_error = LlmError { class: ErrorClass::Configuration, message: "no route configured".into() };
        for route in routes {
            let mut routed = request.clone();
            routed.provider = route.provider;
            routed.model = route.model.clone();
            // Providers without native browsing cannot honour a web request; the harness supplies evidence instead.
            routed.web = request.web && route.provider.native_web();
            match self.llm(&routed, site).await {
                Ok(response) => return Ok((response, route.clone())),
                Err(error) if matches!(error.class, ErrorClass::Quota | ErrorClass::Configuration) => {
                    tracing::warn!("route {} unavailable ({}); trying next route", route.label(), error.message);
                    last_error = error;
                }
                Err(error) => return Err(error),
            }
        }
        Err(last_error)
    }
}

/// Pulls the last JSON object out of model text: a fenced ```json block if present, otherwise the last balanced `{…}`.
pub fn extract_json(text: &str) -> Option<serde_json::Value> {
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(text.trim()) {
        if value.is_object() {
            return Some(value);
        }
    }
    let mut candidates: Vec<&str> = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find("```") {
        let after = &rest[start + 3..];
        let body_start = after.find('\n').map(|index| index + 1).unwrap_or(0);
        let Some(end) = after[body_start..].find("```") else { break };
        candidates.push(&after[body_start..body_start + end]);
        rest = &after[body_start + end + 3..];
    }
    for candidate in candidates.iter().rev() {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(candidate.trim()) {
            if value.is_object() {
                return Some(value);
            }
        }
    }
    // Last balanced object in the text.
    let bytes: Vec<char> = text.chars().collect();
    let mut end = bytes.len();
    while let Some(close) = bytes[..end].iter().rposition(|character| *character == '}') {
        let mut depth = 0i32;
        let mut index = close as i64;
        while index >= 0 {
            match bytes[index as usize] {
                '}' => depth += 1,
                '{' => {
                    depth -= 1;
                    if depth == 0 {
                        let slice: String = bytes[index as usize..=close].iter().collect();
                        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&slice) {
                            return Some(value);
                        }
                        break;
                    }
                }
                _ => {}
            }
            index -= 1;
        }
        end = close;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::extract_json;

    #[test]
    fn extracts_fenced_and_trailing_json() {
        let text = "Report body\n```json\n{\"status\":\"done\",\"n\":1}\n```\n";
        assert_eq!(extract_json(text).unwrap()["status"], "done");
        let trailing = "words words {\"a\": {\"b\": 2}} tail";
        assert_eq!(extract_json(trailing).unwrap()["a"]["b"], 2);
    }
}
