//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Per-process lifecycle management: spawn, wait, timeout, memory monitoring,
//! kill, and cleanup.
//!
//! Functions decompiled from:
//!   kill_and_wait:               0x1b5620..0x1b5b56  (1334 bytes)
//!   process_info_deser:          0x21c970..0x21cb45  (469 bytes)
//!   cgroup_config_deser:         0x21ce40..0x21d015  (469 bytes)
//!   proc_handle_deser:           0x21d120..0x21d303  (483 bytes)
//!   process_info_variant_deser:  0x21d560..0x21d732  (466 bytes)

use std::path::PathBuf;
use std::time::{Duration, Instant};

use nix::sys::signal::{self, Signal};
use nix::sys::wait::{waitpid, WaitPidFlag, WaitStatus};
use nix::unistd::Pid;
use tokio::sync::oneshot;

use crate::cgroup::{self, CgroupController, CgroupVersion};
use crate::pid_tree;

/// Exit status of a managed process.
/// Decompiled from 0x21d120..0x21d303 (483 bytes)
/// Xrefs: "killed_by_process_api"
#[derive(Debug, Clone)]
pub enum ExitReason {
    /// Process exited normally with a status code.
    Exited { status: i32 },
    /// Process was killed by a signal.
    Signaled {
        signal: i32,
        core_dumped: bool,
    },
    /// Process timed out and was killed by process_api.
    TimedOut { timeout_secs: u64 },
    /// Process exceeded its per-process memory limit.
    OutOfMemory { limit_bytes: u64 },
    /// Process was killed due to container-level OOM.
    ContainerOom { limit_bytes: u64 },
    /// Process was killed by process_api for another reason.
    KilledByProcessApi,
}

/// Per-process handle tracking its lifecycle.
/// Decompiled from struct layout at 0x21c970..0x21cb45 (469 bytes)
/// Xrefs: "reattachable", "timeout", "memory_limit_bytes"
#[derive(Debug)]
pub struct ProcHandle {
    pub pid: u32,
    pub process_group_pid: u32,
    pub reattachable: bool,
    pub timeout: Option<Duration>,
    pub memory_limit_bytes: Option<u64>,
    pub start_time: Instant,
    pub cgroup_path: Option<PathBuf>,
    pub killed_by_process_api: bool,
    /// Sender to signal that the process should stop waiting.
    pub stop_waiting_tx: Option<oneshot::Sender<()>>,
}

impl ProcHandle {
    pub fn new(
        pid: u32,
        reattachable: bool,
        timeout: Option<Duration>,
        memory_limit_bytes: Option<u64>,
    ) -> Self {
        Self {
            pid,
            process_group_pid: pid,
            reattachable,
            timeout,
            memory_limit_bytes,
            start_time: Instant::now(),
            cgroup_path: None,
            killed_by_process_api: false,
            stop_waiting_tx: None,
        }
    }
}

/// Decompiled from 0x1b5620..0x1b5b56  (1334 bytes)
/// Xrefs: kill(pid, SIGKILL), waitpid() loop with errno ECHILD handling
///
/// Kill a process and all its descendants, then wait for them to exit.
/// Sends SIGKILL to the process group, then loops waitpid() until ECHILD.
pub async fn kill_and_wait(pid: u32, cgroup_path: Option<&PathBuf>) {
    let nix_pid = Pid::from_raw(pid as i32);

    // Kill the process group
    log::debug!("[DEBUG] Killing process group for PID {pid}");
    if let Err(e) = signal::killpg(nix_pid, Signal::SIGKILL) {
        log::debug!("[DEBUG] killpg({pid}, SIGKILL) failed: {e}, trying kill");
        // Fall back to killing just the PID
        let _ = signal::kill(nix_pid, Signal::SIGKILL);
    }

    // Also kill all descendants found via the PID tree
    if let Ok(descendants) = pid_tree::get_all_descendant_pids(pid).await {
        for desc_pid in &descendants {
            let _ = signal::kill(Pid::from_raw(*desc_pid as i32), Signal::SIGKILL);
        }
    }

    // Wait for the process to exit
    loop {
        match waitpid(nix_pid, Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::StillAlive) => {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            Ok(_status) => {
                break;
            }
            Err(nix::errno::Errno::ECHILD) => {
                // No more children to wait for
                break;
            }
            Err(e) => {
                log::debug!("[DEBUG] waitpid({pid}) error: {e}");
                break;
            }
        }
    }

    // Clean up the cgroup directory
    if let Some(cgroup_path) = cgroup_path {
        if let Err(e) = cgroup::remove_process_cgroup(cgroup_path).await {
            log::debug!("[DEBUG] Failed to remove cgroup for {pid}: {e}");
        }
    }
}

/// Wait for a child process to exit, with optional timeout and memory monitoring.
///
/// String refs from binary offset 0x1b8598:
///   "[DEBUG] forward_stdin: Starting stdin forwarding for process"
///   "[DEBUG] Received shutdown signal for process"
///   "[DEBUG] Stopping waiting for process"
///   "process_ws_message: Shutting down, terminating"
///   "signal: , core_dumped: , stopped_signal: , continued: "
///   ") timed out after"
///   "process_ws_message: Timeout"
///   ") exceeded memory limit of  bytes"
pub async fn wait_for_child_to_exit(
    pid: u32,
    timeout: Option<Duration>,
    memory_limit_bytes: Option<u64>,
    cgroup_path: Option<PathBuf>,
    cgroup_version: Option<CgroupVersion>,
    oom_killed_rx: Option<oneshot::Receiver<()>>,
    stop_rx: Option<oneshot::Receiver<()>>,
) -> ExitReason {
    let start = Instant::now();
    let nix_pid = Pid::from_raw(pid as i32);

    // Pin the OOM receiver for select
    let mut oom_rx = oom_killed_rx;
    let mut stop = stop_rx;

    loop {
        // Check if process has exited
        match waitpid(nix_pid, Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::Exited(_, status)) => {
                log::debug!("[DEBUG] Process {pid} exited with status {status}");
                return ExitReason::Exited { status };
            }
            Ok(WaitStatus::Signaled(_, sig, core_dumped)) => {
                log::debug!(
                    "[DEBUG] Process {pid} killed by signal: {sig}, core_dumped: {core_dumped}"
                );
                return ExitReason::Signaled {
                    signal: sig as i32,
                    core_dumped,
                };
            }
            Ok(WaitStatus::StillAlive) => {}
            Ok(_) => {}
            Err(nix::errno::Errno::ECHILD) => {
                return ExitReason::Exited { status: -1 };
            }
            Err(_) => {}
        }

        // Check timeout
        if let Some(timeout_dur) = timeout {
            if start.elapsed() >= timeout_dur {
                log::debug!(
                    "[DEBUG] Process {pid} timed out after {} seconds",
                    timeout_dur.as_secs()
                );
                kill_and_wait(pid, cgroup_path.as_ref()).await;
                return ExitReason::TimedOut {
                    timeout_secs: timeout_dur.as_secs(),
                };
            }
        }

        // Check per-process memory limit
        if let (Some(limit), Some(ref cp), Some(version)) =
            (memory_limit_bytes, &cgroup_path, cgroup_version)
        {
            if let Ok(usage) = cgroup::read_memory_usage(cp, version).await {
                if usage > limit {
                    log::debug!(
                        "[DEBUG] Process {pid} exceeded memory limit of {limit} bytes (usage: {usage})"
                    );
                    kill_and_wait(pid, cgroup_path.as_ref()).await;
                    return ExitReason::OutOfMemory {
                        limit_bytes: limit,
                    };
                }
            }
        }

        // Check for container-level OOM kill notification
        if let Some(ref mut rx) = oom_rx {
            if rx.try_recv().is_ok() {
                log::debug!("[DEBUG] Process {pid} received container OOM notification");
                kill_and_wait(pid, cgroup_path.as_ref()).await;
                return ExitReason::ContainerOom {
                    limit_bytes: memory_limit_bytes.unwrap_or(0),
                };
            }
        }

        // Check for stop signal (shutdown)
        if let Some(ref mut rx) = stop {
            if rx.try_recv().is_ok() {
                log::debug!("[DEBUG] Stopping waiting for process {pid}");
                kill_and_wait(pid, cgroup_path.as_ref()).await;
                return ExitReason::KilledByProcessApi;
            }
        }

        // Poll interval
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// Format exit reason as a human-readable string for WebSocket responses.
/// Decompiled from 0x1bafc0..0x1bb772  (1970 bytes)
/// Xrefs: "src/io.rs [PID] seconds ... ex..."
pub fn format_exit_reason(pid: u32, reason: &ExitReason, elapsed_secs: f64) -> String {
    match reason {
        ExitReason::Exited { status } => {
            format!("[PID {pid}] {elapsed_secs:.1} seconds: exited with status: {status}")
        }
        ExitReason::Signaled { signal, core_dumped } => {
            format!(
                "[PID {pid}] {elapsed_secs:.1} seconds: signal: {signal}, core_dumped: {core_dumped}"
            )
        }
        ExitReason::TimedOut { timeout_secs } => {
            format!("[PID {pid}] {elapsed_secs:.1} seconds: timed out after {timeout_secs} seconds")
        }
        ExitReason::OutOfMemory { limit_bytes } => {
            format!(
                "[PID {pid}] {elapsed_secs:.1} seconds: exceeded memory limit of {limit_bytes} bytes"
            )
        }
        ExitReason::ContainerOom { limit_bytes } => {
            format!(
                "[PID {pid}] {elapsed_secs:.1} seconds: exceeded container memory limit of {limit_bytes} bytes"
            )
        }
        ExitReason::KilledByProcessApi => {
            format!("[PID {pid}] {elapsed_secs:.1} seconds: killed by process_api")
        }
    }
}
