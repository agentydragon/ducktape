//! `hostexecd`: outbound node execution daemon for Haku Console.
//!
//! The daemon has no listener. It authenticates to the console with a per-node routing bearer,
//! heartbeats, long-polls for approved work, renews a lease during execution, and returns the
//! result. Every command still carries the approving operator's short-lived Authentik token;
//! `authorize` remains the fail-closed execution boundary.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow};
use authorize::authorize;
use clap::Parser;
use exec::{ExecRequest, ExecResult, run_command};
use jwks::Jwks;
use log::{info, warn};
use replay::ReplayStore;
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use tokio::time::{MissedTickBehavior, interval, sleep};
use uuid::Uuid;

#[derive(Debug, Parser)]
#[command(about = "Haku outbound node execution daemon")]
struct Config {
    #[arg(long, env = "HOSTEXEC_HOST")]
    host: String,
    #[arg(long, env = "HOSTEXEC_ISSUER")]
    issuer: String,
    #[arg(long, env = "HOSTEXEC_JWKS_URL")]
    jwks_url: String,
    #[arg(long, env = "HOSTEXEC_CONSOLE_URL")]
    console_url: String,
    #[arg(long, env = "HOSTEXEC_DAEMON_TOKEN_FILE")]
    daemon_token_file: PathBuf,
    #[arg(long, env = "HOSTEXEC_VERSION", default_value = env!("CARGO_PKG_VERSION"))]
    version: String,
}

struct App {
    config: Config,
    daemon_token: String,
    client: Client,
    instance_id: Uuid,
    jwks: Jwks,
    replay: ReplayStore,
}

#[derive(Serialize)]
struct HeartbeatRequest<'a> {
    instance_id: String,
    version: &'a str,
    backends: [&'static str; 1],
    capacity: u8,
}

#[derive(Deserialize)]
struct HeartbeatResponse {
    heartbeat_interval_seconds: u64,
    lease_seconds: u64,
}

#[derive(Serialize)]
struct ClaimRequest {
    instance_id: String,
    wait_seconds: u8,
}

#[derive(Deserialize)]
struct ClaimedExecution {
    execution_id: String,
    backend: String,
    payload: RunRequest,
    lease_token: String,
    lease_expires_at: String,
}

struct ExecutionLease {
    execution_id: String,
    lease_token: String,
}

#[derive(Deserialize)]
struct RunRequest {
    token: String,
    run_as: String,
    argv: Vec<String>,
    cwd: Option<String>,
    max_bytes: usize,
    timeout_ms: u64,
}

#[derive(Serialize)]
struct LeaseRequest<'a> {
    instance_id: String,
    lease_token: &'a str,
}

#[derive(Serialize)]
struct ResultRequest<'a> {
    instance_id: String,
    lease_token: &'a str,
    outcome: &'static str,
    result: Option<&'a ExecResult>,
    error: Option<&'a str>,
}

impl App {
    fn request(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        self.client
            .request(
                method,
                format!("{}{path}", self.config.console_url.trim_end_matches('/')),
            )
            .bearer_auth(&self.daemon_token)
    }

    async fn heartbeat(&self) -> Result<HeartbeatResponse> {
        self.request(reqwest::Method::POST, "/api/node-daemons/v1/heartbeat")
            .json(&HeartbeatRequest {
                instance_id: self.instance_id.to_string(),
                version: &self.config.version,
                backends: ["hostexec"],
                capacity: 1,
            })
            .send()
            .await?
            .error_for_status()?
            .json()
            .await
            .context("decode heartbeat response")
    }

    async fn claim(&self) -> Result<Option<ClaimedExecution>> {
        let response = self
            .request(reqwest::Method::POST, "/api/node-daemons/v1/work/claim")
            .json(&ClaimRequest {
                instance_id: self.instance_id.to_string(),
                wait_seconds: 20,
            })
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        Ok(Some(response.error_for_status()?.json().await?))
    }

    async fn renew(&self, lease: &ExecutionLease) -> Result<()> {
        self.request(
            reqwest::Method::POST,
            &format!(
                "/api/node-daemons/v1/executions/{}/heartbeat",
                lease.execution_id
            ),
        )
        .json(&LeaseRequest {
            instance_id: self.instance_id.to_string(),
            lease_token: &lease.lease_token,
        })
        .send()
        .await?
        .error_for_status()?;
        Ok(())
    }

    async fn submit_result(
        &self,
        lease: &ExecutionLease,
        result: Result<&ExecResult, &str>,
    ) -> Result<()> {
        let body = match result {
            Ok(result) => ResultRequest {
                instance_id: self.instance_id.to_string(),
                lease_token: &lease.lease_token,
                outcome: "succeeded",
                result: Some(result),
                error: None,
            },
            Err(error) => ResultRequest {
                instance_id: self.instance_id.to_string(),
                lease_token: &lease.lease_token,
                outcome: "failed",
                result: None,
                error: Some(error),
            },
        };
        self.request(
            reqwest::Method::POST,
            &format!(
                "/api/node-daemons/v1/executions/{}/result",
                lease.execution_id
            ),
        )
        .json(&body)
        .send()
        .await?
        .error_for_status()?;
        Ok(())
    }

    async fn execute(&self, request: RunRequest) -> Result<ExecResult> {
        let header =
            jsonwebtoken::decode_header(&request.token).context("malformed operator token")?;
        let kid = header.kid.context("operator token has no kid")?;
        let key = self.jwks.key_for(&kid).await?;
        let authorized = authorize(
            &request.token,
            &key,
            &self.config.issuer,
            &self.config.host,
            &request.run_as,
            &self.replay,
            now_unix(),
        )?;
        let argv0 = request.argv.first().cloned();
        info!(
            "exec approved by {} host={} run_as={} argv0={argv0:?}",
            authorized.subject, self.config.host, request.run_as
        );
        let result = run_command(&ExecRequest {
            argv: request.argv,
            cwd: request.cwd.map(Into::into),
            timeout: Duration::from_millis(request.timeout_ms),
            max_bytes: request.max_bytes,
            credentials: Some(authorized.credentials),
        })
        .await?;
        info!(
            "exec done host={} run_as={} argv0={argv0:?} exit={:?} duration_ms={}",
            self.config.host, request.run_as, result.exit, result.duration_ms
        );
        Ok(result)
    }

    async fn run_claim(&self, claim: ClaimedExecution, policy: &HeartbeatResponse) -> Result<()> {
        let ClaimedExecution {
            execution_id,
            backend,
            payload,
            lease_token,
            lease_expires_at,
        } = claim;
        if backend != "hostexec" {
            return Err(anyhow!("unsupported backend {backend}"));
        }
        let lease = ExecutionLease {
            execution_id,
            lease_token,
        };
        info!(
            "claimed execution {} lease_expires_at={}",
            lease.execution_id, lease_expires_at
        );
        let execution = self.execute(payload);
        tokio::pin!(execution);
        let mut renewals = interval(Duration::from_secs(
            policy
                .heartbeat_interval_seconds
                .min((policy.lease_seconds / 3).max(2)),
        ));
        renewals.set_missed_tick_behavior(MissedTickBehavior::Delay);
        renewals.tick().await;
        loop {
            tokio::select! {
                result = &mut execution => {
                    match result {
                        Ok(result) => self.submit_result(&lease, Ok(&result)).await?,
                        Err(error) => {
                            let message = error.to_string();
                            self.submit_result(&lease, Err(&message)).await?;
                        }
                    }
                    return Ok(());
                }
                _ = renewals.tick() => {
                    self.heartbeat().await?;
                    self.renew(&lease).await?;
                },
            }
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::init();
    let config = Config::parse();
    let daemon_token = std::fs::read_to_string(&config.daemon_token_file)
        .with_context(|| format!("read {}", config.daemon_token_file.display()))?
        .trim()
        .to_owned();
    if daemon_token.is_empty() {
        return Err(anyhow!("daemon token file is empty"));
    }
    let app = Arc::new(App {
        jwks: Jwks::new(config.jwks_url.clone()),
        config,
        daemon_token,
        client: Client::builder().timeout(Duration::from_secs(30)).build()?,
        instance_id: Uuid::new_v4(),
        replay: ReplayStore::new(),
    });
    info!(
        "hostexecd starting outbound session instance={}",
        app.instance_id
    );

    let mut backoff = 1_u64;
    loop {
        match app.heartbeat().await {
            Ok(policy) => match app.claim().await {
                Ok(Some(claim)) => {
                    if let Err(error) = app.run_claim(claim, &policy).await {
                        warn!("execution channel failed: {error:#}");
                    }
                    backoff = 1;
                }
                Ok(None) => backoff = 1,
                Err(error) => {
                    warn!("work claim failed: {error:#}; retrying in {backoff}s");
                    sleep(Duration::from_secs(backoff)).await;
                    backoff = (backoff * 2).min(60);
                }
            },
            Err(error) => {
                warn!("heartbeat failed: {error:#}; retrying in {backoff}s");
                sleep(Duration::from_secs(backoff)).await;
                backoff = (backoff * 2).min(60);
            }
        }
    }
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before 1970")
        .as_secs()
}
