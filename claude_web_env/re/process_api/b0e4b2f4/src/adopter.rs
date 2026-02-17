//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Orphan process adoption and zombie reaping. As PID 1 in the container,
//! process_api is responsible for adopting orphaned processes and reaping
//! zombie children.
//!
//! No definitive decompiled functions were found with unique Ghidra string
//! markers. The functionality was reconstructed from string evidence at
//! binary file offsets:
//!   0x1afc80: "/build/src/adopter.rs"
//!             "[DEBUG] monitor_orphans: Failed to adopt orphans: "
//!             "[DEBUG] monitor_orphans: Received shutdown signal, exiting"
//!   0x1afd02: "[DEBUG] Error getting child PIDs: "
//!             "[DEBUG] Error listing process API cgroups: "
//!             "[DEBUG] Error getting PID to cgroup map: "
//!             "[DEBUG] Reaping zombie PID  (first seen  ago)"
//!   0x1afde1: "[DEBUG] Error reading status for orphaned PID "
//!             "[DEBUG] Found orphan process ) in unattributed cgroup "
//!             "[DEBUG] Failed to adopt orphan process ) in cgroup "
//!             "[DEBUG] Successfully adopted orphan process "
//!             "[DEBUG] Reaping tracked orphaned zombie, first seen "
//!             "[DEBUG] Found new zombie for tracked orphan ), will reap in next iteration"

use std::collections::HashMap;
use std::collections::hash_map::Entry;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use nix::sys::wait::{WaitPidFlag, WaitStatus, waitpid};
use nix::unistd::Pid;
use tokio::sync::broadcast;

use crate::cgroup::{self, CgroupController};
use crate::pid_tree;
use crate::state::ProcessMap;

/// Tracked orphan zombie awaiting reaping.
struct TrackedZombie {
    pid: u32,
    first_seen: Instant,
}

/// One-shot orphan adoption pass, callable from outside the monitor loop.
/// Used by the container OOM monitor to ensure accurate process tracking
/// before scanning memory usage.
pub async fn try_adopt_orphans(
    controller: &CgroupController,
    proc_map: &ProcessMap,
) -> Result<(), String> {
    let mut tracked_zombies = HashMap::new();
    adopt_orphans(controller, proc_map, &mut tracked_zombies).await
}

/// Main orphan monitor loop. Runs as a spawned task.
///
/// Reconstructed from string evidence at binary file offset 0x1afc80:
///   "[DEBUG] monitor_orphans: Failed to adopt orphans:"
///   "[DEBUG] monitor_orphans: Received shutdown signal, exiting"
///   "[DEBUG] Starting orphan monitor task"
pub async fn monitor_orphans(
    controller: CgroupController,
    proc_map: ProcessMap,
    mut shutdown_rx: broadcast::Receiver<()>,
) {
    log::debug!("[DEBUG] Starting orphan monitor task");
    let mut tracked_zombies: HashMap<u32, TrackedZombie> = HashMap::new();

    loop {
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(5)) => {}
            _ = shutdown_rx.recv() => {
                log::debug!("[DEBUG] monitor_orphans: Received shutdown signal, exiting");
                return;
            }
        }

        // Clean up tracked zombies that no longer exist
        // Xrefs: "[DEBUG] Removed orphaned process ) because it no longer exists"
        tracked_zombies.retain(|_, zombie| {
            if !pid_tree::pid_exists(zombie.pid) {
                log::debug!(
                    "[DEBUG] Removed orphaned process (PID {}) because it no longer exists",
                    zombie.pid
                );
                false
            } else {
                true
            }
        });

        // Reap any zombie children of PID 1
        reap_zombies(&mut tracked_zombies);

        // Find and adopt orphan processes
        if let Err(e) = adopt_orphans(&controller, &proc_map, &mut tracked_zombies).await {
            log::debug!("[DEBUG] monitor_orphans: Failed to adopt orphans: {e}");
        }
    }
}

/// Reap zombie processes using waitpid(-1, WNOHANG).
///
/// String refs at binary offset 0x1afd02:
///   "[DEBUG] Reaping zombie PID  (first seen  ago)"
///   "[DEBUG] Reaping tracked orphaned zombie, first seen "
fn reap_zombies(tracked_zombies: &mut HashMap<u32, TrackedZombie>) {
    loop {
        match waitpid(Pid::from_raw(-1), Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::Exited(pid, _)) | Ok(WaitStatus::Signaled(pid, _, _)) => {
                let pid_u32 = pid.as_raw() as u32;
                if let Some(zombie) = tracked_zombies.remove(&pid_u32) {
                    log::debug!(
                        "[DEBUG] Reaping zombie PID {} (first seen {:?} ago)",
                        zombie.pid,
                        zombie.first_seen.elapsed()
                    );
                }
            }
            Ok(WaitStatus::StillAlive) | Err(_) => break,
            _ => {}
        }
    }
}

/// Find orphan processes in unattributed cgroups and adopt them.
///
/// String refs at binary offset 0x1afde1:
///   "[DEBUG] Error reading status for orphaned PID "
///   "[DEBUG] Found orphan process ) in unattributed cgroup "
///   "[DEBUG] Failed to adopt orphan process ) in cgroup "
///   "[DEBUG] Successfully adopted orphan process "
///   "[DEBUG] Found new zombie for tracked orphan ), will reap in next iteration"
async fn adopt_orphans(
    controller: &CgroupController,
    proc_map: &ProcessMap,
    tracked_zombies: &mut HashMap<u32, TrackedZombie>,
) -> Result<(), String> {
    // Get all child PIDs of PID 1
    let child_pids = pid_tree::get_child_pids(1)
        .await
        .map_err(|e| format!("[DEBUG] Error getting child PIDs: {e}"))?;

    // List all process_api cgroups
    let cgroups = cgroup::list_process_cgroups(&controller.base_path)
        .await
        .map_err(|e| format!("[DEBUG] Error listing process API cgroups: {e}"))?;

    // Build a PID → cgroup map
    let mut pid_to_cgroup: HashMap<u32, PathBuf> = HashMap::new();
    for cgroup_path in &cgroups {
        match cgroup::read_cgroup_pids(cgroup_path).await {
            Ok(pids) => {
                for pid in pids {
                    pid_to_cgroup.insert(pid, cgroup_path.clone());
                }
            }
            Err(e) => {
                log::debug!("[DEBUG] Error getting PID to cgroup map: {e}");
            }
        }
    }

    // Find PIDs that are children of PID 1 but not in any tracked cgroup
    let known_pids: std::collections::HashSet<u32> = {
        let map = proc_map.lock();
        map.values().map(|e| e.pid).collect()
    };

    for &pid in &child_pids {
        if pid == 1 || known_pids.contains(&pid) {
            continue;
        }

        // Check if this PID is a zombie
        let status_path = format!("/proc/{pid}/status");
        match tokio::fs::read_to_string(&status_path).await {
            Ok(status) => {
                if status.contains("State:\tZ") {
                    // Zombie process - track it for reaping in the next iteration
                    if let Entry::Vacant(e) = tracked_zombies.entry(pid) {
                        log::debug!(
                            "[DEBUG] Found new zombie for tracked orphan (PID {pid}), will reap in next iteration"
                        );
                        e.insert(TrackedZombie {
                            pid,
                            first_seen: Instant::now(),
                        });
                    }
                    continue;
                }

                // Live orphan process - try to adopt it
                if let Some(cgroup_path) = pid_to_cgroup.get(&pid) {
                    log::debug!(
                        "[DEBUG] Found orphan process (PID {pid}) in unattributed cgroup {}",
                        cgroup_path.display()
                    );
                } else {
                    log::debug!("[DEBUG] Found orphan process (PID {pid}) with no cgroup");
                }

                // Attempt to adopt by moving into our cgroup
                match cgroup::add_process_to_cgroup(&controller.base_path, pid).await {
                    Ok(()) => {
                        log::debug!("[DEBUG] Successfully adopted orphan process (PID {pid})");
                    }
                    Err(e) => {
                        log::debug!(
                            "[DEBUG] Failed to adopt orphan process (PID {pid}) in cgroup: {e}"
                        );
                    }
                }
            }
            Err(e) => {
                log::debug!("[DEBUG] Error reading status for orphaned PID {pid}: {e}");
            }
        }
    }

    Ok(())
}
