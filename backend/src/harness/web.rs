//! Web research tools used by the harness for providers that cannot browse on their own
//! (Ollama, OpenAI-compatible servers, the offline simulator). Claude/Codex/Copilot use their native web tools.
//!
//! Free sources only, no API keys required: DuckDuckGo's HTML endpoint and the Wikipedia search API.
//! A SearXNG instance can be plugged in with `SEARXNG_URL`. Every query and page is cached in `web_cache`
//! (24h) so agents in the same run never fetch the same thing twice.

use std::time::Duration;

use scraper::{Html, Selector};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::PgPool;

use super::{compress, ledger::sha256_hex};

const USER_AGENT: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) ViewEngineResearch/0.3";
const CACHE_HOURS: i32 = 24;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchHit {
    pub title: String,
    pub url: String,
    pub snippet: String,
    pub source: String,
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::limited(5))
        .build()
        .expect("http client")
}

async fn cached(database: &PgPool, kind: &str, key: &str) -> Option<String> {
    sqlx::query_scalar("SELECT payload FROM web_cache WHERE key = $1 AND kind = $2 AND created_at > NOW() - make_interval(hours => $3)")
        .bind(key)
        .bind(kind)
        .bind(CACHE_HOURS)
        .fetch_optional(database)
        .await
        .ok()
        .flatten()
}

async fn store(database: &PgPool, kind: &str, key: &str, payload: &str) {
    let _ = sqlx::query("INSERT INTO web_cache (key, kind, payload) VALUES ($1, $2, $3) ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, created_at = NOW()")
        .bind(key)
        .bind(kind)
        .bind(payload)
        .execute(database)
        .await;
}

/// Runs every configured search backend and merges results, deduplicated by URL.
pub async fn search(database: &PgPool, http: &reqwest::Client, query: &str, limit: usize) -> Vec<SearchHit> {
    let key = sha256_hex(&format!("search:{query}:{limit}"));
    if let Some(payload) = cached(database, "search", &key).await {
        if let Ok(hits) = serde_json::from_str(&payload) {
            return hits;
        }
    }
    let (searx, ddg, wiki) = tokio::join!(searxng(http, query), duckduckgo(http, query), wikipedia(http, query));
    let mut hits: Vec<SearchHit> = Vec::new();
    for hit in searx.into_iter().chain(ddg).chain(wiki) {
        if !hits.iter().any(|existing| existing.url == hit.url) {
            hits.push(hit);
        }
    }
    hits.truncate(limit);
    if !hits.is_empty() {
        store(database, "search", &key, &serde_json::to_string(&hits).unwrap_or_default()).await;
    }
    hits
}

async fn duckduckgo(http: &reqwest::Client, query: &str) -> Vec<SearchHit> {
    let Ok(response) = http.post("https://html.duckduckgo.com/html/").form(&[("q", query)]).send().await else { return vec![] };
    let Ok(body) = response.text().await else { return vec![] };
    let document = Html::parse_document(&body);
    let (Ok(result), Ok(link), Ok(snippet)) = (Selector::parse(".result"), Selector::parse(".result__a"), Selector::parse(".result__snippet")) else { return vec![] };
    document
        .select(&result)
        .filter_map(|node| {
            let anchor = node.select(&link).next()?;
            let href = anchor.value().attr("href")?;
            let url = decode_ddg_redirect(href)?;
            Some(SearchHit {
                title: anchor.text().collect::<String>().trim().to_string(),
                url,
                snippet: node.select(&snippet).next().map(|element| element.text().collect::<String>().trim().to_string()).unwrap_or_default(),
                source: "duckduckgo".into(),
            })
        })
        .take(8)
        .collect()
}

fn decode_ddg_redirect(href: &str) -> Option<String> {
    if let Some(position) = href.find("uddg=") {
        let encoded = href[position + 5..].split('&').next()?;
        return urlencoding::decode(encoded).ok().map(|url| url.into_owned());
    }
    href.starts_with("http").then(|| href.to_string())
}

async fn wikipedia(http: &reqwest::Client, query: &str) -> Vec<SearchHit> {
    let url = format!("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3&srsearch={}", urlencoding::encode(query));
    let Ok(response) = http.get(url).send().await else { return vec![] };
    let Ok(value) = response.json::<Value>().await else { return vec![] };
    value["query"]["search"]
        .as_array()
        .map(|results| {
            results
                .iter()
                .filter_map(|result| {
                    let title = result["title"].as_str()?;
                    Some(SearchHit {
                        title: title.to_string(),
                        url: format!("https://en.wikipedia.org/wiki/{}", urlencoding::encode(&title.replace(' ', "_"))),
                        snippet: compress::html_to_text(result["snippet"].as_str().unwrap_or_default()),
                        source: "wikipedia".into(),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

async fn searxng(http: &reqwest::Client, query: &str) -> Vec<SearchHit> {
    let Ok(base) = std::env::var("SEARXNG_URL") else { return vec![] };
    let url = format!("{}/search?format=json&q={}", base.trim_end_matches('/'), urlencoding::encode(query));
    let Ok(response) = http.get(url).send().await else { return vec![] };
    let Ok(value) = response.json::<Value>().await else { return vec![] };
    value["results"]
        .as_array()
        .map(|results| {
            results
                .iter()
                .take(8)
                .filter_map(|result| {
                    Some(SearchHit {
                        title: result["title"].as_str()?.to_string(),
                        url: result["url"].as_str()?.to_string(),
                        snippet: result["content"].as_str().unwrap_or_default().to_string(),
                        source: "searxng".into(),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Fetches a page and returns readable text (HTML stripped, dense blobs elided). Cached per URL.
pub async fn fetch_text(database: &PgPool, http: &reqwest::Client, url: &str) -> Option<String> {
    let key = sha256_hex(&format!("page:{url}"));
    if let Some(text) = cached(database, "page", &key).await {
        return Some(text);
    }
    let response = http.get(url).send().await.ok()?;
    if !response.status().is_success() {
        return None;
    }
    let content_type = response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|value| value.to_str().ok()).unwrap_or("").to_string();
    let body = response.text().await.ok()?;
    let text = if content_type.contains("html") || body.trim_start().starts_with('<') { compress::html_to_text(&body) } else { compress::elide_dense_lines(&body) };
    let text = compress::truncate_chars(&text, 60_000);
    store(database, "page", &key, &text).await;
    Some(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_duckduckgo_redirects() {
        assert_eq!(
            decode_ddg_redirect("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x").as_deref(),
            Some("https://example.com/a?b=1")
        );
    }
}
