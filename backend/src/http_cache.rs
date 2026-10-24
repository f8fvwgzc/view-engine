//! HTTP caching between the browser and the API.
//!
//! Every 200 answer to a GET under /api/ gets a `Cache-Control` (unless the handler set one) and JSON answers get
//! a weak ETag. The browser then does the work on its own: market reads are reused for a few seconds and refreshed
//! in the background (stale-while-revalidate), replays (`as_of` in the past) never change, and everything else is
//! `no-cache`, i.e. kept but revalidated on each use, which costs a 304 with no body when nothing changed.
//! The edge (Caddy, HTTP/3) passes these headers through and compresses; see edge/Caddyfile.

use axum::{
    body::{Body, HttpBody},
    extract::Request,
    http::{header, HeaderValue, Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
};
use chrono::{DateTime, Utc};
use sha2::{Digest, Sha256};

/// Bodies above this are sent without an ETag rather than buffered.
const MAX_ETAG_BYTES: u64 = 16 * 1024 * 1024;

const NO_STORE: &str = "no-store";
/// Stored, but revalidated with the ETag on every use: always current, cheap when unchanged.
const REVALIDATE: &str = "no-cache";
const MARKET: &str = "private, max-age=15, stale-while-revalidate=45";
const REPLAY: &str = "private, max-age=86400, immutable";
const SYMBOLS: &str = "private, max-age=3600, stale-while-revalidate=86400";
const MODEL_REPORTS: &str = "private, max-age=60, stale-while-revalidate=300";

fn policy(path: &str, query: Option<&str>) -> &'static str {
    match path {
        "/api/health" | "/api/market/health" | "/api/rlcd/health" => NO_STORE,
        "/api/market/symbols" => SYMBOLS,
        "/api/rlcd/heads" | "/api/rlcd/calibration" | "/api/rlcd/bernoulli" => MODEL_REPORTS,
        path if path.starts_with("/api/rlcd/train") => NO_STORE,
        path if path.starts_with("/api/market/") => if replay(query) { REPLAY } else { MARKET },
        _ => REVALIDATE,
    }
}

/// A replay an hour or more in the past is computed from closed candles only and will not change.
fn replay(query: Option<&str>) -> bool {
    query
        .into_iter()
        .flat_map(|query| query.split('&'))
        .filter_map(|pair| pair.strip_prefix("as_of="))
        .filter_map(|value| urlencoding::decode(value).ok())
        .filter_map(|value| DateTime::parse_from_rfc3339(&value).ok())
        .any(|as_of| as_of.with_timezone(&Utc) < Utc::now() - chrono::Duration::hours(1))
}

fn etag_matches(if_none_match: &str, etag: &str) -> bool {
    if_none_match.split(',').map(str::trim).any(|tag| tag == "*" || tag.trim_start_matches("W/") == etag.trim_start_matches("W/"))
}

pub async fn layer(request: Request, next: Next) -> Response {
    if !matches!(*request.method(), Method::GET | Method::HEAD) || !request.uri().path().starts_with("/api/") {
        return next.run(request).await;
    }
    let policy = policy(request.uri().path(), request.uri().query());
    let if_none_match = request.headers().get(header::IF_NONE_MATCH).and_then(|value| value.to_str().ok()).map(str::to_owned);
    let response = next.run(request).await;
    if response.status() != StatusCode::OK {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    parts.headers.entry(header::CACHE_CONTROL).or_insert(HeaderValue::from_static(policy));
    let json = parts.headers.get(header::CONTENT_TYPE).and_then(|value| value.to_str().ok()).is_some_and(|value| value.starts_with("application/json"));
    let bounded = body.size_hint().upper().is_some_and(|size| size <= MAX_ETAG_BYTES);
    if !json || !bounded || policy == NO_STORE || parts.headers.contains_key(header::ETAG) {
        return Response::from_parts(parts, body);
    }
    let Ok(bytes) = axum::body::to_bytes(body, MAX_ETAG_BYTES as usize).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let etag = format!("W/\"{}\"", hex::encode(&Sha256::digest(&bytes)[..12]));
    parts.headers.insert(header::ETAG, HeaderValue::from_str(&etag).expect("hex digest is a valid header value"));
    if if_none_match.is_some_and(|tags| etag_matches(&tags, &etag)) {
        parts.status = StatusCode::NOT_MODIFIED;
        parts.headers.remove(header::CONTENT_TYPE);
        parts.headers.remove(header::CONTENT_LENGTH);
        return Response::from_parts(parts, Body::empty());
    }
    Response::from_parts(parts, Body::from(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn picks_a_policy_per_route() {
        assert_eq!(policy("/api/dashboard", None), REVALIDATE);
        assert_eq!(policy("/api/health", None), NO_STORE);
        assert_eq!(policy("/api/rlcd/train/abc123", None), NO_STORE);
        assert_eq!(policy("/api/market/symbols", None), SYMBOLS);
        assert_eq!(policy("/api/market/chart", Some("symbol=EURUSD&interval=15m")), MARKET);
        assert_eq!(policy("/api/market/chart", Some("symbol=EURUSD&as_of=2024-03-01T12%3A00%3A00Z")), REPLAY);
        let soon = urlencoding::encode(&(Utc::now() - chrono::Duration::minutes(5)).to_rfc3339()).into_owned();
        assert_eq!(policy("/api/market/mtf", Some(&format!("symbol=XAUUSD&as_of={soon}"))), MARKET);
    }

    #[test]
    fn compares_etags_weakly() {
        assert!(etag_matches("W/\"abc\"", "W/\"abc\""));
        assert!(etag_matches("\"abc\", W/\"def\"", "W/\"abc\""));
        assert!(etag_matches("*", "W/\"abc\""));
        assert!(!etag_matches("W/\"abd\"", "W/\"abc\""));
    }
}
