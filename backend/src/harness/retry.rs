//! Retry and backoff policy.
//!
//! `retry_delay` / `DelayType` are ported from texc-symphony
//! (crates/symphony-runtime/src/orchestrator/retry.rs, Apache-2.0, itself a port of openai/symphony):
//! continuation retries fire after 1s, failure retries back off 10s·2^(n-1) up to a cap.
//! `call_backoff` follows openai/codex `async-utils/src/backoff.rs`: 200ms·2^(n-1) with ±10% jitter,
//! used for fast in-call retries of transient provider errors. See NOTICE for attribution.

use std::time::Duration;

use rand::Rng;

pub const CONTINUATION_RETRY_DELAY: Duration = Duration::from_millis(1_000);
pub const FAILURE_RETRY_BASE_MS: u64 = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DelayType {
    Continuation,
    Failure,
}

pub fn retry_delay(attempt: u32, delay_type: DelayType, max_retry_backoff_ms: u64) -> Duration {
    if delay_type == DelayType::Continuation && attempt == 1 {
        return CONTINUATION_RETRY_DELAY;
    }
    let power = attempt.saturating_sub(1).min(10);
    let delay = FAILURE_RETRY_BASE_MS.saturating_mul(1 << power);
    Duration::from_millis(delay.min(max_retry_backoff_ms))
}

/// In-call backoff (Codex style): 200ms·2^(n-1) with ±10% jitter, capped at 60s.
pub fn call_backoff(attempt: u32) -> Duration {
    let base = 200u64.saturating_mul(2u64.saturating_pow(attempt.saturating_sub(1)));
    let jitter = rand::rng().random_range(0.9..1.1);
    Duration::from_millis(((base as f64) * jitter).min(60_000.0) as u64)
}

/// How the harness should react to a provider failure (Codex `CodexErr::retry_delay` + Paperclip `errorFamily`).
#[derive(Debug, Clone, PartialEq)]
pub enum ErrorClass {
    /// Rate limit, overload, 5xx, timeouts, a CLI that crashed mid-stream: retry with backoff.
    Transient,
    /// The subscription's usage window is exhausted: retrying soon is pointless; the router falls back instead.
    Quota,
    /// Missing binary, not logged in, bad model name: needs the operator.
    Configuration,
    /// Anything else: fail this attempt, the scheduler may still retry the node.
    Fatal,
}

impl ErrorClass {
    pub fn retryable(&self) -> bool {
        matches!(self, ErrorClass::Transient)
    }
}

/// Classifies provider error text. Patterns mirror Paperclip's CLAUDE_TRANSIENT_UPSTREAM_RE / CLAUDE_PROVIDER_QUOTA_RE.
pub fn classify(message: &str) -> ErrorClass {
    let text = message.to_ascii_lowercase();
    let any = |needles: &[&str]| needles.iter().any(|needle| text.contains(needle));
    if any(&["usage limit", "5-hour limit", "weekly limit", "quota", "limit reached", "credit balance", "rate_limit_error"]) {
        ErrorClass::Quota
    } else if any(&["not found: claude", "no such file", "not logged in", "please run /login", "invalid api key", "authentication", "not supported when using", "does not exist or you do not have access", "unknown model"]) {
        ErrorClass::Configuration
    } else if any(&["429", "rate limit", "overloaded", "529", "503", "502", "500", "timed out", "timeout", "connection reset", "econnreset", "network", "stream closed", "temporarily unavailable", "harness crash"]) {
        ErrorClass::Transient
    } else {
        ErrorClass::Fatal
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failure_backoff_doubles_and_caps() {
        assert_eq!(retry_delay(1, DelayType::Failure, 300_000), Duration::from_secs(10));
        assert_eq!(retry_delay(2, DelayType::Failure, 300_000), Duration::from_secs(20));
        assert_eq!(retry_delay(9, DelayType::Failure, 300_000), Duration::from_secs(300));
        assert_eq!(retry_delay(1, DelayType::Continuation, 300_000), Duration::from_secs(1));
    }

    #[test]
    fn classifies_common_provider_errors() {
        assert_eq!(classify("API Error: 529 Overloaded"), ErrorClass::Transient);
        assert_eq!(classify("Claude AI usage limit reached|1700000000"), ErrorClass::Quota);
        assert_eq!(classify("The 'gpt-5' model is not supported when using Codex with a ChatGPT account."), ErrorClass::Configuration);
    }
}
