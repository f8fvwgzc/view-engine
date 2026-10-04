//! Run configuration: global defaults (settings table) overlaid with per-task overrides (tasks.config).
//!
//! There is deliberately no money budget: CLI providers run on the user's own subscription limits and local
//! models are free. Runs are bounded by team size, iterations, rounds and timeouts; efficiency comes from
//! caching, compression and memory (always on), and a provider's own usage-limit error triggers the router's fallback.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::PgPool;

use super::providers::ProviderKind;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RunSettings {
    /// Default provider for every role without an explicit route.
    pub provider: ProviderKind,
    /// Default model (e.g. "sonnet" for Claude); empty = the CLI's own default.
    pub model: Option<String>,
    /// Lighter model for framing and helper calls (intent, search queries, memory consolidation).
    pub utility_model: Option<String>,
    /// Model router: role → "provider:model". Roles: orchestrator, intent, researcher, analyst, specialist,
    /// strategist, critic, utility.
    pub routes: BTreeMap<String, String>,
    /// Tried in order when a route hits its usage limit or is unavailable, e.g. ["claude:haiku", "codex", "ollama:qwen3"].
    pub fallbacks: Vec<String>,
    /// quick | standard | deep
    pub depth: String,
    pub max_agents: usize,
    /// Agents running at the same time (Symphony `max_concurrent_agents`).
    pub concurrency: usize,
    pub max_iterations: i32,
    /// Extra critic-driven rounds allowed after the first pass.
    pub max_rounds: i32,
    /// Node-level retries after in-call retries are exhausted (Symphony failure backoff).
    pub max_attempts: i32,
    pub agent_timeout_secs: u64,
    /// Let agents ask the orchestrator to hire extra specialists mid-run.
    pub allow_agent_hiring: bool,
    /// Reasoning effort passed to providers that support it: low | medium | high | xhigh | max. None = the
    /// provider's default; the fast day-trade path uses low unless this is set.
    pub effort: Option<String>,
}

impl Default for RunSettings {
    fn default() -> Self {
        Self {
            provider: ProviderKind::Claude,
            model: Some("sonnet".into()),
            utility_model: Some("haiku".into()),
            routes: BTreeMap::new(),
            fallbacks: Vec::new(),
            depth: "standard".into(),
            max_agents: 8,
            concurrency: 3,
            max_iterations: 2,
            max_rounds: 1,
            max_attempts: 3,
            agent_timeout_secs: 900,
            allow_agent_hiring: true,
            effort: None,
        }
    }
}

impl RunSettings {
    pub async fn load(database: &PgPool, task_config: &Value) -> Self {
        let global: Value = sqlx::query_scalar("SELECT value FROM settings WHERE id = 1").fetch_optional(database).await.ok().flatten().unwrap_or(Value::Null);
        let mut merged = serde_json::to_value(Self::default()).unwrap_or_default();
        for layer in [&global, task_config] {
            if let (Some(target), Some(source)) = (merged.as_object_mut(), layer.as_object()) {
                for (key, value) in source {
                    if !value.is_null() {
                        target.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        let mut settings: Self = serde_json::from_value(merged).unwrap_or_default();
        // A task that picks its own provider/model means "use this for the whole team": global routes would fight it.
        if task_config.get("provider").is_some_and(|value| !value.is_null()) || task_config.get("model").is_some_and(|value| !value.is_null()) {
            settings.routes.clear();
            if task_config.get("provider").is_some() && task_config.get("utility_model").is_none() {
                settings.utility_model = settings.model.clone();
            }
        }
        settings.apply_depth();
        settings
    }

    fn apply_depth(&mut self) {
        match self.depth.as_str() {
            "quick" => {
                self.max_agents = self.max_agents.min(5);
                self.max_iterations = 1;
                self.max_rounds = 0;
            }
            "deep" => {
                self.max_agents = self.max_agents.max(12);
                self.max_iterations = self.max_iterations.max(3);
                self.max_rounds = self.max_rounds.max(1);
            }
            _ => {}
        }
        self.max_agents = self.max_agents.clamp(3, 24);
        self.concurrency = self.concurrency.clamp(1, 8);
        self.max_iterations = self.max_iterations.clamp(1, 5);
        self.max_rounds = self.max_rounds.clamp(0, 3);
        self.max_attempts = self.max_attempts.clamp(1, 6);
        self.agent_timeout_secs = self.agent_timeout_secs.clamp(60, 3600);
        self.effort = self.effort.take().map(|effort| effort.trim().to_lowercase()).filter(|effort| ["low", "medium", "high", "xhigh", "max"].contains(&effort.as_str()));
    }
}

pub async fn get_global(database: &PgPool) -> Value {
    let stored: Value = sqlx::query_scalar("SELECT value FROM settings WHERE id = 1").fetch_optional(database).await.ok().flatten().unwrap_or(Value::Null);
    let mut merged = serde_json::to_value(RunSettings::default()).unwrap_or_default();
    if let (Some(target), Some(source)) = (merged.as_object_mut(), stored.as_object()) {
        for (key, value) in source {
            if target.contains_key(key) {
                target.insert(key.clone(), value.clone());
            }
        }
    }
    merged
}

pub async fn put_global(database: &PgPool, value: &Value) -> Result<Value, String> {
    let parsed: RunSettings = serde_json::from_value(value.clone()).map_err(|error| format!("invalid settings: {error}"))?;
    for (role, spec) in &parsed.routes {
        if !super::router::ROLES.contains(&role.as_str()) {
            return Err(format!("unknown route role `{role}`"));
        }
        if !spec.trim().is_empty() && super::router::Route::parse(spec).is_none() {
            return Err(format!("route `{spec}` must look like provider:model (providers: claude, codex, copilot, ollama, openai, simulated)"));
        }
    }
    let mut normalized = serde_json::to_value(parsed).map_err(|error| error.to_string())?;
    if let Some(routes) = normalized.get_mut("routes").and_then(Value::as_object_mut) {
        routes.retain(|_, spec| spec.as_str().is_some_and(|spec| !spec.trim().is_empty()));
    }
    sqlx::query("INSERT INTO settings (id, value, updated_at) VALUES (1, $1, NOW()) ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()")
        .bind(&normalized)
        .execute(database)
        .await
        .map_err(|error| error.to_string())?;
    Ok(normalized)
}
