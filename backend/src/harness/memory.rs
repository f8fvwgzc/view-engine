//! Long-term project memory.
//!
//! Ported patterns:
//! - Hindsight (vectorize-io/hindsight, MIT): memory kinds (world/experience/opinion/observation), entity
//!   "signals" fed to the lexical arm, augmented embedding text, multi-arm recall fused with RRF (k=60) and only
//!   ±10% multiplicative recency/importance boosts, token-budgeted packing (skip oversized, always return top-1),
//!   and post-run consolidation of facts into observations (creates/updates JSON, never hard delete).
//! - mem0 (mem0ai/mem0, Apache-2.0): content-hash dedup and integer-ID mapping when asking an LLM about memories.
//! - pgvector README: materialised ANN CTE with the distance threshold applied outside it.
//!
//! Facts come straight from agents' structured findings, so storing them costs no extra LLM call.

use std::collections::HashMap;

use pgvector::Vector;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::PgPool;
use uuid::Uuid;

use super::{compress::estimate_tokens, providers::LlmRequest, CallSite, Harness};

pub const DIM: usize = 768;
const HASH_MODEL: &str = "hash-v1";
const NOMIC_MODEL: &str = "nomic-embed-text";

/// Ollama `nomic-embed-text` when reachable, otherwise a deterministic feature-hashing embedder.
/// Both are 768-d so the column type is fixed; rows remember which model produced them.
pub struct Embedder {
    ollama_url: Option<String>,
}

impl Embedder {
    pub async fn detect(http: &reqwest::Client, ollama_url: &str) -> Self {
        let probe = http
            .post(format!("{ollama_url}/api/embed"))
            .timeout(std::time::Duration::from_secs(3))
            .json(&json!({"model": NOMIC_MODEL, "input": ["probe"]}))
            .send()
            .await;
        let usable = match probe {
            Ok(response) if response.status().is_success() => response
                .json::<Value>()
                .await
                .ok()
                .and_then(|value| value["embeddings"][0].as_array().map(|vector| vector.len() == DIM))
                .unwrap_or(false),
            _ => false,
        };
        if usable {
            tracing::info!("memory embeddings: ollama {NOMIC_MODEL}");
        } else {
            tracing::info!("memory embeddings: local {HASH_MODEL} (install `ollama pull {NOMIC_MODEL}` for semantic vectors)");
        }
        Self { ollama_url: usable.then(|| ollama_url.to_string()) }
    }

    pub fn model(&self) -> &'static str {
        if self.ollama_url.is_some() { NOMIC_MODEL } else { HASH_MODEL }
    }

    /// (duplicate threshold, ADD threshold) in cosine similarity; hash vectors measure lexical overlap only.
    fn thresholds(&self) -> f64 {
        if self.ollama_url.is_some() { 0.95 } else { 0.90 }
    }

    pub async fn embed(&self, http: &reqwest::Client, texts: &[String], query: bool) -> Vec<Vec<f32>> {
        if let Some(url) = &self.ollama_url {
            let prefix = if query { "search_query: " } else { "search_document: " };
            let input: Vec<String> = texts.iter().map(|text| format!("{prefix}{text}")).collect();
            if let Ok(response) = http.post(format!("{url}/api/embed")).json(&json!({"model": NOMIC_MODEL, "input": input})).send().await {
                if let Ok(value) = response.json::<Value>().await {
                    if let Some(rows) = value["embeddings"].as_array() {
                        let vectors: Vec<Vec<f32>> = rows
                            .iter()
                            .map(|row| row.as_array().map(|values| values.iter().filter_map(|value| value.as_f64().map(|v| v as f32)).collect()).unwrap_or_default())
                            .collect();
                        if vectors.len() == texts.len() && vectors.iter().all(|vector| vector.len() == DIM) {
                            return vectors;
                        }
                    }
                }
            }
        }
        texts.iter().map(|text| hash_embed(text)).collect()
    }
}

const STOPWORDS: &[&str] = &["the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were", "be", "by", "with", "as", "at", "that", "this", "it", "from", "its", "into", "than", "then", "but", "not", "no", "has", "have", "had", "will", "would", "can", "could", "should", "may", "might"];

fn fnv1a(namespace: u8, text: &str) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in std::iter::once(namespace).chain(text.bytes()) {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

/// Signed feature hashing over unigrams, word bigrams and char 3–5-grams, sublinear tf, L2-normalised.
pub fn hash_embed(text: &str) -> Vec<f32> {
    let lowered = text.to_lowercase();
    let words: Vec<&str> = lowered.split(|character: char| !character.is_alphanumeric()).filter(|word| !word.is_empty() && !STOPWORDS.contains(word)).collect();
    let mut features: HashMap<u64, f32> = HashMap::new();
    let mut add = |namespace: u8, key: &str, weight: f32| *features.entry(fnv1a(namespace, key)).or_default() += weight;
    for word in &words {
        add(b'w', word, 1.0);
        let padded: Vec<char> = format!("<{word}>").chars().collect();
        for n in 3..=5 {
            if padded.len() < n { break }
            let scale = ((padded.len() - n + 1) as f32).sqrt();
            for gram in padded.windows(n) {
                add(b'c', &gram.iter().collect::<String>(), 0.5 / scale);
            }
        }
    }
    for pair in words.windows(2) {
        add(b'b', &format!("{} {}", pair[0], pair[1]), 0.7);
    }
    let mut vector = vec![0f32; DIM];
    for (hash, weight) in features {
        let sign = if hash >> 63 == 1 { -1.0 } else { 1.0 };
        vector[(hash % DIM as u64) as usize] += sign * (1.0 + weight).ln();
    }
    let norm = vector.iter().map(|value| value * value).sum::<f32>().sqrt();
    if norm > 0.0 {
        vector.iter_mut().for_each(|value| *value /= norm);
    }
    vector
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewMemory {
    pub kind: String,
    pub content: String,
    pub entities: Vec<String>,
    pub source_url: Option<String>,
    pub importance: f32,
    pub confidence: Option<f32>,
}

pub struct Scope {
    pub project_id: Uuid,
    pub task_id: Option<Uuid>,
    pub run_id: Option<Uuid>,
    pub agent_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct MemoryHit {
    pub id: Uuid,
    pub kind: String,
    pub content: String,
    pub source_url: Option<String>,
    pub proof_count: i32,
    pub importance: f32,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub score: f64,
    pub sem_rank: Option<i64>,
    pub lex_rank: Option<i64>,
}

#[derive(Debug, Default, Serialize)]
pub struct RetainReport {
    pub added: usize,
    pub reinforced: usize,
}

/// Stores facts with exact-hash and near-duplicate checks. Duplicates reinforce (proof_count++) instead of piling up.
pub async fn retain(harness: &Harness, scope: &Scope, facts: Vec<NewMemory>) -> RetainReport {
    let mut report = RetainReport::default();
    let facts: Vec<NewMemory> = facts.into_iter().filter(|fact| fact.content.trim().len() >= 12).collect();
    if facts.is_empty() {
        return report;
    }
    // Hindsight augments the embedded text with entities so related facts land near each other.
    let texts: Vec<String> = facts.iter().map(|fact| if fact.entities.is_empty() { fact.content.clone() } else { format!("{} [{}]", fact.content, fact.entities.join(", ")) }).collect();
    let vectors = harness.embedder.embed(&harness.http, &texts, false).await;
    let model = harness.embedder.model();
    let duplicate_at = harness.embedder.thresholds();
    for (fact, vector) in facts.into_iter().zip(vectors) {
        let embedding = Vector::from(vector);
        let near: Option<(Uuid, f64)> = sqlx::query_as(
            "WITH nn AS MATERIALIZED (
                SELECT id, embedding <=> $2 AS dist FROM memories
                WHERE project_id = $1 AND embed_model = $3 AND superseded_by IS NULL AND kind = $4
                ORDER BY embedding <=> $2 LIMIT 5)
             SELECT id, 1 - dist FROM nn WHERE 1 - dist >= $5 ORDER BY dist LIMIT 1",
        )
        .bind(scope.project_id)
        .bind(&embedding)
        .bind(model)
        .bind(&fact.kind)
        .bind(duplicate_at)
        .fetch_optional(&harness.database)
        .await
        .ok()
        .flatten();
        if let Some((existing, _similarity)) = near {
            let _ = sqlx::query("UPDATE memories SET proof_count = proof_count + 1, updated_at = NOW(), importance = GREATEST(importance, $2) WHERE id = $1")
                .bind(existing)
                .bind(fact.importance)
                .execute(&harness.database)
                .await;
            report.reinforced += 1;
            continue;
        }
        let inserted = sqlx::query(
            "INSERT INTO memories (id, project_id, task_id, run_id, agent_key, kind, content, signals, entities, source_url, importance, confidence, embedding, embed_model)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
             ON CONFLICT (project_id, content_hash) WHERE superseded_by IS NULL
             DO UPDATE SET proof_count = memories.proof_count + 1, updated_at = NOW()",
        )
        .bind(Uuid::new_v4())
        .bind(scope.project_id)
        .bind(scope.task_id)
        .bind(scope.run_id)
        .bind(&scope.agent_key)
        .bind(&fact.kind)
        .bind(fact.content.trim())
        .bind(fact.entities.join(" "))
        .bind(&fact.entities)
        .bind(&fact.source_url)
        .bind(fact.importance.clamp(0.0, 1.0))
        .bind(fact.confidence.map(|value| value.clamp(0.0, 1.0)))
        .bind(&embedding)
        .bind(model)
        .execute(&harness.database)
        .await;
        match inserted {
            Ok(_) => report.added += 1,
            Err(error) => tracing::warn!("memory insert failed: {error}"),
        }
    }
    report
}

/// Hybrid recall: lexical arm (weighted tsvector, OR-of-lexemes, ts_rank_cd) + semantic arm (HNSW cosine),
/// fused by reciprocal rank (k=60), nudged ±10% by recency (90-day half-life) and importance.
pub async fn recall(harness: &Harness, project_id: Uuid, query: &str, limit: i64) -> Vec<MemoryHit> {
    if query.trim().is_empty() {
        return vec![];
    }
    let vector = Vector::from(harness.embedder.embed(&harness.http, &[query.to_string()], true).await.remove(0));
    let mut transaction = match harness.database.begin().await {
        Ok(transaction) => transaction,
        Err(_) => return vec![],
    };
    // pgvector ≥0.8: keep scanning the HNSW graph until enough rows pass the project filter.
    let _ = sqlx::query("SET LOCAL hnsw.ef_search = 100").execute(&mut *transaction).await;
    let _ = sqlx::query("SET LOCAL hnsw.iterative_scan = relaxed_order").execute(&mut *transaction).await;
    let hits = sqlx::query_as::<_, MemoryHit>(
        "WITH q AS (
            SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS tsq
            FROM unnest(tsvector_to_array(to_tsvector('english', $2))) AS lexeme),
         semantic AS MATERIALIZED (
            SELECT id, embedding <=> $3 AS dist FROM memories
            WHERE project_id = $1 AND embed_model = $6 AND superseded_by IS NULL
            ORDER BY embedding <=> $3 LIMIT 100),
         sem AS (SELECT id, row_number() OVER (ORDER BY dist) AS rnk FROM semantic WHERE 1 - dist >= 0.25),
         lex AS (
            SELECT m.id, row_number() OVER (ORDER BY ts_rank_cd(m.search_vector, q.tsq, 32) DESC) AS rnk
            FROM memories m, q
            WHERE m.project_id = $1 AND m.superseded_by IS NULL AND q.tsq IS NOT NULL AND m.search_vector @@ q.tsq
            ORDER BY ts_rank_cd(m.search_vector, q.tsq, 32) DESC LIMIT 100),
         fused AS (
            SELECT COALESCE(s.id, l.id) AS id,
                   COALESCE(1.0 / ($4 + s.rnk), 0) + COALESCE(1.0 / ($4 + l.rnk), 0) AS rrf,
                   s.rnk AS sem_rank, l.rnk AS lex_rank
            FROM sem s FULL OUTER JOIN lex l USING (id))
         SELECT m.id, m.kind, m.content, m.source_url, m.proof_count, m.importance, m.created_at,
                (f.rrf
                  * (0.9 + 0.2 * power(0.5, extract(epoch FROM NOW() - COALESCE(m.occurred_at, m.created_at)) / 86400.0 / 90.0))
                  * (0.9 + 0.2 * m.importance)
                  * CASE WHEN m.kind = 'observation' THEN 1 + 0.05 * LEAST(ln(GREATEST(m.proof_count, 1)), 2) ELSE 1 END)::float8 AS score,
                f.sem_rank, f.lex_rank
         FROM fused f JOIN memories m USING (id)
         ORDER BY score DESC LIMIT $5",
    )
    .bind(project_id)
    .bind(query)
    .bind(&vector)
    .bind(60.0f64)
    .bind(limit)
    .bind(harness.embedder.model())
    .fetch_all(&mut *transaction)
    .await
    .unwrap_or_else(|error| {
        tracing::warn!("memory recall failed: {error}");
        vec![]
    });
    let _ = transaction.commit().await;
    if !hits.is_empty() {
        let ids: Vec<Uuid> = hits.iter().map(|hit| hit.id).collect();
        let database = harness.database.clone();
        tokio::spawn(async move {
            let _ = sqlx::query("UPDATE memories SET access_count = access_count + 1, last_accessed_at = NOW() WHERE id = ANY($1)").bind(ids).execute(&database).await;
        });
    }
    hits
}

/// Packs hits into a prompt block within `budget_tokens` (Hindsight fact_budget: skip oversized, keep top-1).
pub fn render_context(hits: &[MemoryHit], budget_tokens: usize) -> String {
    let mut ordered: Vec<&MemoryHit> = hits.iter().collect();
    ordered.sort_by_key(|hit| if hit.kind == "observation" || hit.kind == "decision" { 0 } else { 1 });
    let mut lines = Vec::new();
    let mut used = 0;
    for hit in ordered {
        let line = format!(
            "- [{}{}] {} ({}){}",
            hit.kind,
            if hit.proof_count > 1 { format!(" ·{} sources", hit.proof_count) } else { String::new() },
            hit.content,
            hit.created_at.format("%Y-%m-%d"),
            hit.source_url.as_ref().map(|url| format!(" <{url}>")).unwrap_or_default()
        );
        let cost = estimate_tokens(&line);
        if used + cost > budget_tokens && !lines.is_empty() {
            continue;
        }
        used += cost;
        lines.push(line);
    }
    lines.join("\n")
}

/// Post-run consolidation (Hindsight reflect, simplified to one call per run): turn this run's new facts into
/// durable observations, preferring updates of existing observations over new ones.
pub async fn consolidate(harness: &Harness, project_id: Uuid, site: CallSite, routes: &[super::router::Route]) -> usize {
    let facts: Vec<(Uuid, String)> = sqlx::query_as(
        "SELECT id, content FROM memories WHERE project_id = $1 AND consolidated_at IS NULL AND kind <> 'observation' AND superseded_by IS NULL ORDER BY importance DESC, created_at DESC LIMIT 24",
    )
    .bind(project_id)
    .fetch_all(&harness.database)
    .await
    .unwrap_or_default();
    if facts.len() < 3 {
        return 0;
    }
    let query = facts.iter().map(|(_, content)| content.as_str()).collect::<Vec<_>>().join(" ");
    let existing: Vec<MemoryHit> = recall(harness, project_id, &query, 12).await.into_iter().filter(|hit| hit.kind == "observation").collect();
    // mem0: show integer ids, never UUIDs, so the model cannot invent identifiers.
    let fact_lines = facts.iter().enumerate().map(|(index, (_, content))| format!("F{index}: {content}")).collect::<Vec<_>>().join("\n");
    let observation_lines = existing.iter().enumerate().map(|(index, hit)| format!("O{index}: {}", hit.content)).collect::<Vec<_>>().join("\n");
    let Some(first) = routes.first() else { return 0 };
    let request = LlmRequest {
        provider: first.provider,
        model: first.model.clone(),
        system: super::prompts::SYSTEM_CONSTITUTION.into(),
        prompt: super::prompts::consolidation_prompt(&fact_lines, &observation_lines),
        web: false,
        timeout: std::time::Duration::from_secs(240),
        json_schema: Some(super::prompts::consolidation_schema()),
        progress: None,
        attachments: vec![],
        effort: None,
    };
    let Ok((response, _)) = harness.llm_routed(&request, routes, site).await else { return 0 };
    let Some(plan) = response.structured else { return 0 };
    let fact_ids: Vec<Uuid> = facts.iter().map(|(id, _)| *id).collect();
    let resolve_sources = |value: &Value| -> Vec<Uuid> {
        value.as_array().map(|items| items.iter().filter_map(|item| item.as_str()).filter_map(|label| label.trim_start_matches('F').parse::<usize>().ok()).filter_map(|index| fact_ids.get(index).copied()).collect()).unwrap_or_default()
    };
    let mut changed = 0;
    let scope = Scope { project_id, task_id: None, run_id: Some(site.run_id), agent_key: Some("orchestrator".into()) };
    for create in plan["creates"].as_array().cloned().unwrap_or_default() {
        let Some(text) = create["text"].as_str() else { continue };
        let sources = resolve_sources(&create["source_facts"]);
        let report = retain(harness, &scope, vec![NewMemory { kind: "observation".into(), content: text.into(), entities: vec![], source_url: None, importance: 0.7, confidence: None }]).await;
        if report.added > 0 {
            let _ = sqlx::query("UPDATE memories SET source_memory_ids = $2, proof_count = GREATEST(cardinality($2::uuid[]), 1) WHERE project_id = $1 AND kind = 'observation' AND content = $3")
                .bind(project_id)
                .bind(&sources)
                .bind(text.trim())
                .execute(&harness.database)
                .await;
            changed += 1;
        }
    }
    for update in plan["updates"].as_array().cloned().unwrap_or_default() {
        let (Some(label), Some(text)) = (update["observation"].as_str(), update["text"].as_str()) else { continue };
        let Some(target) = label.trim_start_matches('O').parse::<usize>().ok().and_then(|index| existing.get(index)) else { continue };
        let sources = resolve_sources(&update["source_facts"]);
        let vector = Vector::from(harness.embedder.embed(&harness.http, &[text.to_string()], false).await.remove(0));
        let _ = sqlx::query("INSERT INTO memory_history (memory_id, event, old_content, new_content, reason) VALUES ($1, 'update', $2, $3, $4)")
            .bind(target.id)
            .bind(&target.content)
            .bind(text)
            .bind(update["reason"].as_str())
            .execute(&harness.database)
            .await;
        let _ = sqlx::query("UPDATE memories SET content = $2, embedding = $3, source_memory_ids = source_memory_ids || $4, proof_count = proof_count + cardinality($4::uuid[]), updated_at = NOW() WHERE id = $1")
            .bind(target.id)
            .bind(text)
            .bind(&vector)
            .bind(&sources)
            .execute(&harness.database)
            .await;
        changed += 1;
    }
    let _ = sqlx::query("UPDATE memories SET consolidated_at = NOW() WHERE id = ANY($1)").bind(&fact_ids).execute(&harness.database).await;
    changed
}

pub async fn list(database: &PgPool, project_id: Uuid, limit: i64) -> Vec<Value> {
    sqlx::query_as::<_, (Uuid, String, String, Option<String>, i32, f32, chrono::DateTime<chrono::Utc>)>(
        "SELECT id, kind, content, source_url, proof_count, importance, created_at FROM memories WHERE project_id = $1 AND superseded_by IS NULL ORDER BY (kind = 'observation') DESC, created_at DESC LIMIT $2",
    )
    .bind(project_id)
    .bind(limit)
    .fetch_all(database)
    .await
    .unwrap_or_default()
    .into_iter()
    .map(|(id, kind, content, source_url, proof_count, importance, created_at)| json!({"id": id, "kind": kind, "content": content, "source_url": source_url, "proof_count": proof_count, "importance": importance, "created_at": created_at}))
    .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cosine(a: &[f32], b: &[f32]) -> f32 {
        a.iter().zip(b).map(|(x, y)| x * y).sum()
    }

    #[test]
    fn hash_embedding_is_normalised_and_lexically_meaningful() {
        let a = hash_embed("Lithium battery prices fell sharply in 2024");
        let b = hash_embed("battery prices for lithium dropped in 2024");
        let c = hash_embed("The film won three awards at Cannes");
        assert_eq!(a.len(), DIM);
        assert!((cosine(&a, &a) - 1.0).abs() < 1e-4);
        assert!(cosine(&a, &b) > cosine(&a, &c));
    }
}
