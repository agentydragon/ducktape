//! Console-signed exec capability verifier — the Rust side of the cross-language JWT contract.
//!
//! The console mints capability JWTs (`haku/hostexec/capability.py`, PyJWT, EdDSA); `hostexecd`
//! verifies them here with `jsonwebtoken`. Both use standard RFC 7519 JWT + RFC 8037 EdDSA
//! (Ed25519), so interop is guaranteed by the standard — no hand-rolled encoding to keep in
//! lockstep. The pinned Python-signed vectors in `capability_test.rs` prove it against real
//! console-minted tokens. This is one of the two independent checks `hostexecd` requires; the
//! other is the operator's Authentik token (the revocable `hostexec-<run_as>-<host>` grant).

use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode};
use serde::Deserialize;

/// Audience that pins a JWT to the capability purpose (must match `capability.py`), so an
/// Authentik token can never be replayed as a capability, or vice versa.
pub const CAPABILITY_AUDIENCE: &str = "hostexec-capability";

/// The POSIX user a command runs as. Deserializes from the lowercase name (matches Python `RunAs`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunAs {
    Agentydragon,
    Root,
}

/// The approved command carried in the capability JWT's claims. `aud` is validated by
/// `jsonwebtoken` (not deserialized here); unknown claims are ignored.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct CapabilityClaims {
    pub host: String,
    pub run_as: RunAs,
    pub argv: Vec<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    pub nonce: String,
    pub exp: u64,
}

/// Why a capability was rejected. Distinct variants so `hostexecd` can log the exact failure.
#[derive(Debug, thiserror::Error)]
pub enum CapabilityError {
    /// Signature, audience, or expiry check failed.
    #[error("capability token invalid: {0}")]
    Token(#[from] jsonwebtoken::errors::Error),
    /// The request's host/run_as disagree with the signed capability.
    #[error(
        "capability authorizes {claim_host}/{claim_run_as:?}, request is {req_host}/{req_run_as:?}"
    )]
    RunAsMismatch {
        claim_host: String,
        claim_run_as: RunAs,
        req_host: String,
        req_run_as: RunAs,
    },
    /// The request's argv is not the argv the capability approved.
    #[error("request argv does not match the approved command")]
    ArgvMismatch,
}

/// Fail-closed capability verification: signature + audience + expiry (`jsonwebtoken`), then a
/// cross-check that the request's `host`/`run_as`/`argv` equal the signed claims. Mirrors
/// `verify_capability` in `capability.py`. Nonce single-use is the caller's replay-store concern,
/// not checked here (it is host-local state, not a property of the token).
pub fn verify_capability(
    token: &str,
    public_key_pem: &[u8],
    host: &str,
    run_as: RunAs,
    argv: &[String],
) -> Result<CapabilityClaims, CapabilityError> {
    let key = DecodingKey::from_ed_pem(public_key_pem)?;
    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.set_audience(&[CAPABILITY_AUDIENCE]);
    validation.set_required_spec_claims(&["exp", "aud"]);
    let claims = decode::<CapabilityClaims>(token, &key, &validation)?.claims;
    if claims.host != host || claims.run_as != run_as {
        return Err(CapabilityError::RunAsMismatch {
            claim_host: claims.host,
            claim_run_as: claims.run_as,
            req_host: host.to_string(),
            req_run_as: run_as,
        });
    }
    if claims.argv != argv {
        return Err(CapabilityError::ArgvMismatch);
    }
    Ok(claims)
}
