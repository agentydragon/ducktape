//! Command execution for `hostexecd`: run `req.cmd` as a bash script (`bash -c cmd` — full shell
//! semantics, not an argv vector), enforce a wall-clock timeout that kills the process, and cap
//! captured stdout/stderr while still counting the total bytes so truncation is reported honestly.
//!
//! The result shape mirrors `mcp_infra.exec.models.BaseExecResult` (the shape every exec backend
//! in the repo returns) so the console-side caller deserializes it directly: `exit` is a
//! `kind`-tagged union (`exited`/`killed`/`timed_out`); each stream is either a bare string or a
//! `{truncated_text, total_bytes}` object. Privilege-drop to the target user is layered on
//! separately (it needs root and is not part of this module).

use std::io;
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::process::{ExitStatus as ProcessExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde::Serialize;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::task::JoinHandle;
use users::Credentials;

/// How the process ended (mirrors the Python `ExitStatus` discriminated union).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ExitStatus {
    TimedOut,
    Exited { exit_code: i32 },
    Killed { signal: i32 },
}

/// Captured stream output: a full string, or a truncated prefix with the original size (mirrors
/// the Python `str | TruncatedStream`). Untagged: serializes as a bare string or the object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum OutputStream {
    Full(String),
    Truncated {
        truncated_text: String,
        total_bytes: usize,
    },
}

/// The exec result (mirrors `mcp_infra.exec.models.BaseExecResult`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ExecResult {
    pub exit: ExitStatus,
    pub stdout: OutputStream,
    pub stderr: OutputStream,
    pub duration_ms: u64,
}

/// What to run: bash script text, executed as `bash -c cmd`.
#[derive(Debug, Clone)]
pub struct ExecRequest {
    pub cmd: String,
    pub cwd: Option<PathBuf>,
    pub timeout: Duration,
    pub max_bytes: usize,
    /// Drop to these credentials for the child (`hostexecd` runs as root). `None` runs as the
    /// current user. The drop is a full one — supplementary groups, then gid, then uid — via a
    /// `pre_exec` hook. It requires root, so it is validated on a host, not the non-root unit tests.
    pub credentials: Option<Credentials>,
}

/// After the process is killed on timeout, how long to keep draining the pipes before giving up
/// (an orphaned grandchild holding a pipe open must not hang the server).
const DRAIN_GRACE: Duration = Duration::from_secs(5);

/// Spawn `bash -c req.cmd`, capture capped stdout/stderr, enforce the timeout, and return the
/// outcome. Errors only on spawn/IO failure; a non-zero exit or a timeout is a normal `ExecResult`.
pub async fn run_command(req: &ExecRequest) -> io::Result<ExecResult> {
    let start = Instant::now();
    let mut command = Command::new("bash");
    command
        .arg("-c")
        .arg(&req.cmd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(creds) = &req.credentials {
        // The privilege drop below switches *identity*; without this the child would keep the
        // daemon's environment, so a command running as `alice` would see root's `$HOME` (or none
        // at all, which makes `set -u` scripts die on `HOME: unbound variable`). Set the same four
        // variables `su`, `runuser` and systemd's `User=` do, all from the passwd entry of the
        // account being dropped to — no new privilege, and no daemon state leaking in.
        command
            .env("HOME", &creds.home)
            .env("USER", &creds.name)
            .env("LOGNAME", &creds.name)
            .env("SHELL", &creds.shell);
    }
    // Default to the target account's home rather than whatever directory the daemon happens to be
    // in, for the same reason.
    match (&req.cwd, &req.credentials) {
        (Some(cwd), _) => {
            command.current_dir(cwd);
        }
        (None, Some(creds)) => {
            command.current_dir(&creds.home);
        }
        (None, None) => {}
    }
    if let Some(creds) = &req.credentials {
        let (uid, gid, groups) = (creds.uid, creds.gid, creds.groups.clone());
        // SAFETY: runs in the child after fork, before exec — only async-signal-safe syscalls.
        // Full privilege drop: supplementary groups, then gid, then uid. Order matters — after
        // setuid the process no longer has the privilege to set groups/gid. A failure returns an
        // error, so a drop that cannot complete fails the spawn (never runs with the wrong uid).
        unsafe {
            command.pre_exec(move || {
                if libc::setgroups(groups.len(), groups.as_ptr()) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if libc::setgid(gid) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if libc::setuid(uid) != 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    let mut child = command.spawn()?;
    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    // Read both streams concurrently with waiting, so a full pipe can't deadlock the process.
    let out_task = tokio::spawn(read_capped(stdout, req.max_bytes));
    let err_task = tokio::spawn(read_capped(stderr, req.max_bytes));

    let (exit, out, err) = match tokio::time::timeout(req.timeout, child.wait()).await {
        Ok(status) => {
            let status = status?;
            (
                exit_status_of(status),
                finish_reader(out_task).await?,
                finish_reader(err_task).await?,
            )
        }
        Err(_elapsed) => {
            let _ = child.start_kill();
            let _ = child.wait().await;
            // Best-effort drain of whatever the readers captured before the pipes closed.
            (
                ExitStatus::TimedOut,
                drain_reader(out_task).await,
                drain_reader(err_task).await,
            )
        }
    };

    Ok(ExecResult {
        exit,
        stdout: render_stream(out, req.max_bytes),
        stderr: render_stream(err, req.max_bytes),
        duration_ms: start.elapsed().as_millis() as u64,
    })
}

fn exit_status_of(status: ProcessExitStatus) -> ExitStatus {
    if let Some(exit_code) = status.code() {
        ExitStatus::Exited { exit_code }
    } else if let Some(signal) = status.signal() {
        ExitStatus::Killed { signal }
    } else {
        ExitStatus::Exited { exit_code: 0 }
    }
}

/// Read to EOF, storing at most `cap` bytes but counting the true total (so truncation is exact).
async fn read_capped<R: AsyncRead + Unpin + Send + 'static>(
    mut reader: R,
    cap: usize,
) -> io::Result<(Vec<u8>, usize)> {
    let mut stored = Vec::new();
    let mut total = 0usize;
    let mut buf = [0u8; 8192];
    loop {
        let n = reader.read(&mut buf).await?;
        if n == 0 {
            break;
        }
        total += n;
        if stored.len() < cap {
            let take = (cap - stored.len()).min(n);
            stored.extend_from_slice(&buf[..take]);
        }
    }
    Ok((stored, total))
}

async fn finish_reader(
    task: JoinHandle<io::Result<(Vec<u8>, usize)>>,
) -> io::Result<(Vec<u8>, usize)> {
    match task.await {
        Ok(result) => result,
        Err(join_err) => Err(io::Error::other(join_err)),
    }
}

/// Timeout path: give the reader `DRAIN_GRACE` to finish (the kill should close the pipes), then
/// abort it so a grandchild holding the pipe open cannot stall the server. Best-effort — a failed
/// or aborted read yields empty output for the (already failed) timed-out command.
async fn drain_reader(mut task: JoinHandle<io::Result<(Vec<u8>, usize)>>) -> (Vec<u8>, usize) {
    tokio::select! {
        joined = &mut task => match joined {
            Ok(Ok(captured)) => captured,
            _ => (Vec::new(), 0),
        },
        _ = tokio::time::sleep(DRAIN_GRACE) => {
            task.abort();
            (Vec::new(), 0)
        }
    }
}

fn render_stream((stored, total): (Vec<u8>, usize), cap: usize) -> OutputStream {
    if cap == 0 || total == 0 {
        return OutputStream::Full(String::new());
    }
    // `stored` is already capped at `cap`, so total > cap means it was truncated.
    if total <= cap {
        OutputStream::Full(String::from_utf8_lossy(&stored).into_owned())
    } else {
        OutputStream::Truncated {
            truncated_text: String::from_utf8_lossy(&stored).into_owned(),
            total_bytes: total,
        }
    }
}
