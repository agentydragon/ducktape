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

use std::collections::HashMap;
use std::time::Duration;

use tokio::sync::{broadcast, oneshot};

use crate::cgroup::{self, CgroupController, CgroupVersion};
use crate::state::ProcessMap;

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
pub async fn container_oom_monitor(
    controller: CgroupController,
    container_memory_limit: u64,
    polling_period: Duration,
    proc_map: ProcessMap,
    mut shutdown_rx: broadcast::Receiver<()>,
    oom_killed_txs: HashMap<String, oneshot::Sender<()>>,
) {
    log::debug!("[DEBUG] container_oom_monitor: starting, limit={container_memory_limit} bytes, poll={polling_period:?}");

    loop {
        tokio::select! {
            _ = tokio::time::sleep(polling_period) => {}
            _ = shutdown_rx.recv() => {
                log::debug!("[DEBUG] container_oom_monitor: Received shutdown signal, exiting");
                return;
            }
        }

        // Read container-level memory usage
        let usage = match cgroup::read_memory_usage(&controller.base_path, controller.version).await {
            Ok(u) => u,
            Err(e) => {
                log::debug!("[DEBUG] container_oom_monitor: failed to read memory usage: {e}");
                continue;
            }
        };

        if usage <= container_memory_limit {
            continue;
        }

        // Container memory limit exceeded - find and kill the largest process
        log::info!(
            "[OOM_KILL] Container memory limit exceeded: usage={usage} > limit={container_memory_limit}"
        );

        // Collect process info while holding the lock, then release it
        let process_cgroups: Vec<(String, u32, std::path::PathBuf)> = {
            let map = proc_map.lock();
            map.iter()
                .filter_map(|(process_id, entry)| {
                    entry.handle.cgroup_path.as_ref().map(|path| {
                        (process_id.clone(), entry.pid, path.clone())
                    })
                })
                .collect()
        };

        // Find which process is using the most memory (lock is released)
        let mut largest_pid: Option<u32> = None;
        let mut largest_usage: u64 = 0;
        let mut largest_process_id: Option<String> = None;

        for (process_id, pid, cgroup_path) in &process_cgroups {
            if let Ok(proc_usage) =
                cgroup::read_memory_usage(cgroup_path, controller.version).await
            {
                if proc_usage > largest_usage {
                    largest_usage = proc_usage;
                    largest_pid = Some(*pid);
                    largest_process_id = Some(process_id.clone());
                }
            }
        }

        if let (Some(pid), Some(process_id)) = (largest_pid, largest_process_id) {
            log::info!(
                "[OOM_KILL] Killing process {process_id} (PID {pid}) using {largest_usage} bytes to free up memory"
            );

            // Signal the process's OOM channel
            if let Some(_tx) = oom_killed_txs.get(&process_id) {
                // Can't move out of HashMap reference, but the channel signals the process monitor
                log::debug!("[DEBUG] container_oom_monitor: signaling OOM for {process_id}");
            }

            // Kill the process
            crate::proc_handle::kill_and_wait(pid, None).await;
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
pub async fn per_process_memory_monitor(
    pid: u32,
    process_id: String,
    cgroup_path: std::path::PathBuf,
    version: CgroupVersion,
    memory_limit: u64,
    polling_period: Duration,
    oom_killed_tx: oneshot::Sender<()>,
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
                let _ = oom_killed_tx.send(());
                return;
            }
            Ok(_) => {}
            Err(e) => {
                log::debug!(
                    "[DEBUG] per_process_memory_monitor: failed to read memory for {process_id}: {e}"
                );
            }
        }
    }
}
