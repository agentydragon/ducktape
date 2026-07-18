//! Single-use replay store: reject a token `hostexecd` has already executed.
//!
//! Keyed on a SHA-256 of the token (Authentik access tokens don't reliably carry a `jti`, and a
//! token hash is a unique per-token id regardless). Each entry expires at the token's own `exp`,
//! so the store stays bounded — evicted lazily on each claim. Thread-safe; the mutex is not a
//! bottleneck (exec is). This is the "single-use" rung: a leaked token is consumed by its
//! legitimate call and cannot be replayed.

use std::collections::HashMap;
use std::sync::Mutex;

use sha2::{Digest, Sha256};

/// A token was presented a second time.
#[derive(Debug, thiserror::Error)]
#[error("token already used")]
pub struct AlreadyUsedError;

#[derive(Default)]
pub struct ReplayStore {
    /// token SHA-256 → the token's `exp` (unix seconds).
    seen: Mutex<HashMap<[u8; 32], u64>>,
}

impl ReplayStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record the first use of `token` (valid until `expires_at`). Errors if it was already used.
    /// `now` is unix seconds; expired entries are evicted so the map stays bounded by live tokens.
    pub fn claim(&self, token: &str, expires_at: u64, now: u64) -> Result<(), AlreadyUsedError> {
        let mut id = [0u8; 32];
        id.copy_from_slice(&Sha256::digest(token.as_bytes()));
        let mut seen = self.seen.lock().expect("replay store mutex poisoned");
        seen.retain(|_, exp| *exp > now);
        if seen.contains_key(&id) {
            return Err(AlreadyUsedError);
        }
        seen.insert(id, expires_at);
        Ok(())
    }
}
