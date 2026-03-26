//! Re-verified against 91c789ff (2026-03-26).
//!
//! Cgroup v1/v2 setup, memory/cpu controllers.
//!
//! Functions identified in 91c789ff:
//!   detect_cgroup_version:       0x10f3e0..0x10f557
//!   setup_cgroup_path:           inlined into setup_cgroup
//!   create_process_cgroup:       inlined into callers (e.g., 0x1a9340 area)
//!   setup_cgroup:                0x116640..0x1176a4
//!   read_memory_usage (async):   0x336e0..0x33b50 (state machine), also 0x3b1b0..0x3b5d0
//!   remove_process_cgroup:       inlined into process cleanup at 0x117ac0..0x117e8e
//!
//! New strings in 91c789ff not in b0e4b2f4:
//!   "Memory limit not supported, container memory limits"          @ 0x29c1ec
//!   "Container memory limit not set, ignoring all limits"          @ 0x29c220
//!   "[DEBUG] Error listing process API cgroups: "                  @ 0x292007
//!   "[DEBUG] Error getting PID to cgroup map: "                    @ 0x292037
//!   ") in unattributed cgroup "                                    @ 0x292133
//!   ") in cgroup "                                                 @ 0x292182
//!   "[DEBUG] Restoring cgroup ownership for process id "           @ 0x295ac9
//!   "Failed to create cgroup for process api: "                    @ 0x297b39

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use tokio::fs;

/// Cgroup version detected on the system.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CgroupVersion {
    V1,
    V2,
}

/// Manages a cgroup hierarchy for process_api managed processes.
#[derive(Debug, Clone)]
pub struct CgroupController {
    pub version: CgroupVersion,
    pub base_path: PathBuf,
}

/// Decompiled from 0x10f3e0..0x10f557
/// Xrefs: "/sys/fs/cgroup/cgroup.controllers" @ 0x2a3f08,
///   "/sys/fs/cgroup/memory" @ 0x2a3fa2,
///   "Could not detect cgroup version" @ 0x2a3f29,
///   "Cgroup v2 detected but not enabled..." @ 0x2a3f48
pub async fn detect_cgroup_version() -> Result<CgroupVersion, String> {
    // Check for cgroup v2 by looking for cgroup.controllers
    if Path::new("/sys/fs/cgroup/cgroup.controllers").exists() {
        // Verify controllers are actually available
        match fs::read_to_string("/sys/fs/cgroup/cgroup.controllers").await {
            Ok(controllers) => {
                if controllers.trim().is_empty() {
                    return Err(
                        "Cgroup v2 detected but not enabled. Please use --cgroupv2 flag or ensure controllers are available".to_string()
                    );
                }
                return Ok(CgroupVersion::V2);
            }
            Err(_) => {
                return Ok(CgroupVersion::V2);
            }
        }
    }

    // Fall back to cgroup v1 by looking for memory controller
    if Path::new("/sys/fs/cgroup/memory").exists() {
        return Ok(CgroupVersion::V1);
    }

    Err("Could not detect cgroup version".to_string())
}

/// Read the current cgroup path from /proc/self/cgroup for v2 nested detection.
/// Xrefs: "/proc/self/cgroup", "/core"
async fn detect_v2_cgroup_self_path() -> Option<PathBuf> {
    let contents = fs::read_to_string("/proc/self/cgroup").await.ok()?;
    // v2 format: "0::/path/to/cgroup"
    for line in contents.lines() {
        if line.starts_with("0::") {
            let cgroup_path = line.strip_prefix("0::")?;
            let trimmed = cgroup_path.trim();
            if trimmed != "/" {
                return Some(PathBuf::from("/sys/fs/cgroup").join(trimmed.trim_start_matches('/')));
            }
        }
    }
    None
}

/// Inlined into setup_cgroup in 91c789ff (was 0x1b50e0..0x1b54b5 in b0e4b2f4).
/// Xrefs: "/sys/fs/cgroup/memory/process_api" @ 0x2a3fd1,
///   "/sys/fs/cgroup/process_api" @ 0x2a3fb7,
///   "Direct creation succeeded" @ 0x29c1cb,
///   "Direct creation failed: " @ 0x2915a3,
///   "Failed to create directory: " @ 0x2915c0,
///   "mkdir-p" @ 0x29c1e5
pub async fn setup_cgroup_path(version: CgroupVersion) -> Result<PathBuf, String> {
    let base = match version {
        CgroupVersion::V1 => PathBuf::from("/sys/fs/cgroup/memory"),
        CgroupVersion::V2 => {
            // Check for nested cgroup v2 (e.g., in systemd-managed containers)
            if let Some(self_path) = detect_v2_cgroup_self_path().await {
                self_path
            } else {
                PathBuf::from("/sys/fs/cgroup")
            }
        }
    };

    let path = base.join("process_api");

    // Create the cgroup directory if it doesn't exist
    if !path.exists() {
        // Try direct creation first
        match fs::create_dir_all(&path).await {
            Ok(()) => {
                log::debug!("Direct creation succeeded: {}", path.display());
            }
            Err(e) => {
                // Fallback: try mkdir -p via subprocess
                log::debug!("Direct creation failed: mkdir-p: {}", path.display());
                let output = tokio::process::Command::new("mkdir")
                    .arg("-p")
                    .arg(path.as_os_str())
                    .output()
                    .await
                    .map_err(|e2| {
                        format!(
                            "Failed to create directory: {} (direct: {e}, mkdir: {e2})",
                            path.display()
                        )
                    })?;
                if !output.status.success() {
                    return Err(format!(
                        "Failed to create directory: {} (direct: {e}, mkdir exit: {})",
                        path.display(),
                        output.status
                    ));
                }
            }
        }
    }

    Ok(path)
}

/// Inlined into callers in 91c789ff (was 0x1b54c0..0x1b5616 in b0e4b2f4).
/// Xrefs: "Failed to create cgroup for process api: " @ 0x297b39
pub async fn create_process_cgroup(base_path: &Path, pid: u32) -> Result<PathBuf, String> {
    let cgroup_path = base_path.join(pid.to_string());

    fs::create_dir_all(&cgroup_path).await.map_err(|e| {
        format!(
            "Failed to create process cgroup {}: {e}",
            cgroup_path.display()
        )
    })?;

    Ok(cgroup_path)
}

/// Helper to enable a controller in a cgroup's subtree_control.
/// Xrefs: "controller already enabled in root cgroup",
///   "controller in root cgroup"
async fn enable_controller_in_subtree(
    subtree_path: &Path,
    controller: &str,
    label: &str,
) -> Result<(), String> {
    if let Ok(current) = fs::read_to_string(subtree_path).await {
        log::debug!("[DEBUG] {label}: current_controllers: {}", current.trim());
        if current.contains(controller) {
            log::debug!("[DEBUG] {controller} controller already enabled in {label}");
            return Ok(());
        }
    }

    let enable_str = format!("+{controller}");
    match fs::write(subtree_path, &enable_str).await {
        Ok(()) => {
            log::debug!("[DEBUG] Enabled {controller} controller in {label}");
            Ok(())
        }
        Err(e) => {
            log::debug!("[DEBUG] Failed to enable {controller} controller in {label}: {e}");
            Err(format!("Failed to enable {controller} in {label}: {e}"))
        }
    }
}

/// Decompiled from 0x116640..0x1176a4
/// Xrefs: "[DEBUG] Detected cgroup version: " @ 0x2950da,
///   "[DEBUG] Set process_api/cgroup.procs permissions to 0o666" @ 0x2a3ff2,
///   "[DEBUG] Failed to set permissions on process_api/cgroup.procs: " @ 0x295137,
///   "[DEBUG] Enabled memory controller in process_api cgroup" @ 0x2a402c,
///   "[DEBUG] memory controller already enabled in process_api cgroup" @ 0x2a4064,
///   "[DEBUG] Failed to enable memory controller in process_api cgroup: " @ 0x295226,
///   "[DEBUG] root: subtree_control: " @ 0x2951ac,
///   "controller in root cgroup" @ 0x295286,
///   "controller in root cgroup: " @ 0x2952be,
///   "controller already enabled in root cgroup" @ 0x2952e8,
///   "/cgroup.subtree_control" @ 0x29517d,
///   "/cgroup.controllers" @ 0x295197
pub async fn setup_cgroup(forced_v2: bool) -> Result<CgroupController, String> {
    let version = if forced_v2 {
        log::debug!("Forced cgroup v2 mode");
        CgroupVersion::V2
    } else {
        let v = detect_cgroup_version().await?;
        log::debug!("Detected cgroup version: {v:?}");
        v
    };

    let base_path = setup_cgroup_path(version).await?;
    log::debug!("[DEBUG] Set process_api cgroup: {}", base_path.display());

    if version == CgroupVersion::V2 {
        // Enable memory controller in the root cgroup's subtree_control
        let root_subtree = PathBuf::from("/sys/fs/cgroup/cgroup.subtree_control");
        let _ = enable_controller_in_subtree(&root_subtree, "memory", "root cgroup").await;

        // Enable controllers in the process_api subtree
        let pa_subtree = base_path.join("cgroup.subtree_control");
        if let Ok(current) = fs::read_to_string(&pa_subtree).await {
            log::debug!(
                "[DEBUG] process_api: current_controllers: {}",
                current.trim()
            );
        }

        // Enable memory in process_api subtree
        // Binary writes "+memory\n" (8 bytes at 0x288028), not "+memory +pids"
        match fs::write(&pa_subtree, "+memory\n").await {
            Ok(()) => {
                log::debug!("[DEBUG] Enabled memory controller in process_api cgroup");
            }
            Err(e) => {
                log::debug!(
                    "[DEBUG] Failed to enable memory controller in process_api cgroup: {e}"
                );
            }
        }
    }

    // Set cgroup.procs permissions to 0o666 so unprivileged processes can be moved
    let procs_path = base_path.join("cgroup.procs");
    match std::fs::set_permissions(&procs_path, std::fs::Permissions::from_mode(0o666)) {
        Ok(()) => {
            log::debug!("[DEBUG] Set process_api/cgroup.procs permissions to 0o666");
        }
        Err(e) => {
            log::debug!("[DEBUG] Failed to set permissions on process_api/cgroup.procs: {e}");
        }
    }

    // Move current process (PID 1) into the cgroup
    let my_pid = std::process::id();
    match fs::write(&procs_path, my_pid.to_string()).await {
        Ok(()) => {
            log::debug!(
                "[DEBUG] Moved current process (PID {my_pid}) to {}",
                base_path.display()
            );
        }
        Err(e) => {
            log::debug!("[DEBUG] Failed to move current process (PID {my_pid}) to cgroup: {e}");
        }
    }

    Ok(CgroupController { version, base_path })
}

/// Add a process to a cgroup by writing its PID to cgroup.procs.
pub async fn add_process_to_cgroup(cgroup_path: &Path, pid: u32) -> Result<(), String> {
    let procs_path = cgroup_path.join("cgroup.procs");
    fs::write(&procs_path, pid.to_string())
        .await
        .map_err(|e| format!("Failed to add PID {pid} to cgroup: {e}"))
}

/// Set memory limit for a cgroup.
pub async fn set_memory_limit(
    cgroup_path: &Path,
    version: CgroupVersion,
    limit_bytes: u64,
) -> Result<(), String> {
    let limit_file = match version {
        CgroupVersion::V1 => cgroup_path.join("memory.limit_in_bytes"),
        CgroupVersion::V2 => cgroup_path.join("memory.max"),
    };

    fs::write(&limit_file, limit_bytes.to_string())
        .await
        .map_err(|e| format!("Failed to set memory limit: {e}"))
}

/// Set CPU shares/weight for a cgroup.
/// Inlined into setup_cgroup at 0x11741a..0x117570 area.
/// Xrefs: "cpu.weight" @ 0x2a3ec8, "cpu.shares" @ 0x2a3ebe,
///   "/sys/fs/cgroup/cpu,cpuacct" @ 0x2aab1c, "/sys/fs/cgroup/cpu" @ 0x294d0d
pub async fn set_cpu_shares(
    cgroup_path: &Path,
    version: CgroupVersion,
    shares: u64,
) -> Result<(), String> {
    let cpu_file = match version {
        CgroupVersion::V1 => {
            // v1: try cpu,cpuacct combined controller first, then cpu
            let combined = PathBuf::from("/sys/fs/cgroup/cpu,cpuacct/cpu.shares");
            if combined.exists() {
                combined
            } else {
                PathBuf::from("/sys/fs/cgroup/cpu/cpu.shares")
            }
        }
        CgroupVersion::V2 => cgroup_path.join("cpu.weight"),
    };

    fs::write(&cpu_file, shares.to_string())
        .await
        .map_err(|e| format!("Failed to set CPU shares: {e}"))
}

/// Async state machine at 0x336e0..0x33b50 and 0x3b1b0..0x3b5d0
/// Xrefs: "memory.usage_in_bytes" @ 0x2a3ed2, "memory.current" @ 0x2a3ee7,
///   "Cgroup is not ready" @ 0x2a3ef5
pub async fn read_memory_usage(cgroup_path: &Path, version: CgroupVersion) -> Result<u64, String> {
    let usage_file = match version {
        CgroupVersion::V1 => cgroup_path.join("memory.usage_in_bytes"),
        CgroupVersion::V2 => cgroup_path.join("memory.current"),
    };

    let contents = fs::read_to_string(&usage_file).await.map_err(|e| {
        format!(
            "Failed to read memory usage from {}: {e}",
            usage_file.display()
        )
    })?;

    contents
        .trim()
        .parse::<u64>()
        .map_err(|e| format!("Failed to parse memory usage '{}': {e}", contents.trim()))
}

/// Remove a process cgroup directory.
/// Part of process cleanup function at 0x117ac0..0x117e8e.
/// Xrefs: "Removed cgroup directory" @ 0x295450,
///   "Failed to remove cgroup directory: " @ 0x29541c (actually 0x295412 in 91c789ff)
pub async fn remove_process_cgroup(cgroup_path: &Path) -> Result<(), String> {
    match fs::remove_dir(cgroup_path).await {
        Ok(()) => {
            log::debug!("Removed cgroup directory: {}", cgroup_path.display());
            Ok(())
        }
        Err(e) => {
            log::debug!(
                "Failed to remove cgroup directory: {}: {e}",
                cgroup_path.display()
            );
            Err(format!(
                "Failed to remove cgroup {}: {e}",
                cgroup_path.display()
            ))
        }
    }
}

/// List all process cgroup directories under the base path.
pub async fn list_process_cgroups(base_path: &Path) -> Result<Vec<PathBuf>, String> {
    let mut cgroups = Vec::new();
    let mut entries = fs::read_dir(base_path)
        .await
        .map_err(|e| format!("Failed to list cgroups: {e}"))?;

    while let Some(entry) = entries
        .next_entry()
        .await
        .map_err(|e| format!("Failed to read cgroup entry: {e}"))?
    {
        let path = entry.path();
        if path.is_dir() {
            // Only include numeric-named directories (PID-based cgroups)
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.parse::<u32>().is_ok() {
                    cgroups.push(path);
                }
            }
        }
    }

    Ok(cgroups)
}

/// Read PIDs from a cgroup's cgroup.procs file.
pub async fn read_cgroup_pids(cgroup_path: &Path) -> Result<Vec<u32>, String> {
    let procs_path = cgroup_path.join("cgroup.procs");
    let contents = fs::read_to_string(&procs_path)
        .await
        .map_err(|e| format!("Failed to read cgroup.procs: {e}"))?;

    let mut pids = Vec::new();
    for line in contents.lines() {
        if let Ok(pid) = line.trim().parse::<u32>() {
            pids.push(pid);
        }
    }

    Ok(pids)
}
