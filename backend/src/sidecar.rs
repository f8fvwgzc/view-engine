//! Transport to the Python sidecars (quant, RLCD): gRPC first, HTTP as the fallback, behind the two-tier cache.
//!
//! Every call goes through `viewengine.sidecar.v1.Sidecar/Call` (`proto/`) on one long-lived HTTP/2 channel per
//! sidecar: multiplexed, keepalive-pinged, gzip on the way back, deadline sent as grpc-timeout. When the gRPC port
//! refuses (sidecar not updated, bridge disabled) the call goes over the old HTTP route instead and gRPC is not
//! tried again for 30 s. Both transports reach the same FastAPI routes and return the same status, body,
//! Cache-Control and ETag.
//!
//! Idempotent calls (GETs and POSTs that are pure queries) are cached by the sidecar's Cache-Control and coalesced;
//! anything else (training, feedback) always goes to the sidecar.

use std::{
    collections::HashSet,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

use bytes::Bytes;
use sha2::{Digest, Sha256};
use tonic::{codec::CompressionEncoding, transport::Channel, Code};

use crate::cache::{now_ms, Entry, Freshness, TieredCache};

pub mod pb {
    tonic::include_proto!("viewengine.sidecar.v1");
}

/// Snapshots and training datasets are well over gRPC's 4 MB default.
const MAX_MESSAGE: usize = 64 * 1024 * 1024;
/// After a refused gRPC call, how long to go straight to HTTP.
const GRPC_RETRY_MS: u64 = 30_000;

#[derive(Clone, Copy, PartialEq)]
pub enum Method {
    Get,
    Post,
}

pub struct SidecarRequest {
    pub method: Method,
    pub path: String,
    pub query: Vec<(String, String)>,
    pub body: Bytes,
    pub timeout: Duration,
    /// Safe to serve from cache and to coalesce with identical concurrent calls.
    pub idempotent: bool,
}

impl SidecarRequest {
    pub fn get(path: &str, query: Vec<(String, String)>, timeout_secs: u64) -> Self {
        Self { method: Method::Get, path: path.into(), query, body: Bytes::new(), timeout: Duration::from_secs(timeout_secs), idempotent: true }
    }

    /// `query` marks a POST whose answer depends only on its body (RLCD's systemone, decide, scan, rank).
    pub fn post(path: &str, body: &serde_json::Value, timeout_secs: u64, query: bool) -> Self {
        Self { method: Method::Post, path: path.into(), query: vec![], body: Bytes::from(body.to_string()), timeout: Duration::from_secs(timeout_secs), idempotent: query }
    }
}

/// A sidecar answer as both transports return it.
struct Answer {
    status: u16,
    body: Bytes,
    cache_control: String,
    etag: String,
}

struct Inner {
    /// Cache namespace and log name: "quant" or "rlcd".
    name: &'static str,
    http_base: String,
    http: reqwest::Client,
    grpc: Option<pb::sidecar_client::SidecarClient<Channel>>,
    grpc_down_until: AtomicU64,
    api_key: Option<String>,
    cache: Option<Arc<TieredCache>>,
    generation: AtomicU64,
    refreshing: Mutex<HashSet<String>>,
}

#[derive(Clone)]
pub struct Sidecar(Arc<Inner>);

pub struct SidecarConfig {
    pub name: &'static str,
    pub http_url: String,
    /// None disables gRPC (`*_GRPC_URL=off`).
    pub grpc_url: Option<String>,
    pub api_key: Option<String>,
}

impl SidecarConfig {
    /// `{PREFIX}_URL` (HTTP), `{PREFIX}_GRPC_URL` (gRPC, "off" disables), `{PREFIX}_API_KEY`.
    pub fn from_env(name: &'static str, prefix: &str, http_default: &str, grpc_default: &str) -> Self {
        let var = |key: &str| std::env::var(format!("{prefix}_{key}")).ok().map(|value| value.trim().to_string()).filter(|value| !value.is_empty());
        let grpc_url = var("GRPC_URL").unwrap_or_else(|| grpc_default.into());
        Self {
            name,
            http_url: var("URL").unwrap_or_else(|| http_default.into()).trim_end_matches('/').to_string(),
            grpc_url: (!matches!(grpc_url.to_ascii_lowercase().as_str(), "off" | "0" | "false")).then_some(grpc_url),
            api_key: var("API_KEY"),
        }
    }
}

impl Sidecar {
    pub async fn new(config: SidecarConfig, http: reqwest::Client, cache: Option<Arc<TieredCache>>) -> Self {
        let grpc = config.grpc_url.as_deref().and_then(|url| match tonic::transport::Endpoint::from_shared(url.to_string()) {
            Ok(endpoint) => Some(
                pb::sidecar_client::SidecarClient::new(
                    endpoint
                        .connect_timeout(Duration::from_secs(2))
                        .tcp_nodelay(true)
                        .http2_keep_alive_interval(Duration::from_secs(30))
                        .keep_alive_timeout(Duration::from_secs(10))
                        .keep_alive_while_idle(true)
                        .http2_adaptive_window(true)
                        .connect_lazy(),
                )
                .accept_compressed(CompressionEncoding::Gzip)
                .max_decoding_message_size(MAX_MESSAGE)
                .max_encoding_message_size(MAX_MESSAGE),
            ),
            Err(error) => {
                tracing::warn!("{} gRPC URL {url:?} is invalid ({error}); using HTTP", config.name);
                None
            }
        });
        let generation = match &cache {
            Some(cache) => cache.generation(config.name).await,
            None => 0,
        };
        Self(Arc::new(Inner {
            name: config.name,
            http_base: config.http_url,
            http,
            grpc,
            grpc_down_until: AtomicU64::new(0),
            api_key: config.api_key,
            cache,
            generation: AtomicU64::new(generation),
            refreshing: Mutex::new(HashSet::new()),
        }))
    }

    pub fn url(&self) -> &str {
        &self.0.http_base
    }

    /// Status and JSON body of a sidecar route. `Err` only when the sidecar cannot be reached on either transport.
    pub async fn call(&self, request: SidecarRequest) -> Result<(u16, Bytes), String> {
        let Some(cache) = self.0.cache.clone().filter(|_| request.idempotent) else {
            return self.origin(&request, None).await.map(|answer| (answer.status, answer.body));
        };
        let key = self.key(&request);
        if let Some(entry) = cache.get(&key).await {
            if !entry.is_fresh() {
                self.refresh(key, request, entry.clone());
            }
            return Ok((entry.status, entry.body.clone()));
        }
        let this = self.clone();
        let load_key = key.clone();
        let entry = cache
            .get_or_load(key, async move {
                match this.cache().l2_get(&load_key).await {
                    Some(entry) => {
                        let entry = Arc::new(entry);
                        if !entry.is_fresh() {
                            this.refresh(load_key, request, entry.clone());
                        }
                        Ok(entry)
                    }
                    None => this.fetch(&load_key, &request, None).await,
                }
            })
            .await
            .map_err(|error| error.to_string())?;
        Ok((entry.status, entry.body.clone()))
    }

    /// Drops every cached answer of this sidecar (new generation; old keys age out of both tiers on their own).
    pub async fn invalidate(&self) {
        let local = self.0.generation.fetch_add(1, Ordering::SeqCst) + 1;
        if let Some(shared) = self.0.cache.as_ref() {
            if let Some(next) = shared.bump_generation(self.0.name).await {
                self.0.generation.fetch_max(next.max(local), Ordering::SeqCst);
            }
        }
    }

    fn cache(&self) -> &TieredCache {
        self.0.cache.as_deref().expect("cached calls only run with a cache")
    }

    fn key(&self, request: &SidecarRequest) -> String {
        let mut query: Vec<&(String, String)> = request.query.iter().collect();
        query.sort_by(|a, b| a.0.cmp(&b.0));
        let mut hash = Sha256::new();
        hash.update(if request.method == Method::Post { "POST " } else { "GET " });
        hash.update(&request.path);
        for (key, value) in query {
            hash.update([0u8]);
            hash.update(key);
            hash.update([1u8]);
            hash.update(value);
        }
        hash.update([2u8]);
        hash.update(&request.body);
        format!("viewengine:cache:{}:{}:{}", self.0.name, self.0.generation.load(Ordering::SeqCst), hex::encode(&hash.finalize()[..16]))
    }

    /// Calls the sidecar (revalidating with `previous`'s ETag when there is one) and stores a cacheable answer in L2.
    async fn fetch(&self, key: &str, request: &SidecarRequest, previous: Option<&Entry>) -> Result<Arc<Entry>, String> {
        let answer = self.origin(request, previous.map(|entry| entry.etag.as_str())).await?;
        let freshness = if (200..300).contains(&answer.status) || answer.status == 304 { Freshness::parse(&answer.cache_control) } else { Freshness::default() };
        let entry = match (answer.status, previous) {
            (304, Some(previous)) => Entry::new(previous.status, previous.body.clone(), answer.etag, freshness),
            (304, None) => return Err(format!("{} answered 304 to a call without a cached copy", self.0.name)),
            (status, _) => Entry::new(status, answer.body, answer.etag, freshness),
        };
        if freshness.cacheable() {
            self.cache().l2_put(key, &entry).await;
        }
        Ok(Arc::new(entry))
    }

    /// Stale-while-revalidate: one background call per key refreshes a copy that is being served stale.
    fn refresh(&self, key: String, request: SidecarRequest, previous: Arc<Entry>) {
        if !self.0.refreshing.lock().map(|mut keys| keys.insert(key.clone())).unwrap_or(false) {
            return;
        }
        let this = self.clone();
        tokio::spawn(async move {
            match this.fetch(&key, &request, Some(previous.as_ref())).await {
                Ok(entry) => this.cache().insert(key.clone(), entry).await,
                Err(error) => tracing::debug!("{} refresh of {} failed: {error}", this.0.name, request.path),
            }
            if let Ok(mut keys) = this.0.refreshing.lock() {
                keys.remove(&key);
            }
        });
    }

    async fn origin(&self, request: &SidecarRequest, etag: Option<&str>) -> Result<Answer, String> {
        if let Some(client) = self.0.grpc.clone().filter(|_| now_ms() >= self.0.grpc_down_until.load(Ordering::Relaxed)) {
            match self.call_grpc(client, request, etag).await {
                Ok(answer) => return Ok(answer),
                Err(status) if matches!(status.code(), Code::Unavailable | Code::Unimplemented) => {
                    self.0.grpc_down_until.store(now_ms() + GRPC_RETRY_MS, Ordering::Relaxed);
                    tracing::debug!("{} gRPC unavailable ({}); falling back to HTTP", self.0.name, status.message());
                }
                Err(status) => return Err(format!("gRPC {:?}: {}", status.code(), status.message())),
            }
        }
        self.call_http(request).await
    }

    async fn call_grpc(&self, mut client: pb::sidecar_client::SidecarClient<Channel>, request: &SidecarRequest, etag: Option<&str>) -> Result<Answer, tonic::Status> {
        let mut call = tonic::Request::new(pb::CallRequest {
            method: if request.method == Method::Post { "POST" } else { "GET" }.into(),
            path: request.path.clone(),
            query: request.query.iter().map(|(key, value)| pb::QueryParam { key: key.clone(), value: value.clone() }).collect(),
            body: request.body.clone(),
            if_none_match: etag.unwrap_or_default().into(),
        });
        call.set_timeout(request.timeout);
        if let Some(value) = self.0.api_key.as_ref().and_then(|key| format!("Bearer {key}").parse().ok()) {
            call.metadata_mut().insert("authorization", value);
        }
        let answer = client.call(call).await?.into_inner();
        Ok(Answer { status: answer.status as u16, body: answer.body, cache_control: answer.cache_control, etag: answer.etag })
    }

    async fn call_http(&self, request: &SidecarRequest) -> Result<Answer, String> {
        let url = format!("{}{}", self.0.http_base, request.path);
        let builder = match request.method {
            Method::Get => self.0.http.get(url),
            Method::Post => self.0.http.post(url).header(reqwest::header::CONTENT_TYPE, "application/json").body(request.body.clone()),
        };
        let builder = match &self.0.api_key {
            Some(key) => builder.bearer_auth(key),
            None => builder,
        };
        let response = builder.query(&request.query).timeout(request.timeout).send().await.map_err(|error| error.to_string())?;
        let header = |name: reqwest::header::HeaderName| response.headers().get(name).and_then(|value| value.to_str().ok()).unwrap_or_default().to_string();
        let (status, cache_control, etag) = (response.status().as_u16(), header(reqwest::header::CACHE_CONTROL), header(reqwest::header::ETAG));
        let body = response.bytes().await.map_err(|error| error.to_string())?;
        Ok(Answer { status, body, cache_control, etag })
    }
}
