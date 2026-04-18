//! Background command execution — ports
//! `devinfra/claude/hook_daemon/session_start/handler.py::_launch_background_command`.
//!
//! Each bg command spawns a `bash -c` subprocess with:
//!
//!   - `cwd = project_dir`
//!   - `env = os::environ + env_overlay + HOOK_DAEMON_SOCK=<sock>`
//!   - stdout/stderr piped and read line-by-line into the session's bg
//!     buffers, so a REPL hook can drain them without waiting for the
//!     process to finish
//!   - a watcher task that either reports completion (exit code) or
//!     timeout (kills the process, posts a message) to the session
//!     mailbox.
//!
//! `after_env: true` commands source the session env file first.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

use claude_hook_config::BackgroundCommand;

use crate::session::{BgStream, Session};

/// Spawn one background command and wire stdout/stderr → session buffers +
/// a watcher that reports completion / timeout via the mailbox.
pub fn launch(
    session: Arc<Session>,
    cmd: BackgroundCommand,
    sock_path: PathBuf,
    env_file_path: Option<PathBuf>,
    project_dir: PathBuf,
    env_overlay: HashMap<String, String>,
) {
    tokio::spawn(async move {
        session.post_message(format!("Task [{}] started.", cmd.name));

        let shell_cmd = if cmd.after_env {
            if let Some(envf) = env_file_path.as_ref() {
                format!(
                    "source {} && {}",
                    shlex::try_quote(envf.to_str().unwrap_or("")).unwrap(),
                    cmd.command
                )
            } else {
                cmd.command.clone()
            }
        } else {
            cmd.command.clone()
        };

        let mut proc = match spawn(&shell_cmd, &project_dir, &sock_path, &env_overlay) {
            Ok(p) => p,
            Err(e) => {
                session.post_message(format!("Task [{}] failed to spawn: {e}", cmd.name));
                return;
            }
        };

        let stdout = proc.stdout.take().expect("stdout piped");
        let stderr = proc.stderr.take().expect("stderr piped");

        let s_out = session.clone();
        let task_out = cmd.name.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                s_out.push_bg_line(&task_out, BgStream::Stdout, line);
            }
        });

        let s_err = session.clone();
        let task_err = cmd.name.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                s_err.push_bg_line(&task_err, BgStream::Stderr, line);
            }
        });

        match tokio::time::timeout(Duration::from_secs(cmd.timeout), proc.wait()).await {
            Ok(Ok(status)) => {
                let rc = status.code().unwrap_or(-1);
                session.post_message(format!("Task [{}] exited {rc}.", cmd.name));
            }
            Ok(Err(e)) => {
                session.post_message(format!("Task [{}] wait failed: {e}", cmd.name));
            }
            Err(_) => {
                if let Err(e) = proc.kill().await {
                    eprintln!("bg [{}]: kill failed: {e}", cmd.name);
                }
                session.post_message(format!(
                    "Task [{}] timed out after {}s.",
                    cmd.name, cmd.timeout
                ));
            }
        }
    });
}

fn spawn(
    shell_cmd: &str,
    project_dir: &Path,
    sock_path: &Path,
    env_overlay: &HashMap<String, String>,
) -> std::io::Result<tokio::process::Child> {
    let mut c = Command::new("bash");
    c.arg("-c")
        .arg(shell_cmd)
        .current_dir(project_dir)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .stdin(std::process::Stdio::null());

    // Inherit daemon env + overlay + HOOK_DAEMON_SOCK.
    for (k, v) in env_overlay {
        c.env(k, v);
    }
    c.env("HOOK_DAEMON_SOCK", sock_path);
    c.spawn()
}
