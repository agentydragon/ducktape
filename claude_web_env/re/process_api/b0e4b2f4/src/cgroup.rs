//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Cgroup v1/v2 setup, memory/cpu controllers.
//!
//! Functions decompiled from:
//!   detect_cgroup_version:       0x1b4f20..0x1b5085
//!   setup_cgroup_path:           0x1b50e0..0x1b54b5
//!   create_process_cgroup:       0x1b54c0..0x1b5616
//!   setup_cgroup:                0x1b5df0..0x1b729c
//!   read_memory_usage (async):   0x1328a0..0x132d66
//!   read_memory_file (async):    0x132d70..0x1331a5

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

/// Decompiled from 0x1b4f20..0x1b5085
/// Xrefs: "/sys/fs/cgroup/cgroup.controllers", "/sys/fs/cgroup/memory",
///   "Cgroup v2 detected but not enabl...", "Could not detect cgroup version"
pub async fn detect_cgroup_version() -> Result<CgroupVersion, String> {
    // Check for cgroup v2 by looking for cgroup.controllers
    if Path::new("/sys/fs/cgroup/cgroup.controllers").exists() {
        return Ok(CgroupVersion::V2);
    }

    // Fall back to cgroup v1 by looking for memory controller
    if Path::new("/sys/fs/cgroup/memory").exists() {
        return Ok(CgroupVersion::V1);
    }

    Err("Could not detect cgroup version".to_string())
}

/// Decompiled from 0x1b50e0..0x1b54b5
/// Xrefs: "/sys/fs/cgroup/memory/process_api", "/sys/fs/cgroup/process_api"
pub async fn setup_cgroup_path(version: CgroupVersion) -> Result<PathBuf, String> {
    let path = match version {
        CgroupVersion::V1 => PathBuf::from("/sys/fs/cgroup/memory/process_api"),
        CgroupVersion::V2 => PathBuf::from("/sys/fs/cgroup/process_api"),
    };

    // Create the cgroup directory if it doesn't exist
    if !path.exists() {
        fs::create_dir_all(&path)
            .await
            .map_err(|e| format!("Failed to create cgroup directory {}: {e}", path.display()))?;
    }

    Ok(path)
}

/// Decompiled from 0x1b54c0..0x1b5616
/// Xrefs: "src/cgroup.rs" (cgroup helper function)
pub async fn create_process_cgroup(
    base_path: &Path,
    pid: u32,
) -> Result<PathBuf, String> {
    let cgroup_path = base_path.join(pid.to_string());

    fs::create_dir_all(&cgroup_path)
        .await
        .map_err(|e| format!("Failed to create process cgroup {}: {e}", cgroup_path.display()))?;

    Ok(cgroup_path)
}

/// Decompiled from 0x1b5df0..0x1b729c
/// Xrefs: "[DEBUG] Detected cgroup version", "[DEBUG] Set process_api cgroup",
///   "[DEBUG] Enabled memory controller", "[DEBUG] Failed to enable controller",
///   "[DEBUG] memory controller already", "[DEBUG] root subtree_control",
///   "[DEBUG] root current controller", "cgroup.subtree_control"
pub async fn setup_cgroup(
    forced_v2: bool,
) -> Result<CgroupController, String> {
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
        if let Ok(current) = fs::read_to_string(&root_subtree).await {
            log::debug!("[DEBUG] root current controller: {current}");
            if !current.contains("memory") {
                log::debug!("[DEBUG] root subtree_control: enabling memory controller");
                if let Err(e) = fs::write(&root_subtree, "+memory").await {
                    log::debug!("[DEBUG] Failed to enable controller: {e}");
                } else {
                    log::debug!("[DEBUG] Enabled memory controller");
                }
            } else {
                log::debug!("[DEBUG] memory controller already enabled");
            }
        }

        // Enable controllers in the process_api subtree
        let pa_subtree = base_path.join("cgroup.subtree_control");
        if let Err(e) = fs::write(&pa_subtree, "+memory +pids").await {
            log::debug!("[DEBUG] Failed to enable controllers in process_api subtree: {e}");
        }
    }

    Ok(CgroupController {
        version,
        base_path,
    })
}

/// Add a process to a cgroup by writing its PID to cgroup.procs.
pub async fn add_process_to_cgroup(
    cgroup_path: &Path,
    pid: u32,
) -> Result<(), String> {
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
pub async fn set_cpu_shares(
    cgroup_path: &Path,
    version: CgroupVersion,
    shares: u64,
) -> Result<(), String> {
    let cpu_file = match version {
        CgroupVersion::V1 => {
            // v1 uses cpu.shares in a separate cpu controller
            PathBuf::from("/sys/fs/cgroup/cpu/cpu.shares")
        }
        CgroupVersion::V2 => cgroup_path.join("cpu.weight"),
    };

    fs::write(&cpu_file, shares.to_string())
        .await
        .map_err(|e| format!("Failed to set CPU shares: {e}"))
}

/// Decompiled from 0x1328a0..0x132d66
/// Xrefs: "Cgroup is not ready Waiting for p...", "memory.usage_in_bytes"
pub async fn read_memory_usage(
    cgroup_path: &Path,
    version: CgroupVersion,
) -> Result<u64, String> {
    let usage_file = match version {
        CgroupVersion::V1 => cgroup_path.join("memory.usage_in_bytes"),
        CgroupVersion::V2 => cgroup_path.join("memory.current"),
    };

    let contents = fs::read_to_string(&usage_file)
        .await
        .map_err(|e| format!("Failed to read memory usage from {}: {e}", usage_file.display()))?;

    contents
        .trim()
        .parse::<u64>()
        .map_err(|e| format!("Failed to parse memory usage '{}': {e}", contents.trim()))
}

/// Remove a process cgroup directory.
pub async fn remove_process_cgroup(cgroup_path: &Path) -> Result<(), String> {
    fs::remove_dir(cgroup_path)
        .await
        .map_err(|e| format!("Failed to remove cgroup {}: {e}", cgroup_path.display()))
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
