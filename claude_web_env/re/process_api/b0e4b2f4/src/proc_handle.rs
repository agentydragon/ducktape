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
use serde::{Deserialize, Serialize};
use tokio::sync::oneshot;

use crate::cgroup::{self, CgroupVersion};
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
/// Xrefs: "reattachable", "timeout", "memory_limit_bytes", "start_time",
///   "process_group_pid", "killed_by_process_api", "stop_waiting_tx",
///   "exit_status_rx", "exit_status_tx"
#[derive(Debug)]
pub struct ProcHandle {
    pub pid: u32,
    pub process_group_pid: u32,
    pub reattachable: bool,
    pub timeout: Option<Duration>,
    pub memory_limit_bytes: Option<u64>,
    pub start_time: Instant,
    pub memory_cgroup_path: Option<PathBuf>,
    pub killed_by_process_api: bool,
    /// Sender to signal that the process should stop waiting.
    /// Xrefs: "stop_waiting_tx is already taken"
    pub stop_waiting_tx: Option<oneshot::Sender<()>>,
    /// Receiver for the stop-waiting signal (passed to wait_for_child_to_exit).
    pub stop_waiting_rx: Option<oneshot::Receiver<()>>,
    /// Receiver for exit status from the wait task.
    /// Xrefs: "exit_status_rx is already taken"
    pub exit_status_rx: Option<oneshot::Receiver<ExitReason>>,
    /// Sender for exit status (passed to wait_for_child_to_exit).
    pub exit_status_tx: Option<oneshot::Sender<ExitReason>>,
    /// Sender for OOM kill notification (moved to OomChannelMap on registration).
    pub oom_killed_tx: Option<oneshot::Sender<()>>,
    /// Receiver for OOM kill notification (passed to wait_for_child_to_exit).
    pub oom_killed_rx: Option<oneshot::Receiver<()>>,
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
            memory_cgroup_path: None,
            killed_by_process_api: false,
            stop_waiting_tx: None,
            stop_waiting_rx: None,
            exit_status_rx: None,
            exit_status_tx: None,
            oom_killed_tx: None,
            oom_killed_rx: None,
        }
    }
}

/// Static process configuration.
/// Serde visitor at 0x21c970..0x21cb45 (469 bytes), variant visitor at
/// 0x21d560..0x21d732 (466 bytes).
/// Fields from disassembly: process_id, pid, reattachable, timeout,
///   memory_limit_bytes, start_time
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProcessInfo {
    pub process_id: String,
    pub pid: u32,
    pub reattachable: bool,
    pub timeout: Option<u64>,
    pub memory_limit_bytes: Option<u64>,
    pub start_time: u64,
}

/// Cgroup state for a managed process.
/// Serde visitor at 0x21ce40..0x21d015 (469 bytes).
/// Fields from disassembly: process_id, memory_limit_bytes,
///   memory_usage_bytes, memory_cgroup_path, process_group_pid,
///   internal_state
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CgroupConfig {
    pub process_id: String,
    pub memory_limit_bytes: Option<u64>,
    pub memory_usage_bytes: Option<u64>,
    pub memory_cgroup_path: Option<String>,
    pub process_group_pid: u32,
    pub internal_state: String,
}

/// Wraps CgroupConfig, OOM channel, and ProcessInfo for a managed process.
/// Debug impl at 0x21c870..0x21c96b (251 bytes).
/// Fields from disassembly: cgroup, oom_killed_tx, process_info
#[derive(Debug, Serialize, Deserialize)]
pub struct ProcController {
    pub cgroup: Option<CgroupConfig>,
    /// Present in the original binary's struct layout (confirmed by Debug impl
    /// at 0x21c870) but never read outside of Debug formatting — always None.
    #[serde(skip)]
    #[allow(dead_code)]
    pub oom_killed_tx: Option<oneshot::Sender<()>>,
    pub process_info: ProcessInfo,
}

impl Clone for ProcController {
    fn clone(&self) -> Self {
        ProcController {
            cgroup: self.cgroup.clone(),
            oom_killed_tx: None, // oneshot::Sender cannot be cloned
            process_info: self.process_info.clone(),
        }
    }
}

/// Decompiled from 0x1b5620..0x1b5b56  (1334 bytes)
/// Xrefs: kill(pid, SIGKILL), waitpid() loop with errno ECHILD handling
///
/// Kill a process and all its descendants, then wait for them to exit.
/// Sends SIGKILL to the process group, then loops waitpid() until ECHILD.
///
/// String refs:
///   "Error waiting for process group: "
///   "Process group finished"
///   "Timeout waiting for process group to finish"
///   "Cgroup is not ready Waiting for process group to finish"
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

    // Wait for the process group to finish (with timeout)
    let wait_timeout = Duration::from_secs(30);
    let wait_start = Instant::now();

    loop {
        if wait_start.elapsed() >= wait_timeout {
            log::debug!("[DEBUG] Timeout waiting for process group to finish (PID {pid})");
            break;
        }

        // Check cgroup readiness if we have a cgroup path
        if let Some(cp) = cgroup_path {
            let procs_path = cp.join("cgroup.procs");
            if procs_path.exists() {
                match std::fs::read_to_string(&procs_path) {
                    Ok(contents) => {
                        if !contents.trim().is_empty() {
                            log::debug!(
                                "[DEBUG] Cgroup is not ready. Waiting for process group to finish (PID {pid})"
                            );
                        }
                    }
                    Err(_) => {}
                }
            }
        }

        match waitpid(nix_pid, Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::StillAlive) => {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            Ok(_status) => {
                log::debug!("[DEBUG] Process group finished (PID {pid})");
                break;
            }
            Err(nix::errno::Errno::ECHILD) => {
                // No more children to wait for
                log::debug!("[DEBUG] Process group finished (PID {pid})");
                break;
            }
            Err(e) => {
                log::debug!("[DEBUG] Error waiting for process group (PID {pid}): {e}");
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
/// Sends the exit reason via `exit_status_tx` channel.
///
/// String refs from binary offset 0x1b8598:
///   "[DEBUG] Starting wait_for_child_to_exit for process , start_time: , memory_limit_bytes:"
///   "[DEBUG] Killed process tree for process"
///   "[DEBUG] Failed to send exit status for process"
///   "[DEBUG] Failed to send timeout status for process"
///   "[DEBUG] Failed to send OOM killed status for process"
///   "wait_for_child_to_exit received message to stop waiting for process"
///   "[DEBUG] Exiting wait_for_child_to_exit for process"
///   "[DEBUG] oom killed_rx closed for process"
pub async fn wait_for_child_to_exit(
    pid: u32,
    timeout: Option<Duration>,
    memory_limit_bytes: Option<u64>,
    cgroup_path: Option<PathBuf>,
    cgroup_version: Option<CgroupVersion>,
    oom_killed_rx: Option<oneshot::Receiver<()>>,
    stop_rx: Option<oneshot::Receiver<()>>,
    exit_status_tx: oneshot::Sender<ExitReason>,
) {
    let start = Instant::now();
    let nix_pid = Pid::from_raw(pid as i32);

    log::debug!(
        "[DEBUG] Starting wait_for_child_to_exit for process (PID {pid}), start_time: {:?}, memory_limit_bytes: {memory_limit_bytes:?}",
        start
    );

    let mut oom_rx = oom_killed_rx;
    let mut stop = stop_rx;

    let exit_reason = loop {
        // Check if process has exited
        match waitpid(nix_pid, Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::Exited(_, status)) => {
                log::debug!("[DEBUG] wait_for_child_to_exit: Process {pid} exited with status {status}");
                break ExitReason::Exited { status };
            }
            Ok(WaitStatus::Signaled(_, sig, core_dumped)) => {
                log::debug!(
                    "[DEBUG] wait_for_child_to_exit: Process {pid} killed by signal: {sig}, core_dumped: {core_dumped}"
                );
                break ExitReason::Signaled {
                    signal: sig as i32,
                    core_dumped,
                };
            }
            Ok(WaitStatus::StillAlive) => {}
            Ok(_) => {}
            Err(nix::errno::Errno::ECHILD) => {
                break ExitReason::Exited { status: -1 };
            }
            Err(e) => {
                log::debug!("[DEBUG] Failed to get status for process {pid}: {e}");
            }
        }

        // Check timeout
        if let Some(timeout_dur) = timeout {
            if start.elapsed() >= timeout_dur {
                log::debug!(
                    "[DEBUG] Killed process tree for process (PID {pid}) exceeded timeout of {} seconds",
                    timeout_dur.as_secs()
                );
                kill_and_wait(pid, cgroup_path.as_ref()).await;
                let reason = ExitReason::TimedOut {
                    timeout_secs: timeout_dur.as_secs(),
                };
                log::debug!("[DEBUG] Exiting wait_for_child_to_exit for process (PID {pid})");
                if exit_status_tx.send(reason).is_err() {
                    log::debug!("[DEBUG] Failed to send timeout status for process (PID {pid})");
                }
                return;
            }
        }

        // Check per-process memory limit
        if let (Some(limit), Some(ref cp), Some(version)) =
            (memory_limit_bytes, &cgroup_path, cgroup_version)
        {
            if let Ok(usage) = cgroup::read_memory_usage(cp, version).await {
                if usage > limit {
                    log::debug!(
                        "[DEBUG] Killing process tree OOM killed process (PID {pid}) exceeded memory limit of {limit} bytes (usage: {usage})"
                    );
                    kill_and_wait(pid, cgroup_path.as_ref()).await;
                    let reason = ExitReason::OutOfMemory {
                        limit_bytes: limit,
                    };
                    log::debug!("[DEBUG] Exiting wait_for_child_to_exit for process (PID {pid})");
                    if exit_status_tx.send(reason).is_err() {
                        log::debug!("[DEBUG] Failed to send OOM killed status for process (PID {pid})");
                    }
                    return;
                }
            }
        }

        // Check for container-level OOM kill notification
        if let Some(ref mut rx) = oom_rx {
            match rx.try_recv() {
                Ok(()) => {
                    log::debug!("[DEBUG] Killing process tree OOM killed process (PID {pid})");
                    kill_and_wait(pid, cgroup_path.as_ref()).await;
                    let reason = ExitReason::ContainerOom {
                        limit_bytes: memory_limit_bytes.unwrap_or(0),
                    };
                    log::debug!("[DEBUG] Exiting wait_for_child_to_exit for process (PID {pid})");
                    if exit_status_tx.send(reason).is_err() {
                        log::debug!("[DEBUG] Failed to send OOM killed status for process (PID {pid})");
                    }
                    return;
                }
                Err(oneshot::error::TryRecvError::Closed) => {
                    log::debug!("[DEBUG] oom killed_rx closed for process (PID {pid})");
                    oom_rx = None;
                }
                Err(oneshot::error::TryRecvError::Empty) => {}
            }
        }

        // Check for stop signal (shutdown)
        if let Some(ref mut rx) = stop {
            if rx.try_recv().is_ok() {
                log::debug!("wait_for_child_to_exit received message to stop waiting for process (PID {pid})");
                kill_and_wait(pid, cgroup_path.as_ref()).await;
                break ExitReason::KilledByProcessApi;
            }
        }

        // Poll interval
        tokio::time::sleep(Duration::from_millis(50)).await;
    };

    log::debug!("[DEBUG] Exiting wait_for_child_to_exit for process (PID {pid})");
    if exit_status_tx.send(exit_reason).is_err() {
        log::debug!("[DEBUG] Failed to send exit status for process (PID {pid})");
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
