//! The host-side authorization composition: turn an operator's token + requested `(host, run_as)`
//! into the credentials to drop to, or a typed rejection. This is the whole security decision in
//! one testable place, so the axum handler carries no policy — it calls `authorize` and, on `Ok`,
//! hands the credentials to `run_command`.
//!
//! Three gates, in order:
//!   1. verify the Authentik token (signature, issuer, per-host audience, expiry, `run_as` group);
//!   2. resolve the POSIX user to uid/gid (a group can authorize a user the host doesn't have);
//!   3. claim single-use — the last gate, so a replayed token is rejected before it can exec, and
//!      a request that fails verification or user-resolution does not burn the token.

use std::io;

use jsonwebtoken::DecodingKey;
use replay::ReplayStore;
use users::Credentials;

/// A fully authorized request: who approved it, and the credentials to run it under.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Authorized {
    /// The operator's Authentik subject, for the audit log.
    pub subject: String,
    pub credentials: Credentials,
}

/// Why a request was refused. Each variant is a distinct, loggable reason.
#[derive(Debug, thiserror::Error)]
pub enum AuthorizeError {
    /// Token signature, issuer, audience, expiry, or the `run_as` group check failed.
    #[error(transparent)]
    Auth(#[from] authentik::AuthError),
    /// The token was already used (single-use replay).
    #[error(transparent)]
    Replay(#[from] replay::AlreadyUsedError),
    /// The token authorizes `run_as`, but no such POSIX user exists on this host.
    #[error("run_as user does not exist: {0}")]
    NoSuchUser(String),
    /// The passwd lookup itself failed (not a clean miss).
    #[error("user lookup failed: {0}")]
    UserLookup(io::Error),
}

/// Authorize `token` to run as `run_as` on `host`, returning the operator identity and the
/// credentials to drop to. `key` is the Authentik signing key (resolved from the token's `kid` via
/// JWKS); `now` is unix seconds (injected so the replay TTL is testable).
pub fn authorize(
    token: &str,
    key: &DecodingKey,
    issuer: &str,
    host: &str,
    run_as: &str,
    replay: &ReplayStore,
    now: u64,
) -> Result<Authorized, AuthorizeError> {
    let operator = authentik::verify_operator_token(token, key, issuer, host, run_as)?;
    let credentials = match users::resolve(run_as) {
        Ok(credentials) => credentials,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Err(AuthorizeError::NoSuchUser(run_as.to_string()));
        }
        Err(error) => return Err(AuthorizeError::UserLookup(error)),
    };
    replay.claim(token, operator.expires_at, now)?;
    Ok(Authorized {
        subject: operator.subject,
        credentials,
    })
}
