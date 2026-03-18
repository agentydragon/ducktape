//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Container-level OOM monitoring: polls cgroup memory usage and kills
//! the largest process when the container exceeds its memory limit.
//!
//! Functions decompiled from:
//!   oom_event_handler_setup:     0x21c2b0..0x21c510  (608 bytes)
//!   oom_killed_tx_setup:         0x21c870..0x21c96b  (251 bytes)
//!
//! String refs from binary file offsets:
//!   0x1af21e: "container_oom_monitor: Received shutdown signal, exiting container_oom_killer"
//!   0x1af5ed: "container_oom_monitor: Received shutdown signal during post-kill wait, exiting"
//!   0x1af72a: "per_process_memory_monitor: Received shutdown signal, exiting per_process_memory_monitor"
//!
//! Additional string refs:
//!   "[DEBUG] No channel available to send OOM notification for process_id , killing directly"
//!   "[OOM_KILL] process_id= pid= memory_bytes= limit_bytes="
//!   "[process_id=, pid=] killed by container OOM killer"
//!   "reason=container_limit cmdline="
//!   "/var/log/.process_api", "/oom_killed.log"
//!   "[DEBUG] container_oom_monitor: Failed to create directory for OOM killed process "
//!   "[DEBUG] container_oom_monitor: Failed to open OOM killed log for process "
//!   "[DEBUG] container_oom_monitor: Failed to write to OOM killed log for process "
//!   "[DEBUG] container_oom_monitor: Waiting for killed process ) to exit and memory to be reclaimed"
//!   "[DEBUG] container_oom_monitor: Phase 1 timed out"
//!   "[DEBUG] container_oom_monitor: Timed out 30s after killing"

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use tokio::sync::{broadcast, oneshot};

use crate::adopter;
use crate::cgroup::{self, CgroupController, CgroupVersion};
use crate::pid_tree;
use crate::state::ProcessMap;

/// Shared registry of per-process OOM notification channels.
/// The container OOM monitor and per-process monitors both access this
/// to signal wait_for_child_to_exit when a process should be killed.
pub type OomChannelMap = Arc<Mutex<HashMap<String, oneshot::Sender<()>>>>;

/// Create a new empty OOM channel map.
pub fn new_oom_channel_map() -> OomChannelMap {
    Arc::new(Mutex::new(HashMap::new()))
}

/// Read the command line of a process from /proc/{pid}/cmdline.
async fn read_cmdline(pid: u32) -> String {
    match tokio::fs::read(format!("/proc/{pid}/cmdline")).await {
        Ok(data) => {
            // cmdline uses NUL bytes as separators
            data.iter()
                .map(|&b| if b == 0 { ' ' } else { b as char })
                .collect::<String>()
                .trim()
                .to_string()
        }
        Err(_) => String::new(),
    }
}

/// Write an OOM kill event to /var/log/.process_api/oom_killed.log.
///
/// String refs:
///   "/var/log/.process_api", "/oom_killed.log"
///   "[DEBUG] container_oom_monitor: Failed to create directory for OOM killed process "
///   "[DEBUG] container_oom_monitor: Failed to open OOM killed log for process "
///   "[DEBUG] container_oom_monitor: Failed to write to OOM killed log for process "
async fn write_oom_kill_log(
    process_id: &str,
    pid: u32,
    memory_bytes: u64,
    limit_bytes: u64,
    cmdline: &str,
) {
    let log_dir = std::path::Path::new("/var/log/.process_api");
    if !log_dir.exists() {
        if let Err(e) = tokio::fs::create_dir_all(log_dir).await {
            log::debug!(
                "[DEBUG] container_oom_monitor: Failed to create directory for OOM killed process {process_id}: {e}"
            );
            return;
        }
    }

    let log_path = log_dir.join("oom_killed.log");
    let entry = format!(
        "[OOM_KILL] process_id={process_id} pid={pid} memory_bytes={memory_bytes} limit_bytes={limit_bytes} reason=container_limit cmdline={cmdline}\n"
    );

    match tokio::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .await
    {
        Ok(mut file) => {
            use tokio::io::AsyncWriteExt;
            if let Err(e) = file.write_all(entry.as_bytes()).await {
                log::debug!(
                    "[DEBUG] container_oom_monitor: Failed to write to OOM killed log for process {process_id}: {e}"
                );
            }
        }
        Err(e) => {
            log::debug!(
                "[DEBUG] container_oom_monitor: Failed to open OOM killed log for process {process_id}: {e}"
            );
        }
    }
}

/// Decompiled from 0x21c2b0..0x21c510  (608 bytes)
/// Xrefs: "proc_handle", "oom_killed_rx"
///
/// Container-level OOM monitor task. Polls the container cgroup's memory
/// usage at a configured interval. When usage exceeds the limit, identifies
/// the process using the most memory and kills it.
///
/// String refs at binary offset 0x1af21e:
///   "container_oom_monitor: Received shutdown signal, exiting"
///   "[OOM_KILL] Container memory limit exceeded"
///   "Killing process ... to free up memory"
///   "[DEBUG] No channel available to send OOM notification for process_id , killing directly"
///   "[DEBUG] container_oom_monitor: Waiting for killed process ) to exit and memory to be reclaimed"
///   "container_oom_monitor: Received shutdown signal during post-kill wait, exiting"
pub async fn container_oom_monitor(
    controller: CgroupController,
    container_memory_limit: u64,
    polling_period: Duration,
    proc_map: ProcessMap,
    mut shutdown_rx: broadcast::Receiver<()>,
    oom_channels: OomChannelMap,
) {
    log::debug!(
        "[DEBUG] container_oom_monitor: starting, limit={container_memory_limit} bytes, poll={polling_period:?}"
    );

    loop {
        tokio::select! {
            _ = tokio::time::sleep(polling_period) => {}
            _ = shutdown_rx.recv() => {
                log::debug!("[DEBUG] container_oom_monitor: Received shutdown signal, exiting container_oom_killer");
                return;
            }
        }

        // Read container-level memory usage
        let usage = match cgroup::read_memory_usage(&controller.base_path, controller.version).await
        {
            Ok(u) => u,
            Err(e) => {
                log::debug!("[DEBUG] Error getting container memory usage: {e}");
                continue;
            }
        };

        if usage <= container_memory_limit {
            continue;
        }

        // Container memory limit exceeded
        log::info!(
            "[DEBUG] container_oom_monitor: Container memory usage {usage} exceeds limit {container_memory_limit}"
        );

        // Adopt orphans before memory scan to ensure accurate process tracking
        log::debug!("[DEBUG] container_oom_monitor: Adopting orphans before memory scan...");
        if let Err(e) = adopter::try_adopt_orphans(&controller, &proc_map).await {
            log::debug!("[DEBUG] container_oom_monitor: Failed to adopt orphans: {e}");
        }

        // Read fresh memory usage for ALL tracked processes to find the largest
        log::debug!(
            "[DEBUG] container_oom_monitor: Reading fresh memory usage for ALL processes to find largest..."
        );

        // Collect process info while holding the lock, then release it
        let process_cgroups: Vec<(String, u32, std::path::PathBuf)> = {
            let map = proc_map.lock();
            map.iter()
                .filter_map(|(process_id, entry)| {
                    entry
                        .proc_handle
                        .memory_cgroup_path
                        .as_ref()
                        .map(|path| (process_id.clone(), entry.pid, path.clone()))
                })
                .collect()
        };

        // Find which process is using the most memory (lock is released)
        let mut largest_pid: Option<u32> = None;
        let mut largest_usage: u64 = 0;
        let mut largest_process_id: Option<String> = None;
        let mut largest_cgroup_path: Option<std::path::PathBuf> = None;

        for (process_id, pid, cgroup_path) in &process_cgroups {
            if let Ok(proc_usage) = cgroup::read_memory_usage(cgroup_path, controller.version).await
            {
                if proc_usage > largest_usage {
                    largest_usage = proc_usage;
                    largest_pid = Some(*pid);
                    largest_process_id = Some(process_id.clone());
                    largest_cgroup_path = Some(cgroup_path.clone());
                }
            }
        }

        if let (Some(pid), Some(process_id)) = (largest_pid, &largest_process_id) {
            log::info!(
                "[DEBUG] container_oom_monitor: Killing process {process_id} (PID {pid}) with memory usage {largest_usage} to free up memory"
            );
            log::info!("[process_id={process_id}, pid={pid}] killed by container OOM killer");

            // Read cmdline for logging
            let cmdline = read_cmdline(pid).await;

            // Write OOM kill event to log file
            write_oom_kill_log(
                process_id,
                pid,
                largest_usage,
                container_memory_limit,
                &cmdline,
            )
            .await;

            let kill_start = std::time::Instant::now();

            // Signal the process's OOM channel (take ownership via remove)
            let tx = {
                let mut channels = oom_channels.lock();
                channels.remove(process_id)
            };

            if let Some(tx) = tx {
                log::debug!("[DEBUG] container_oom_monitor: signaling OOM for {process_id}");
                if tx.send(()).is_err() {
                    log::debug!(
                        "[DEBUG] container_oom_monitor: Failed to notify kill for process {process_id}"
                    );
                }
            } else {
                log::debug!(
                    "[DEBUG] No channel available to send OOM notification for process_id {process_id}, killing directly"
                );
                crate::proc_handle::kill_and_wait(pid, largest_cgroup_path.as_ref()).await;
            }

            // Post-kill wait: verify memory is reclaimed
            // String refs: "Waiting for killed process ) to exit and memory to be reclaimed (max s)..."
            //   "Phase 1 timed out", "Timed out 30s after killing"
            //   "container_oom_monitor: Received shutdown signal during post-kill wait, exiting"
            let max_wait = Duration::from_secs(30);
            let start = std::time::Instant::now();
            log::debug!(
                "[DEBUG] container_oom_monitor: Waiting for killed process {process_id} (PID {pid}) to exit and memory to be reclaimed (max {}s)...",
                max_wait.as_secs()
            );

            // Phase 1: wait for process to exit (disappear from /proc)
            let phase1_timeout = Duration::from_secs(10);
            loop {
                if start.elapsed() >= phase1_timeout {
                    log::debug!(
                        "[DEBUG] container_oom_monitor: Phase 1 timed out: process {process_id} (PID {pid}) still in /proc after {:?}",
                        start.elapsed()
                    );
                    break;
                }

                if !pid_tree::pid_exists(pid) {
                    let elapsed = kill_start.elapsed();
                    log::debug!(
                        "[DEBUG] container_oom_monitor: Killed process {process_id} (PID {pid}) exited after {:.1}s",
                        elapsed.as_secs_f64()
                    );
                    break;
                }

                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(100)) => {}
                    _ = shutdown_rx.recv() => {
                        log::debug!("container_oom_monitor: Received shutdown signal during post-kill wait, exiting");
                        return;
                    }
                }
            }

            // Phase 2: wait for memory to drop below limit
            loop {
                if start.elapsed() >= max_wait {
                    log::debug!(
                        "[DEBUG] container_oom_monitor: Timed out 30s after killing {process_id} (pid {pid}): memory may still be above limit"
                    );
                    break;
                }

                match cgroup::read_memory_usage(&controller.base_path, controller.version).await {
                    Ok(current_usage) if current_usage <= container_memory_limit => {
                        let elapsed = kill_start.elapsed();
                        log::debug!(
                            "[DEBUG] container_oom_monitor: Memory reclaimed to {current_usage}) {:.1}s after kill",
                            elapsed.as_secs_f64()
                        );
                        break;
                    }
                    Ok(_) => {}
                    Err(e) => {
                        log::debug!(
                            "[DEBUG] container_oom_monitor: Failed to read memory during post-kill wait: {e}"
                        );
                        break;
                    }
                }

                tokio::select! {
                    _ = tokio::time::sleep(polling_period) => {}
                    _ = shutdown_rx.recv() => {
                        log::debug!("container_oom_monitor: Received shutdown signal during post-kill wait, exiting");
                        return;
                    }
                }
            }
        }
    }
}

/// Decompiled from 0x21c870..0x21c96b  (251 bytes)
/// Xrefs: "oom_killed_tx"
///
/// Per-process memory monitor task. Checks a specific process's cgroup
/// memory usage against its per-process limit.
///
/// String refs at binary offset 0x1af72a:
///   "per_process_memory_monitor: Received shutdown signal, exiting"
#[allow(clippy::too_many_arguments)] // Matches binary's function signature
pub async fn per_process_memory_monitor(
    pid: u32,
    process_id: String,
    cgroup_path: std::path::PathBuf,
    version: CgroupVersion,
    memory_limit: u64,
    polling_period: Duration,
    oom_channels: OomChannelMap,
    mut shutdown_rx: broadcast::Receiver<()>,
) {
    log::debug!(
        "[DEBUG] per_process_memory_monitor: starting for {process_id} (PID {pid}), limit={memory_limit}"
    );

    loop {
        tokio::select! {
            _ = tokio::time::sleep(polling_period) => {}
            _ = shutdown_rx.recv() => {
                log::debug!(
                    "[DEBUG] per_process_memory_monitor: Received shutdown signal, exiting"
                );
                return;
            }
        }

        match cgroup::read_memory_usage(&cgroup_path, version).await {
            Ok(usage) if usage > memory_limit => {
                log::info!(
                    "[OOM_KILL] Process {process_id} (PID {pid}) exceeded memory limit: \
                     usage={usage} > limit={memory_limit}"
                );
                // Take OOM channel from shared map and signal
                let tx = {
                    let mut channels = oom_channels.lock();
                    channels.remove(&process_id)
                };
                if let Some(tx) = tx {
                    let _ = tx.send(());
                }
                return;
            }
            Ok(_) => {}
            Err(e) => {
                log::debug!(
                    "[DEBUG] per_process_memory_monitor: Failed to check memory usage for process {process_id}: {e}"
                );
            }
        }
    }
}
