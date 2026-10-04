//! Token ledger and content-addressed response cache.
//!
//! - Response cache key = sha256 over length-prefixed (provider, model, system, prompt, CACHE_VERSION)
//!   (headroom `response_cache` sketch); a hit costs zero tokens and is recorded as such.
//! - Usage aggregation keeps provider-reported cache reads/writes separate from fresh input, as Claude reports
//!   them (`input_tokens` excludes cache reads), so cache efficiency can be shown per run and per agent.
//! - Savings from compression are recorded as raw − compressed tokens (rtk `tracking.rs`, signed then clamped).

use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use uuid::Uuid;

use super::providers::{LlmResponse, Usage};

/// Bump when prompts or compression change so stale cached answers are not reused.
pub const CACHE_VERSION: &str = "godview-harness-v1";

pub fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

pub fn cache_key(parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    for part in parts.iter().chain(std::iter::once(&CACHE_VERSION)) {
        hasher.update((part.len() as u64).to_le_bytes());
        hasher.update(part.as_bytes());
    }
    hex::encode(hasher.finalize())
}

pub async fn cache_get(database: &PgPool, key: &str, ttl_hours: i64) -> Option<(String, Usage)> {
    let row: Option<(String, serde_json::Value)> = sqlx::query_as(
        "UPDATE llm_cache SET hits = hits + 1 WHERE key = $1 AND created_at > NOW() - make_interval(hours => $2::int) RETURNING response, usage",
    )
    .bind(key)
    .bind(ttl_hours as i32)
    .fetch_optional(database)
    .await
    .ok()
    .flatten();
    row.map(|(response, usage)| (response, serde_json::from_value(usage).unwrap_or_default()))
}

pub async fn cache_put(database: &PgPool, key: &str, provider: &str, model: Option<&str>, response: &LlmResponse) {
    let _ = sqlx::query(
        "INSERT INTO llm_cache (key, provider, model, response, usage) VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (key) DO UPDATE SET response = EXCLUDED.response, usage = EXCLUDED.usage, created_at = NOW()",
    )
    .bind(key)
    .bind(provider)
    .bind(model)
    .bind(&response.text)
    .bind(json!(response.usage))
    .execute(database)
    .await;
}

/// Adds one call's usage to the agent row and the run totals in a single round trip each.
pub async fn record_usage(database: &PgPool, run_id: Uuid, agent_id: Option<Uuid>, usage: &Usage, cache_hit: bool) {
    let _ = sqlx::query(
        "UPDATE runs SET tokens_in = tokens_in + $2, tokens_out = tokens_out + $3, tokens_cache_read = tokens_cache_read + $4,
         tokens_cache_write = tokens_cache_write + $5, llm_calls = llm_calls + 1,
         cache_hits = cache_hits + $6 WHERE id = $1",
    )
    .bind(run_id)
    .bind(usage.input)
    .bind(usage.output)
    .bind(usage.cache_read)
    .bind(usage.cache_write)
    .bind(if cache_hit { 1 } else { 0 })
    .execute(database)
    .await;
    if let Some(agent_id) = agent_id {
        let _ = sqlx::query(
            "UPDATE run_agents SET tokens_in = tokens_in + $2, tokens_out = tokens_out + $3, tokens_cache_read = tokens_cache_read + $4,
             tokens_cache_write = tokens_cache_write + $5 WHERE id = $1",
        )
        .bind(agent_id)
        .bind(usage.input)
        .bind(usage.output)
        .bind(usage.cache_read)
        .bind(usage.cache_write)
        .execute(database)
        .await;
    }
}

pub async fn record_savings(database: &PgPool, run_id: Uuid, raw_tokens: usize, compressed_tokens: usize) {
    let saved = raw_tokens.saturating_sub(compressed_tokens) as i64;
    if saved > 0 {
        let _ = sqlx::query("UPDATE runs SET tokens_saved = tokens_saved + $2 WHERE id = $1").bind(run_id).bind(saved).execute(database).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_key_is_length_prefixed() {
        assert_ne!(cache_key(&["ab", "c"]), cache_key(&["a", "bc"]));
        assert_eq!(cache_key(&["a", "b"]), cache_key(&["a", "b"]));
    }
}
