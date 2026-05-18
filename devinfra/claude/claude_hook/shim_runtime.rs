//! `claude-hook shim <name> [args...]` runtime.
//!
//! The shim sends a `ShimExecRequest` to the daemon's `/shim-exec` endpoint.
//! The daemon resolves the real binary, applies policy (git block, bazelrc
//! injection), and returns either `Blocked` (shim prints message, exits 1)
//! or `Execve` with a fully resolved argv (shim just exec's it).
//!
//! Mirrors `devinfra/claude/hook_daemon/shim.py`: the shim **never spawns**
//! the daemon. Shims fire from inside tool invocations, after Claude Code's
//! `PreToolUse` hook has already run — which is the event that owns daemon
//! startup (`main.rs::dispatch_hook` → `daemon_lifecycle::ensure_daemon`). If
//! the daemon is unreachable from the shim, treat it as a real outage and
//! pass straight through to the original argv; another attempt from the shim
//! path won't make things better and risks spinning while the user's command
//! waits. Daemon lifecycle (kill-stale, fork, circuit breaker, etc.) lives
//! in `daemon_lifecycle.rs`.

use std::collections::HashMap;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use claude_hook_shim_install::SHIM_SESSION_ID_ENV;
use protocol::{ShimExecRequest, ShimResponse};

/// Whole-`run_shim` wall-clock ceiling. The body is one RPC (2s connect
/// deadline + parse), so under normal conditions it completes in well under
/// a second; this ceiling is a safety net against a daemon that accepted the
/// connection then never replied. If the deadlock is inside the tokio runtime
/// itself, this timer won't fire — that's a separate belt-and-braces story
/// (`setitimer(SIGALRM)`) intentionally out of scope.
const SHIM_BODY_TIMEOUT: Duration = Duration::from_secs(5);

/// The decision the shim is going to take, derived from the daemon's
/// response (or absence thereof). Split out of `run_shim` so it can be
/// tested without actually `exec`ing.
#[derive(Debug)]
pub(crate) enum ShimDecision {
    /// Daemon said to block: print `message` to stderr and exit 1.
    Block(String),
    /// Daemon returned an approved argv (argv[0] is an absolute path the
    /// daemon has already resolved) — exec it.
    Exec(Vec<String>),
    /// Daemon unreachable — exec the original argv. `reason` carries the
    /// RPC error so `run_shim` can surface a single user-facing message.
    Passthrough { argv: Vec<String>, reason: String },
}

/// Ask the daemon how to handle this shim invocation. On any RPC failure —
/// daemon unreachable, malformed response, HTTP 5xx — return Passthrough so
/// the shim execs the original argv. The shim does not attempt to spawn or
/// respawn the daemon; daemon lifecycle is the hook-dispatch path's job.
async fn decide(sock: &Path, req: &ShimExecRequest, original_argv: Vec<String>) -> ShimDecision {
    match call_daemon(sock, req).await {
        Ok(ShimResponse::Blocked { message }) => ShimDecision::Block(message),
        Ok(ShimResponse::Execve { argv }) => ShimDecision::Exec(argv),
        Err(reason) => ShimDecision::Passthrough {
            argv: original_argv,
            reason,
        },
    }
}

pub async fn run_shim(name: String, forwarded: Vec<String>) -> ! {
    let session_id = std::env::var(SHIM_SESSION_ID_ENV).unwrap_or_else(|_| {
        eprintln!("claude-hook shim: {SHIM_SESSION_ID_ENV} not set in env — shim wrapper broken?");
        std::process::exit(2);
    });

    let mut argv = vec![name.clone()];
    argv.extend(forwarded);

    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
    let env: HashMap<String, String> = std::env::vars().collect();
    let pid = std::process::id();

    let report = ShimExecRequest {
        shim: name.clone(),
        session_id: session_id.clone(),
        cwd,
        argv: argv.clone(),
        pid,
        env,
    };

    let sock_path = crate::daemon_sock_path(&session_id);
    let original_argv = argv.clone();

    let decision_fut = decide(&sock_path, &report, argv);

    let decision = match tokio::time::timeout(SHIM_BODY_TIMEOUT, decision_fut).await {
        Ok(d) => d,
        Err(_) => {
            eprintln!(
                "[{name}-shim] watchdog: shim body exceeded {}s — passing through",
                SHIM_BODY_TIMEOUT.as_secs()
            );
            ShimDecision::Passthrough {
                argv: original_argv,
                reason: format!("watchdog timeout ({}s)", SHIM_BODY_TIMEOUT.as_secs()),
            }
        }
    };

    let approved_argv = match decision {
        ShimDecision::Block(message) => {
            eprintln!("[{name}-shim] BLOCKED: {message}");
            std::process::exit(1);
        }
        ShimDecision::Exec(argv) => argv,
        ShimDecision::Passthrough { argv, reason } => {
            eprintln!("[{name}-shim] daemon unreachable: {reason} — passing through");
            argv
        }
    };

    let err = std::process::Command::new(&approved_argv[0])
        .args(&approved_argv[1..])
        .exec();
    eprintln!("{name}: exec failed: {err}");
    std::process::exit(126);
}

async fn call_daemon(sock: &Path, req: &ShimExecRequest) -> Result<ShimResponse, String> {
    let body = serde_json::to_vec(req).map_err(|e| format!("serialize: {e}"))?;
    let resp_bytes = crate::post_json_over_uds(sock, "/shim-exec", body).await?;
    serde_json::from_slice(&resp_bytes).map_err(|e| format!("parse response: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::make_request;
    use axum::Json;
    use axum::routing::post;
    use std::sync::Arc;
    use tokio::net::UnixListener;

    /// Spawn a fake daemon on `sock` that answers every `/shim-exec` request
    /// with `response`. Returns the server task's `JoinHandle`. The task
    /// runs until `handle.abort()` is called or the tokio runtime ends with
    /// the test; dropping the handle alone does NOT abort it.
    async fn spawn_fake_daemon(
        sock: PathBuf,
        response: ShimResponse,
    ) -> tokio::task::JoinHandle<()> {
        let shared = Arc::new(response);
        let app = axum::Router::new().route(
            "/shim-exec",
            post(move || {
                let shared = shared.clone();
                async move {
                    // Clone inner fields — ShimResponse isn't Clone, and the
                    // Arc holds a shared ref while Json wants ownership.
                    let resp = match &*shared {
                        ShimResponse::Blocked { message } => ShimResponse::Blocked {
                            message: message.clone(),
                        },
                        ShimResponse::Execve { argv } => {
                            ShimResponse::Execve { argv: argv.clone() }
                        }
                    };
                    Json(resp)
                }
            }),
        );
        let listener = UnixListener::bind(&sock).unwrap();
        tokio::spawn(async move {
            if let Err(e) = axum::serve(listener, app).await {
                eprintln!("fake daemon serve error: {e}");
            }
        })
    }

    fn git_status_request() -> ShimExecRequest {
        make_request("git", &["git", "status"], "/usr/bin")
    }

    #[tokio::test]
    async fn decide_blocks_on_blocked_response() {
        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("d.sock");
        let _server = spawn_fake_daemon(
            sock.clone(),
            ShimResponse::Blocked {
                message: "nope".into(),
            },
        )
        .await;
        let req = git_status_request();
        match decide(&sock, &req, req.argv.clone()).await {
            ShimDecision::Block(m) => assert_eq!(m, "nope"),
            other => panic!("expected Block, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn decide_execs_absolute_path_on_execve() {
        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("d.sock");
        let _server = spawn_fake_daemon(
            sock.clone(),
            ShimResponse::Execve {
                argv: vec!["/usr/bin/git".into(), "status".into()],
            },
        )
        .await;
        let req = git_status_request();
        match decide(&sock, &req, req.argv.clone()).await {
            ShimDecision::Exec(argv) => {
                assert!(
                    argv[0].starts_with('/'),
                    "argv[0] should be absolute, got {:?}",
                    argv[0]
                );
                assert_eq!(argv, vec!["/usr/bin/git".to_string(), "status".into()]);
            }
            other => panic!("expected Exec, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn decide_passthrough_when_daemon_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("nonexistent.sock");
        let req = git_status_request();
        let original = req.argv.clone();
        match decide(&sock, &req, original.clone()).await {
            ShimDecision::Passthrough { argv, reason } => {
                assert_eq!(argv, original);
                assert!(!reason.is_empty(), "reason should carry the RPC error");
            }
            other => panic!("expected Passthrough, got {other:?}"),
        }
    }
}
