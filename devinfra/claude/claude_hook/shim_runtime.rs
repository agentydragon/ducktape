//! `claude-hook shim <name> [args...]` runtime.
//!
//! The shim sends a `ShimExecRequest` to the daemon's `/shim-exec` endpoint.
//! The daemon resolves the real binary, applies policy (git block, bazelrc
//! injection), and returns either `Blocked` (shim prints message, exits 1)
//! or `Execve` with a fully resolved argv (shim just exec's it).

use std::collections::HashMap;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};

use claude_hook_shim_install::SHIM_SESSION_ID_ENV;
use protocol::{ShimExecRequest, ShimResponse};

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
    /// Daemon unreachable — fall back to execing the original argv.
    Passthrough(Vec<String>),
}

/// Ask the daemon how to handle this shim invocation. Turns RPC failures
/// into `Passthrough(original_argv)` so `run_shim` doesn't need to know
/// anything about the daemon wire protocol.
pub(crate) async fn decide(
    sock: &Path,
    req: &ShimExecRequest,
    original_argv: Vec<String>,
) -> ShimDecision {
    match call_daemon(sock, req).await {
        Ok(ShimResponse::Blocked { message }) => ShimDecision::Block(message),
        Ok(ShimResponse::Execve { argv }) => ShimDecision::Exec(argv),
        Err(e) => {
            // Log the specific RPC error before we lose it — run_shim just
            // prints a generic "passing through" to the user.
            eprintln!("[{}-shim] daemon call failed: {e}", req.shim);
            ShimDecision::Passthrough(original_argv)
        }
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

    let approved_argv = match decide(&sock_path, &report, argv).await {
        ShimDecision::Block(message) => {
            eprintln!("[{name}-shim] BLOCKED: {message}");
            std::process::exit(1);
        }
        ShimDecision::Exec(argv) => argv,
        ShimDecision::Passthrough(argv) => {
            eprintln!("[{name}-shim] daemon unreachable — passing through");
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
    use axum::Json;
    use axum::routing::post;
    use std::sync::Arc;
    use tokio::net::UnixListener;

    fn sample_request() -> ShimExecRequest {
        let mut env = HashMap::new();
        env.insert("PATH".into(), "/usr/bin".into());
        ShimExecRequest {
            shim: "git".into(),
            session_id: "session-test".into(),
            cwd: PathBuf::from("/tmp"),
            argv: vec!["git".into(), "status".into()],
            pid: 42,
            env,
        }
    }

    /// Spawn a one-shot fake daemon on a temp UDS that answers every request
    /// with `response`. The join handle is dropped when the test ends, which
    /// aborts the server task — but the socket path is already cleaned up by
    /// `tempdir()`. Returns the socket path.
    async fn spawn_fake_daemon(
        sock: PathBuf,
        response: ShimResponse,
    ) -> tokio::task::JoinHandle<()> {
        let shared = Arc::new(response);
        let app = axum::Router::new().route(
            "/shim-exec",
            post({
                let shared = shared.clone();
                move || {
                    let shared = shared.clone();
                    async move {
                        // axum's Json needs `Serialize`; ShimResponse is. Clone
                        // the shared value into a fresh owned one for the
                        // response (`Arc::clone` would hand out a shared ref,
                        // but Json takes ownership).
                        let resp: ShimResponse = match &*shared {
                            ShimResponse::Blocked { message } => ShimResponse::Blocked {
                                message: message.clone(),
                            },
                            ShimResponse::Execve { argv } => {
                                ShimResponse::Execve { argv: argv.clone() }
                            }
                        };
                        Json(resp)
                    }
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
        let req = sample_request();
        let decision = decide(&sock, &req, req.argv.clone()).await;
        match decision {
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
        let req = sample_request();
        let decision = decide(&sock, &req, req.argv.clone()).await;
        match decision {
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
        let req = sample_request();
        let original = req.argv.clone();
        let decision = decide(&sock, &req, original.clone()).await;
        match decision {
            ShimDecision::Passthrough(argv) => assert_eq!(argv, original),
            other => panic!("expected Passthrough, got {other:?}"),
        }
    }
}
