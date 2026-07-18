//! `hostexecd`: the host-side daemon Haku's console calls to run an approved command.
//!
//! It runs as root, bound to the host's Nebula address (and Nebula-firewalled), and executes only
//! what a verified, single-use, per-host Authentik token authorizes — dropping to the token's
//! `run_as` user. Authority is the operator's own Authentik identity; there is no standing
//! credential and no bespoke host key. The whole authorization decision lives in `authorize`; this
//! file is the transport: resolve the token's signing key via JWKS, authorize, exec, return the
//! result. Fail-closed — any error is a rejection, never a run.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use authorize::{AuthorizeError, authorize};
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use exec::{ExecRequest, run_command};
use jwks::{Jwks, JwksError};
use log::{info, warn};
use replay::ReplayStore;
use serde::Deserialize;

/// Runtime configuration, from the environment (the systemd unit sets these).
struct Config {
    /// This host's short name — the token's audience is `hostexec-<host>` and the required group is
    /// `hostexec-<run_as>-<host>`.
    host: String,
    /// The Authentik issuer URL the token must carry.
    issuer: String,
    /// The Authentik JWKS endpoint that publishes the signing keys.
    jwks_url: String,
    /// Address to bind — the host's Nebula address (e.g. `10.42.x.y:PORT`).
    bind: SocketAddr,
}

fn config_from_env() -> Result<Config> {
    let var = |name: &str| std::env::var(name).with_context(|| format!("{name} must be set"));
    Ok(Config {
        host: var("HOSTEXEC_HOST")?,
        issuer: var("HOSTEXEC_ISSUER")?,
        jwks_url: var("HOSTEXEC_JWKS_URL")?,
        bind: var("HOSTEXEC_BIND")?
            .parse()
            .context("HOSTEXEC_BIND must be a socket address")?,
    })
}

struct App {
    host: String,
    issuer: String,
    jwks: Jwks,
    replay: ReplayStore,
}

/// The request body — mirrors `haku.hostexec.wire.HostexecRequest`.
#[derive(Deserialize)]
struct RunRequest {
    token: String,
    run_as: String,
    argv: Vec<String>,
    cwd: Option<String>,
    max_bytes: usize,
    timeout_ms: u64,
}

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::init();
    let config = config_from_env()?;
    let bind = config.bind;
    let app = Arc::new(App {
        host: config.host,
        issuer: config.issuer,
        jwks: Jwks::new(config.jwks_url),
        replay: ReplayStore::new(),
    });

    let router = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        .route("/exec", post(handle_exec))
        .with_state(app);

    info!("hostexecd listening on {bind}");
    let listener = tokio::net::TcpListener::bind(bind).await?;
    axum::serve(listener, router).await?;
    Ok(())
}

async fn handle_exec(
    State(app): State<Arc<App>>,
    Json(request): Json<RunRequest>,
) -> Result<Json<exec::ExecResult>, (StatusCode, String)> {
    let key = {
        let header = jsonwebtoken::decode_header(&request.token).map_err(|error| {
            (
                StatusCode::UNAUTHORIZED,
                format!("malformed token: {error}"),
            )
        })?;
        let kid = header
            .kid
            .ok_or((StatusCode::UNAUTHORIZED, "token has no kid".to_string()))?;
        app.jwks.key_for(&kid).await.map_err(jwks_status)?
    };

    let authorized = authorize(
        &request.token,
        &key,
        &app.issuer,
        &app.host,
        &request.run_as,
        &app.replay,
        now_unix(),
    )
    .map_err(|error| {
        warn!(
            "rejected exec run_as={} on {}: {error}",
            request.run_as, app.host
        );
        authorize_status(error)
    })?;

    info!(
        "exec approved by {} run_as={} argv0={:?}",
        authorized.subject,
        request.run_as,
        request.argv.first()
    );

    let result = run_command(&ExecRequest {
        argv: request.argv,
        cwd: request.cwd.map(Into::into),
        timeout: Duration::from_millis(request.timeout_ms),
        max_bytes: request.max_bytes,
        credentials: Some(authorized.credentials),
    })
    .await
    .map_err(|error| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("exec failed: {error}"),
        )
    })?;

    Ok(Json(result))
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before 1970")
        .as_secs()
}

/// Map an authorization failure to an HTTP status. The body carries the reason for the console log.
fn authorize_status(error: AuthorizeError) -> (StatusCode, String) {
    let status = match &error {
        AuthorizeError::Auth(_) => StatusCode::UNAUTHORIZED,
        AuthorizeError::Replay(_) => StatusCode::CONFLICT,
        AuthorizeError::NoSuchUser(_) => StatusCode::UNPROCESSABLE_ENTITY,
        AuthorizeError::UserLookup(_) => StatusCode::INTERNAL_SERVER_ERROR,
    };
    (status, error.to_string())
}

fn jwks_status(error: JwksError) -> (StatusCode, String) {
    let status = match &error {
        // Authentik unreachable is an upstream failure, not the caller's fault.
        JwksError::Fetch(_) => StatusCode::BAD_GATEWAY,
        _ => StatusCode::UNAUTHORIZED,
    };
    (status, error.to_string())
}
