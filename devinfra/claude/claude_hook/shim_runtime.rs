//! `claude-hook shim <name> [args...]` runtime.
//!
//! The shim sends a `ShimExecRequest` to the daemon's `/shim-exec` endpoint.
//! The daemon resolves the real binary, applies policy (git block, bazelrc
//! injection), and returns either `Blocked` (shim prints message, exits 1)
//! or `Execve` with a fully resolved argv (shim just exec's it).
//!
//! Diverges from `devinfra/claude/hook_daemon/shim.py` on the daemon-unreachable
//! path. `shim.py` falls straight through to the original argv (`shim.py:67`);
//! never spawns a daemon from the shim. This runtime instead calls
//! `daemon_lifecycle::ensure_daemon` once via `decide_with_recovery` so a
//! transiently-dead daemon recovers in-process for the very next RPC. The
//! `startup_failure.json` circuit breaker keeps that recovery cheap when the
//! daemon panics deterministically: after a couple of failures `ensure_daemon`
//! short-circuits with the same cooldown (and the same on-disk format) Python's
//! hook-dispatch path uses (see `daemon_lifecycle.rs`).

use std::collections::HashMap;
use std::future::Future;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use claude_hook_shim_install::SHIM_SESSION_ID_ENV;
use protocol::{ShimExecRequest, ShimResponse};

/// Whole-`run_shim` wall-clock ceiling. Covers: 2s connect + up to 10s
/// `wait_for_sock` + 2s retry connect + slack. If the deadlock is inside
/// the tokio runtime itself, this timer won't fire — that's a separate
/// belt-and-braces story (`setitimer(SIGALRM)`) intentionally out of scope.
const SHIM_BODY_TIMEOUT: Duration = Duration::from_secs(20);

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

/// Ask the daemon how to handle this shim invocation, with no recovery
/// (see `decide_with_recovery` for the respawn-and-retry wrapper that
/// `run_shim` uses). Kept for unit tests that exercise the pure
/// request → decision path without faking `ensure_daemon`.
#[cfg(test)]
async fn decide(sock: &Path, req: &ShimExecRequest, original_argv: Vec<String>) -> ShimDecision {
    response_to_decision(call_daemon(sock, req).await, original_argv)
}

/// Why `call_daemon` failed. `Unreachable` means the shim's connect to
/// the daemon's UDS failed fast or timed out — daemon is dead, restart
/// candidate. `Other` means the daemon answered but the answer was
/// unusable (handshake error, HTTP 5xx, malformed body): don't respawn,
/// just passthrough. The inner `String` is for user-facing display.
#[derive(Debug)]
enum CallError {
    Unreachable(String),
    Other(String),
}

impl CallError {
    fn into_reason(self) -> String {
        match self {
            CallError::Unreachable(r) | CallError::Other(r) => r,
        }
    }
}

/// Boundary classifier: the contract with `main.rs::post_json_over_uds_inner`
/// is that every connect-failure reason starts with `"connect:"` (both the
/// fast `ECONNREFUSED`/`ENOENT` case and the new connect-timeout case).
/// Everything else means the daemon answered.
fn classify_rpc_error(reason: String) -> CallError {
    if reason.starts_with("connect:") {
        CallError::Unreachable(reason)
    } else {
        CallError::Other(reason)
    }
}

fn response_to_decision(
    response: Result<ShimResponse, CallError>,
    original_argv: Vec<String>,
) -> ShimDecision {
    match response {
        Ok(ShimResponse::Blocked { message }) => ShimDecision::Block(message),
        Ok(ShimResponse::Execve { argv }) => ShimDecision::Exec(argv),
        Err(e) => ShimDecision::Passthrough {
            argv: original_argv,
            reason: e.into_reason(),
        },
    }
}

/// `decide` + at-most-one retry after `ensure_daemon` if the first attempt
/// looks like the daemon is unreachable. The `ensure_daemon` closure is
/// injectable so tests can stub it without forking a real daemon.
pub(crate) async fn decide_with_recovery<F, Fut>(
    sock: &Path,
    req: &ShimExecRequest,
    original_argv: Vec<String>,
    ensure_daemon: F,
) -> ShimDecision
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = Result<(), String>>,
{
    let first = call_daemon(sock, req).await;
    let first_unreachable = match first {
        Err(CallError::Unreachable(reason)) => reason,
        other => return response_to_decision(other, original_argv),
    };
    match ensure_daemon().await {
        Ok(()) => response_to_decision(call_daemon(sock, req).await, original_argv),
        Err(e) => ShimDecision::Passthrough {
            argv: original_argv,
            reason: format!("{first_unreachable}; ensure_daemon: {e}"),
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
    let daemon_dir = crate::daemon_dir_path(&session_id);
    let original_argv = argv.clone();

    let decision_fut = decide_with_recovery(&sock_path, &report, argv, || async {
        crate::daemon_lifecycle::ensure_daemon(&sock_path, &daemon_dir).await
    });

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

async fn call_daemon(sock: &Path, req: &ShimExecRequest) -> Result<ShimResponse, CallError> {
    let body = serde_json::to_vec(req).map_err(|e| CallError::Other(format!("serialize: {e}")))?;
    let resp_bytes = crate::post_json_over_uds(sock, "/shim-exec", body)
        .await
        .map_err(classify_rpc_error)?;
    serde_json::from_slice(&resp_bytes)
        .map_err(|e| CallError::Other(format!("parse response: {e}")))
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

    fn assert_unreachable(reason: &str) {
        match classify_rpc_error(reason.into()) {
            CallError::Unreachable(_) => {}
            CallError::Other(r) => panic!("expected Unreachable for {reason:?}, got Other({r})"),
        }
    }

    fn assert_other(reason: &str) {
        match classify_rpc_error(reason.into()) {
            CallError::Other(_) => {}
            CallError::Unreachable(r) => {
                panic!("expected Other for {reason:?}, got Unreachable({r})")
            }
        }
    }

    #[test]
    fn classify_connect_errors_as_unreachable() {
        assert_unreachable("connect: No such file or directory (os error 2)");
        assert_unreachable("connect: Connection refused (os error 111)");
        assert_unreachable("connect: timed out after 2s");
    }

    #[test]
    fn classify_post_connect_errors_as_other() {
        // Daemon answered the connect — these are not "respawn me" signals.
        assert_other("http1 handshake: invalid frame");
        assert_other("send request: closed");
        assert_other("daemon returned HTTP 500");
        assert_other("parse response: missing field");
        assert_other("serialize: cycle");
        assert_other("daemon request timed out (300s)");
    }

    /// When `decide` returns Passthrough with a "connect:" reason, the
    /// recovery wrapper calls `ensure_daemon` exactly once and retries.
    #[tokio::test]
    async fn decide_with_recovery_invokes_ensure_on_unreachable() {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};

        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("nonexistent.sock");
        let req = git_status_request();
        let original = req.argv.clone();

        let calls = Arc::new(AtomicUsize::new(0));
        let calls2 = calls.clone();
        let decision = decide_with_recovery(&sock, &req, original.clone(), move || {
            let calls = calls2.clone();
            async move {
                calls.fetch_add(1, Ordering::SeqCst);
                // Stub: pretend respawn failed so we don't need a real daemon.
                Err::<(), String>("test stub: no real daemon".into())
            }
        })
        .await;

        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "ensure_daemon must be called exactly once"
        );
        match decision {
            ShimDecision::Passthrough { argv, reason } => {
                assert_eq!(argv, original);
                assert!(
                    reason.contains("ensure_daemon: test stub"),
                    "ensure_daemon error must surface in passthrough reason, got: {reason}"
                );
            }
            other => panic!("expected Passthrough, got {other:?}"),
        }
    }

    /// When the daemon is reachable but returns Blocked, the recovery wrapper
    /// must NOT call `ensure_daemon` and must return the Block verbatim.
    #[tokio::test]
    async fn decide_with_recovery_no_respawn_when_daemon_answers() {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};

        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("d.sock");
        let _server = spawn_fake_daemon(
            sock.clone(),
            ShimResponse::Blocked {
                message: "denied".into(),
            },
        )
        .await;
        let req = git_status_request();

        let calls = Arc::new(AtomicUsize::new(0));
        let calls2 = calls.clone();
        let decision = decide_with_recovery(&sock, &req, req.argv.clone(), move || {
            let calls = calls2.clone();
            async move {
                calls.fetch_add(1, Ordering::SeqCst);
                Ok::<(), String>(())
            }
        })
        .await;

        assert_eq!(
            calls.load(Ordering::SeqCst),
            0,
            "ensure_daemon must not be called when the daemon answered"
        );
        match decision {
            ShimDecision::Block(m) => assert_eq!(m, "denied"),
            other => panic!("expected Block, got {other:?}"),
        }
    }

    /// If `ensure_daemon` succeeds, the wrapper retries `decide` against the
    /// now-live socket and surfaces whatever the daemon says.
    #[tokio::test]
    async fn decide_with_recovery_retries_after_ensure_succeeds() {
        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("d.sock");
        let req = git_status_request();
        let original = req.argv.clone();

        // Don't bind the socket up front — the first decide() call must see
        // connect failure. The "ensure_daemon" stub spawns the fake daemon,
        // simulating the real respawn behavior. The retry then sees the live
        // daemon and gets an Exec response.
        let sock_for_stub = sock.clone();
        let decision = decide_with_recovery(&sock, &req, original.clone(), move || async move {
            spawn_fake_daemon(
                sock_for_stub,
                ShimResponse::Execve {
                    argv: vec!["/usr/bin/git".into(), "status".into()],
                },
            )
            .await;
            // Wait briefly for axum to start accepting; without this, the
            // retry can race the listener.
            tokio::time::sleep(Duration::from_millis(50)).await;
            Ok::<(), String>(())
        })
        .await;

        match decision {
            ShimDecision::Exec(argv) => {
                assert_eq!(argv, vec!["/usr/bin/git".to_string(), "status".into()]);
            }
            other => panic!("expected Exec after respawn, got {other:?}"),
        }
    }
}
