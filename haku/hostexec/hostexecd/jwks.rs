//! JWKS resolution: fetch Authentik's signing keys and hand `hostexecd` a `DecodingKey` for a
//! token's `kid`. Keys are cached; a `kid` miss triggers one refetch (to pick up a key rotation)
//! before the token is rejected. Authentik's hostexec provider signs RS256 only, so indexing is
//! strict — every JWKS entry must carry a `kid` and convert to a usable key.
//!
//! `index` (the pure JWKS → map step) is unit-tested; the fetch/cache around it is
//! deploy-validated (it needs Authentik's live JWKS endpoint).

use std::collections::HashMap;
use std::sync::RwLock;

use jsonwebtoken::DecodingKey;
use jsonwebtoken::jwk::JwkSet;

#[derive(Debug, thiserror::Error)]
pub enum JwksError {
    #[error("fetching JWKS: {0}")]
    Fetch(#[from] reqwest::Error),
    #[error("JWKS entry has no key id (kid)")]
    MissingKid,
    #[error("converting JWKS entry {kid}: {source}")]
    BadKey {
        kid: String,
        source: jsonwebtoken::errors::Error,
    },
    #[error("no signing key for kid {0}")]
    UnknownKid(String),
}

/// Index a `JwkSet` by `kid`. Errors if any entry lacks a `kid` or does not convert to a key.
pub fn index(set: &JwkSet) -> Result<HashMap<String, DecodingKey>, JwksError> {
    set.keys
        .iter()
        .map(|jwk| {
            let kid = jwk.common.key_id.clone().ok_or(JwksError::MissingKid)?;
            let key = DecodingKey::from_jwk(jwk).map_err(|source| JwksError::BadKey {
                kid: kid.clone(),
                source,
            })?;
            Ok((kid, key))
        })
        .collect()
}

/// A cached view of Authentik's JWKS, refreshed on a `kid` miss.
pub struct Jwks {
    url: String,
    client: reqwest::Client,
    cache: RwLock<HashMap<String, DecodingKey>>,
}

impl Jwks {
    pub fn new(url: String) -> Self {
        Self {
            url,
            client: reqwest::Client::new(),
            cache: RwLock::new(HashMap::new()),
        }
    }

    /// The `DecodingKey` for `kid`. Serves it from cache, else refetches the JWKS once (a rotated
    /// key is unknown until we refetch) before rejecting an unknown `kid`.
    pub async fn key_for(&self, kid: &str) -> Result<DecodingKey, JwksError> {
        if let Some(key) = self.cached(kid) {
            return Ok(key);
        }
        self.refresh().await?;
        self.cached(kid)
            .ok_or_else(|| JwksError::UnknownKid(kid.to_string()))
    }

    fn cached(&self, kid: &str) -> Option<DecodingKey> {
        self.cache
            .read()
            .expect("jwks cache poisoned")
            .get(kid)
            .cloned()
    }

    async fn refresh(&self) -> Result<(), JwksError> {
        let set: JwkSet = self
            .client
            .get(&self.url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        *self.cache.write().expect("jwks cache poisoned") = index(&set)?;
        Ok(())
    }
}
