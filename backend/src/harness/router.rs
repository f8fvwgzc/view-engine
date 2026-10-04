//! Model router: chooses provider + model per agent role, with an ordered fallback chain.
//!
//! Routes are written as `provider:model` (e.g. `claude:sonnet`, `claude:haiku`, `codex`, `ollama:qwen3`).
//! CLI providers run on the user's own subscriptions, so the router's job is quality-per-limit: heavy reasoning
//! roles get the strong model, framing and helper calls get the light one, and when a provider reports its usage
//! limit (or is not configured) the next route in the chain takes over instead of failing the run.

use serde::{Deserialize, Serialize};

use super::{providers::ProviderKind, settings::RunSettings};

pub const ROLES: &[&str] = &["orchestrator", "intent", "researcher", "analyst", "specialist", "strategist", "critic", "utility"];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Route {
    pub provider: ProviderKind,
    pub model: Option<String>,
}

impl Route {
    pub fn parse(spec: &str) -> Option<Self> {
        let spec = spec.trim();
        if spec.is_empty() {
            return None;
        }
        let (provider, model) = match spec.split_once(':') {
            Some((provider, model)) => (provider, Some(model.trim().to_string()).filter(|model| !model.is_empty())),
            None => (spec, None),
        };
        ProviderKind::parse(provider).map(|provider| Self { provider, model })
    }

    pub fn label(&self) -> String {
        match &self.model {
            Some(model) => format!("{}:{model}", self.provider.as_str()),
            None => self.provider.as_str().to_string(),
        }
    }
}

/// Primary route for a role, followed by the configured fallbacks (deduplicated, order preserved).
pub fn resolve(settings: &RunSettings, role: &str) -> Vec<Route> {
    let primary = settings.routes.get(role).and_then(|spec| Route::parse(spec)).unwrap_or_else(|| default_route(settings, role));
    let mut chain = vec![primary];
    for spec in &settings.fallbacks {
        if let Some(route) = Route::parse(spec) {
            if !chain.contains(&route) {
                chain.push(route);
            }
        }
    }
    chain
}

/// Without explicit routes: the run's provider everywhere; on Claude, light roles use the utility model.
fn default_route(settings: &RunSettings, role: &str) -> Route {
    let light = matches!(role, "intent" | "utility");
    let model = if light && settings.provider == ProviderKind::Claude { settings.utility_model.clone().or_else(|| settings.model.clone()) } else { settings.model.clone() };
    Route { provider: settings.provider, model }
}

/// Role used for routing an agent of the given kind.
pub fn role_for_kind(kind: &str) -> &'static str {
    ROLES.iter().find(|role| **role == kind).copied().unwrap_or("specialist")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_specs_and_builds_fallback_chain() {
        assert_eq!(Route::parse("claude:haiku").unwrap().model.as_deref(), Some("haiku"));
        assert_eq!(Route::parse("ollama").unwrap().provider, ProviderKind::Ollama);
        assert!(Route::parse("nope:x").is_none());
        let mut settings = RunSettings::default();
        settings.routes.insert("strategist".into(), "claude:opus".into());
        settings.fallbacks = vec!["claude:opus".into(), "codex".into()];
        let chain = resolve(&settings, "strategist");
        assert_eq!(chain.iter().map(Route::label).collect::<Vec<_>>(), vec!["claude:opus", "codex"]);
        assert_eq!(resolve(&settings, "intent")[0].model.as_deref(), Some("haiku"));
    }
}
