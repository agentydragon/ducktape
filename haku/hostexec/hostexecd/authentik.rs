//! Authentik access-token verifier — `hostexecd`'s authorization check.
//!
//! `hostexecd` verifies the operator's short-lived, per-host Authentik token (RS256, via
//! Authentik's JWKS): the issuer, the per-host audience `hostexec-<host>`, expiry, and that the
//! token's `groups` include `hostexec-<run_as>-<host>`. On success it returns the operator's
//! subject for the audit log. Mirrors the repo's Authentik JWT validation
//! (`mcp_infra/authentik_auth`: RS256, 30s clock skew).
//!
//! This is the pure verification given a decoding key. Two things are deliberately *not* here:
//! JWKS fetch + cache (a host-side layer that resolves the `DecodingKey`), and the single-use
//! replay store (host-local state keyed on the token itself). Authority is the operator's own
//! Authentik identity — there is no bespoke host or console key.

use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode};
use serde::Deserialize;

/// Allowed clock skew when checking `exp` (matches `mcp_infra/authentik_auth`).
const CLOCK_SKEW_SECONDS: u64 = 30;

/// The verified operator identity behind an accepted token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatedOperator {
    pub subject: String,
    /// The token's own `exp` (unix seconds). Used to bound the single-use replay entry.
    pub expires_at: u64,
}

#[derive(Deserialize)]
struct Claims {
    sub: String,
    #[serde(default)]
    groups: Vec<String>,
    exp: u64,
}

/// Why a token was rejected. Distinct variants so `hostexecd` can log the exact failure.
#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    /// Signature, issuer, audience, or expiry check failed.
    #[error("token invalid: {0}")]
    Token(#[from] jsonwebtoken::errors::Error),
    /// The token is valid, but its `groups` do not authorize this run_as on this host.
    #[error("token does not carry the required group {required}")]
    NotAuthorized { required: String },
}

/// Verify that an operator's Authentik token authorizes running as the POSIX user `run_as` on
/// `host`.
///
/// Checks, via `jsonwebtoken`: RS256 signature against `key`, `iss == issuer`, `aud` contains
/// `hostexec-<host>`, and `exp` (30s skew). Then requires the `groups` claim to contain
/// `hostexec-<run_as>-<host>`. Returns the operator subject. Single-use (replay) is enforced
/// separately, host-local, keyed on the token — not here.
pub fn verify_operator_token(
    token: &str,
    key: &DecodingKey,
    issuer: &str,
    host: &str,
    run_as: &str,
) -> Result<AuthenticatedOperator, AuthError> {
    let mut validation = Validation::new(Algorithm::RS256);
    validation.leeway = CLOCK_SKEW_SECONDS;
    validation.set_issuer(&[issuer]);
    validation.set_audience(&[format!("hostexec-{host}")]);
    validation.set_required_spec_claims(&["exp", "aud", "iss"]);
    let claims = decode::<Claims>(token, key, &validation)?.claims;

    let required = format!("hostexec-{run_as}-{host}");
    if !claims.groups.iter().any(|group| group == &required) {
        return Err(AuthError::NotAuthorized { required });
    }
    Ok(AuthenticatedOperator {
        subject: claims.sub,
        expires_at: claims.exp,
    })
}
