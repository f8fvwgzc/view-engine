//! Two-tier cache for service-to-service answers (quant, RLCD).
//!
//! - L1: in this process (moka): TinyLFU admission, weighted by bytes, and concurrent loads of one key are
//!   coalesced, so ten agents asking for the same snapshot cost one sidecar call.
//! - L2: Redis: shared by every API process and kept across restarts. Best effort: with Redis down the cache
//!   runs on L1 alone.
//!
//! Freshness comes from the answering service's `Cache-Control` (see `proto/python/view_engine_grpc/policy.py`):
//! within max-age an entry is served as is; within stale-while-revalidate it is served while one background call
//! refreshes it; after that it is gone from both tiers. Keys carry a per-namespace generation, so bumping the
//! generation drops a whole namespace at once (RLCD after a training run).

use std::{
    future::Future,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use bytes::Bytes;
use moka::{future::Cache, Expiry};
use redis::{aio::ConnectionManager, AsyncCommands};

const L1_BYTES: u64 = 128 * 1024 * 1024;
/// Larger answers stay in L1 only: keeps Redis memory and its round trips small.
const L2_MAX_BYTES: usize = 8 * 1024 * 1024;
/// No answer is kept longer than a day, whatever the service says.
const MAX_AGE_CAP: u64 = 86_400;
/// A cache that waits on Redis is slower than no cache.
const REDIS_TIMEOUT: Duration = Duration::from_millis(250);

pub fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_millis() as u64).unwrap_or(0)
}

/// The directives the sidecars send: max-age, stale-while-revalidate, no-store / no-cache.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Freshness {
    pub max_age: u64,
    pub stale: u64,
}

impl Freshness {
    pub fn parse(header: &str) -> Self {
        let mut freshness = Self::default();
        for directive in header.split(',') {
            let directive = directive.trim().to_ascii_lowercase();
            let (name, value) = directive.split_once('=').map(|(name, value)| (name.trim(), value.trim().trim_matches('"'))).unwrap_or((directive.as_str(), ""));
            match name {
                "max-age" => freshness.max_age = value.parse().unwrap_or(0),
                "stale-while-revalidate" => freshness.stale = value.parse().unwrap_or(0),
                "no-store" | "no-cache" => return Self::default(),
                _ => {}
            }
        }
        Self { max_age: freshness.max_age.min(MAX_AGE_CAP), stale: freshness.stale.min(MAX_AGE_CAP) }
    }

    pub fn cacheable(&self) -> bool {
        self.max_age > 0
    }
}

/// One answer: what the service returned plus until when it may be served (epoch milliseconds).
#[derive(Debug)]
pub struct Entry {
    pub status: u16,
    pub body: Bytes,
    pub etag: String,
    pub fresh_until: u64,
    pub stale_until: u64,
}

impl Entry {
    pub fn new(status: u16, body: Bytes, etag: String, freshness: Freshness) -> Self {
        let now = now_ms();
        let fresh_until = now + freshness.max_age * 1000;
        Self { status, body, etag, fresh_until, stale_until: fresh_until + freshness.stale * 1000 }
    }

    pub fn is_fresh(&self) -> bool {
        now_ms() < self.fresh_until
    }

    pub fn is_usable(&self) -> bool {
        now_ms() < self.stale_until
    }

    fn ttl(&self) -> Duration {
        Duration::from_millis(self.stale_until.saturating_sub(now_ms()))
    }

    /// `fresh_until | stale_until | status | etag length | etag | body`, big endian.
    fn encode(&self) -> Vec<u8> {
        let etag = self.etag.as_bytes();
        let mut out = Vec::with_capacity(20 + etag.len() + self.body.len());
        out.extend_from_slice(&self.fresh_until.to_be_bytes());
        out.extend_from_slice(&self.stale_until.to_be_bytes());
        out.extend_from_slice(&self.status.to_be_bytes());
        out.extend_from_slice(&(etag.len().min(u16::MAX as usize) as u16).to_be_bytes());
        out.extend_from_slice(&etag[..etag.len().min(u16::MAX as usize)]);
        out.extend_from_slice(&self.body);
        out
    }

    fn decode(raw: Vec<u8>) -> Option<Self> {
        let raw = Bytes::from(raw);
        let fresh_until = u64::from_be_bytes(raw.get(0..8)?.try_into().ok()?);
        let stale_until = u64::from_be_bytes(raw.get(8..16)?.try_into().ok()?);
        let status = u16::from_be_bytes(raw.get(16..18)?.try_into().ok()?);
        let etag_len = u16::from_be_bytes(raw.get(18..20)?.try_into().ok()?) as usize;
        let etag = String::from_utf8(raw.get(20..20 + etag_len)?.to_vec()).ok()?;
        Some(Self { status, body: raw.slice(20 + etag_len..), etag, fresh_until, stale_until })
    }
}

/// L1 keeps an entry exactly as long as it may be served (fresh or stale); uncacheable answers expire at once,
/// after the callers waiting on the same load have received them.
struct UntilStale;

impl Expiry<String, Arc<Entry>> for UntilStale {
    fn expire_after_create(&self, _: &String, entry: &Arc<Entry>, _: Instant) -> Option<Duration> {
        Some(entry.ttl())
    }

    fn expire_after_update(&self, _: &String, entry: &Arc<Entry>, _: Instant, _: Option<Duration>) -> Option<Duration> {
        Some(entry.ttl())
    }
}

pub struct TieredCache {
    l1: Cache<String, Arc<Entry>>,
    redis: Option<ConnectionManager>,
}

impl TieredCache {
    pub async fn new(redis: &redis::Client) -> Self {
        let l1 = Cache::builder()
            .max_capacity(L1_BYTES)
            .weigher(|key: &String, entry: &Arc<Entry>| (key.len() + entry.etag.len() + entry.body.len() + 64).min(u32::MAX as usize) as u32)
            .expire_after(UntilStale)
            .build();
        let redis = match tokio::time::timeout(Duration::from_secs(2), ConnectionManager::new(redis.clone())).await {
            Ok(Ok(manager)) => Some(manager),
            _ => {
                tracing::warn!("Redis unreachable: the service-to-service cache runs in process only");
                None
            }
        };
        Self { l1, redis }
    }

    pub async fn get(&self, key: &str) -> Option<Arc<Entry>> {
        self.l1.get(key).await.filter(|entry| entry.is_usable())
    }

    pub async fn insert(&self, key: String, entry: Arc<Entry>) {
        self.l1.insert(key, entry).await
    }

    /// Runs `load` once for all concurrent callers of `key`; its result lands in L1 for as long as it may be served.
    pub async fn get_or_load<F>(&self, key: String, load: F) -> Result<Arc<Entry>, Arc<String>>
    where
        F: Future<Output = Result<Arc<Entry>, String>>,
    {
        self.l1.try_get_with(key, load).await
    }

    pub async fn l2_get(&self, key: &str) -> Option<Entry> {
        let mut redis = self.redis.clone()?;
        let raw: Option<Vec<u8>> = tokio::time::timeout(REDIS_TIMEOUT, redis.get(key)).await.ok()?.ok()?;
        Entry::decode(raw?).filter(Entry::is_usable)
    }

    pub async fn l2_put(&self, key: &str, entry: &Entry) {
        let (Some(mut redis), ttl) = (self.redis.clone(), entry.ttl().as_millis() as u64) else { return };
        if ttl == 0 || entry.body.len() > L2_MAX_BYTES {
            return;
        }
        let _ = tokio::time::timeout(REDIS_TIMEOUT, redis.pset_ex::<_, _, ()>(key, entry.encode(), ttl)).await;
    }

    pub async fn generation(&self, namespace: &str) -> u64 {
        let Some(mut redis) = self.redis.clone() else { return 0 };
        tokio::time::timeout(REDIS_TIMEOUT, redis.get::<_, Option<u64>>(format!("viewengine:cache:gen:{namespace}"))).await.ok().and_then(Result::ok).flatten().unwrap_or(0)
    }

    /// New generation of `namespace`, shared through Redis when it is up.
    pub async fn bump_generation(&self, namespace: &str) -> Option<u64> {
        let mut redis = self.redis.clone()?;
        tokio::time::timeout(REDIS_TIMEOUT, redis.incr::<_, _, u64>(format!("viewengine:cache:gen:{namespace}"), 1)).await.ok()?.ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sidecar_directives() {
        assert_eq!(Freshness::parse("max-age=30, stale-while-revalidate=60"), Freshness { max_age: 30, stale: 60 });
        assert_eq!(Freshness::parse("max-age=86400, immutable"), Freshness { max_age: 86_400, stale: 0 });
        assert!(!Freshness::parse("no-store").cacheable());
        assert!(!Freshness::parse("max-age=30, no-cache").cacheable());
        assert!(!Freshness::parse("").cacheable());
        assert_eq!(Freshness::parse("max-age=999999").max_age, MAX_AGE_CAP);
    }

    #[test]
    fn entry_round_trips_through_redis_encoding() {
        let entry = Entry::new(200, Bytes::from_static(b"{\"price\":1.1}"), "W/\"abc\"".into(), Freshness { max_age: 30, stale: 60 });
        let decoded = Entry::decode(entry.encode()).expect("decodes");
        assert_eq!((decoded.status, decoded.body.as_ref(), decoded.etag.as_str()), (200, b"{\"price\":1.1}".as_ref(), "W/\"abc\""));
        assert_eq!((decoded.fresh_until, decoded.stale_until), (entry.fresh_until, entry.stale_until));
        assert!(decoded.is_fresh() && decoded.stale_until - decoded.fresh_until == 60_000);
        assert!(Entry::decode(vec![1, 2, 3]).is_none());
    }
}
