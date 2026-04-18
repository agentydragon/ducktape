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

    let approved_argv = match call_daemon(&sock_path, &report).await {
        Ok(ShimResponse::Blocked { message }) => {
            eprintln!("[{name}-shim] BLOCKED: {message}");
            std::process::exit(1);
        }
        Ok(ShimResponse::Execve { argv }) => argv,
        Err(e) => {
            eprintln!("[{name}-shim] daemon unreachable: {e} — passing through");
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
