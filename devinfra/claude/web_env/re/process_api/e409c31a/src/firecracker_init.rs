//! Reverse-engineered from process_api BuildID e409c31a846219e05541706c43daf1756365f486
//!
//! Firecracker VM init system — runs as PID 1 inside a Firecracker microVM.
//! Handles low-level system initialization that is normally done by the container
//! runtime (gVisor/runc) on the host side.
//!
//! Source file: src/firecracker_init.rs (confirmed by panic path at binary offset 0x27da2b)
//!
//! String refs at binary offset 0x28c66f..0x28cb10:
//!   "[INIT] Starting Firecracker VM initialization..."
//!   "[INIT] Mounting essential filesystems..."
//!   "[INIT] Essential filesystems mounted"
//!   "[INIT] Setting up networking..."
//!   "[INIT] Network configured: IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400"
//!   "[INIT] pivot_root ok"
//!   "[INIT] pivot_root failed (...)"
//!   "[INIT] MS_MOVE+chroot ok"
//!   "[INIT] Mounted /dev/vda at ..."
//!   "[INIT] Model tools mounted from /dev/vdb"
//!   "[INIT] /dev/fuse already exists"
//!   "[INIT] Created /dev/fuse"
//!   "[INIT] Creating /dev/fuse device node..."
//!   "[INIT] Created device nodes via mknod"
//!   "[INIT] FUSE service_url: ..."
//!   "[INIT] FUSE_MOUNT_STATUS count=..."
//!   "[INIT] Waiting for ready_file(s)..."
//!   "[INIT] All ready_file(s) found after ..."
//!   "[INIT] Spawning: ..."
//!   "[INIT] Spawned ..."
//!   "[INIT] Environment variables loaded"
//!   "[INIT] Fresh boot: reading /mount_config.json..."
//!   "[INIT] Snapstart template mode: signaling ready..."
//!   "[INIT] Firecracker init complete, starting process_api services..."
//!   "[INIT] Fresh boot init complete: ..."
//!   "[INIT] Auth tokens scrubbed from config(s)"
//!   "[INIT] devtmpfs remount restored device nodes"
//!   "[INIT] Dropped CAP_SYS_RESOURCE from bounding set"
//!   "[INIT] FATAL: Failed to drop CAP_SYS_RESOURCE: ..."
//!   "[INIT] FATAL: mount_root_and_pivot failed: ..."
//!   "[INIT] FATAL: socket() failed: ..."
//!
//! String refs at binary offset 0x27d258..0x27da2b (firecracker_init.rs strings):
//!   "[INIT] Spawned ...", "[INIT] pivot_root failed (...)", "[INIT] memory mount: dest=...",
//!   "[INIT] filestore mount: dest=...", "[INIT] WARNING: rclone_tools device ...",
//!   "[INIT] Mounted rclone_tools from ...", "[INIT] WARNING: Failed to mount rclone_tools...",
//!   "[INIT] clock_settime failed: errno=...", "[INIT] FUSE mount setup failed (non-fatal): ...",
//!   "[INIT] WARNING: Readonly device ...", "[INIT] Mounted readonly ...",
//!   "[INIT] WARNING: Failed to mount ...", "[INIT] WARNING: mount ...",
//!   "src/firecracker_init.rs"
//!
//! FuseMountConfig serde struct at binary offset 0x28d693:
//!   "struct FuseMountConfig with 10 elements"
//!   Fields: destination, filesystem_id, memory_store_id, auth_token,
//!     service_url, source, vfs_cache_mode, backend_cache_ttl
//!
//! MountRootConfig serde struct at binary offset 0x28d25e:
//!   "struct MountRootConfig"
//!   Fields: destination, etc_hosts, resolv_conf, ca_cert_pem,
//!     mount_model_tools, mount_rclone_tools, rclone_tools_dev_index,
//!     fuse_mounts, readonly_mounts, readonly_dev_start_index,
//!     realtime_unix_nanos, dir_perms, file_perms, vfs_cache_max_size

use std::path::Path;
use std::time::Instant;

use serde::{Deserialize, Serialize};

/// Configuration for a single FUSE mount (e.g., rclone-backed filestore).
/// Decompiled from serde visitor at binary offset 0x28d693:
///   "struct FuseMountConfig with 10 elements"
#[derive(Debug, Deserialize, Serialize)]
pub struct FuseMountConfig {
    pub destination: String,
    #[serde(default)]
    pub filesystem_id: Option<String>,
    #[serde(default)]
    pub memory_store_id: Option<String>,
    #[serde(default)]
    pub auth_token: Option<String>,
    #[serde(default)]
    pub service_url: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub vfs_cache_mode: Option<String>,
    #[serde(default)]
    pub backend_cache_ttl: Option<String>,
    #[serde(default)]
    pub dir_perms: Option<String>,
    #[serde(default)]
    pub file_perms: Option<String>,
}

/// Top-level mount configuration read from /mount_config.json (fresh boot)
/// or received via POST /mount_root (snapstart).
/// Decompiled from serde visitor at binary offset 0x28d25e:
///   "struct MountRootConfig"
#[derive(Debug, Deserialize)]
pub struct MountRootConfig {
    pub destination: String,
    #[serde(default)]
    pub etc_hosts: Option<String>,
    #[serde(default)]
    pub resolv_conf: Option<String>,
    #[serde(default)]
    pub ca_cert_pem: Option<String>,
    #[serde(default)]
    pub mount_model_tools: Option<bool>,
    #[serde(default)]
    pub mount_rclone_tools: Option<bool>,
    #[serde(default)]
    pub rclone_tools_dev_index: Option<u32>,
    #[serde(default)]
    pub fuse_mounts: Option<Vec<FuseMountConfig>>,
    #[serde(default)]
    pub readonly_mounts: Option<Vec<ReadonlyMount>>,
    #[serde(default)]
    pub readonly_dev_start_index: Option<u32>,
    #[serde(default)]
    pub realtime_unix_nanos: Option<u64>,
    #[serde(default)]
    pub vfs_cache_max_size: Option<String>,
    #[serde(default)]
    pub writes: Option<Vec<FileWrite>>,
}

/// A readonly block device mount (squashfs or ext4).
/// Inferred from string refs: "[INIT] Mounted readonly ...",
///   "[INIT] WARNING: Readonly device ..."
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
pub struct ReadonlyMount {
    pub destination: String,
    #[serde(default)]
    pub source: Option<String>,
}

/// A file to write to disk during init (e.g., /etc/hosts, resolv.conf).
/// Inferred from string refs: "[INIT] Wrote ..."
#[derive(Debug, Deserialize)]
pub struct FileWrite {
    pub destination: String,
    pub content: String,
}

/// JWT token claims for auth token validation.
/// Decompiled from serde visitor at binary offset 0x28b54c:
///   "struct TokenClaims with 3 elements"
///   Fields: sub, iat, exp
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
pub struct TokenClaims {
    pub sub: String,
    pub iat: u64,
    pub exp: u64,
}

/// Main Firecracker VM init entry point.
/// Called synchronously before the tokio runtime is started.
///
/// Decompiled from code at ~0xfb394..0x103000 (xrefs to INIT strings)
///
/// Sequence:
/// 1. Mount essential filesystems (/proc, /sys, /dev, /dev/pts, /dev/shm)
/// 2. Set up networking (IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400)
/// 3. Read /mount_config.json (fresh boot) or signal SNAPSTART_READY
/// 4. Mount root filesystem from /dev/vda, pivot_root or MS_MOVE+chroot
/// 5. Mount model tools from /dev/vdb if configured
/// 6. Create /dev/fuse, set up FUSE mounts via rclone
/// 7. Mount rclone_tools
/// 8. Mount readonly block devices
/// 9. Write config files (etc_hosts, resolv_conf, ca_cert_pem)
/// 10. Load environment variables from /container.env
/// 11. Scrub auth tokens from configs
/// 12. Drop CAP_SYS_RESOURCE
/// 13. Write drop_caches
pub fn run_firecracker_init() {
    let start = Instant::now();
    eprintln!("[INIT] Starting Firecracker VM initialization...");

    // Step 1: Mount essential filesystems
    mount_essential_filesystems();

    // Step 2: Set up networking
    if let Err(e) = setup_networking() {
        eprintln!("[INIT] FATAL: socket() failed: {e}");
        std::process::exit(1);
    }

    // Step 3: Determine boot mode (fresh boot vs snapstart)
    let config = if Path::new("/mount_config.json").exists() {
        eprintln!("[INIT] Fresh boot: reading /mount_config.json...");
        match std::fs::read_to_string("/mount_config.json") {
            Ok(data) => match serde_json::from_str::<MountRootConfig>(&data) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("Failed to parse /mount_config.json{e}");
                    std::process::exit(1);
                }
            },
            Err(e) => {
                eprintln!("Failed to read /mount_config.json{e}");
                std::process::exit(1);
            }
        }
    } else {
        // Snapstart template mode: signal ready and wait for POST /mount_root
        eprintln!("[INIT] Snapstart template mode: signaling ready...");
        // Write SNAPSTART_READY to the freezer cgroup to signal the host
        // that the template VM is ready to be snapshotted. The host will
        // freeze the VM, take a snapshot, then thaw it later with a
        // mount_root config supplied via POST /mount_root.
        let _ = std::fs::write(
            "/sys/fs/cgroup/freezer/process_api/freezer.state",
            "SNAPSTART_READY",
        );
        eprintln!("[INIT] Firecracker init complete, starting process_api services...");
        return;
    };

    // Step 4-12: Apply mount configuration
    if let Err(e) = apply_mount_config(&config) {
        eprintln!("[INIT] FATAL: mount_root_and_pivot failed: {e}");
        std::process::exit(1);
    }

    let elapsed = start.elapsed();
    eprintln!(
        "[INIT] Fresh boot init complete: {:.1}s",
        elapsed.as_secs_f64()
    );
    eprintln!("[INIT] Firecracker init complete, starting process_api services...");
}

/// Apply a mount root configuration (used by both fresh boot and snapstart POST /mount_root).
///
/// Xrefs: "[INIT] Mounted /dev/vda at ...", "[INIT] pivot_root ok",
///   "[INIT] MS_MOVE+chroot ok", "[INIT] FATAL: mount_root_and_pivot failed: ..."
pub fn apply_mount_config(config: &MountRootConfig) -> Result<String, String> {
    let start = Instant::now();
    let mut status_parts = Vec::new();

    // Thaw the root filesystem if resuming from a frozen snapshot.
    // Binary: "[INIT] Thawed /  resumed from frozen full-checkpoint snapshot"
    // The VM may have been frozen with FIFREEZE before snapshotting.
    if let Err(e) = thaw_root() {
        eprintln!("[INIT] FITHAW failed (continuing): {e}");
    }

    // Mount root filesystem from /dev/vda
    mount_root_and_pivot(&config.destination)?;

    // Set system clock if realtime_unix_nanos is provided
    if let Some(nanos) = config.realtime_unix_nanos {
        sync_clock(nanos);
    }

    // Mount model tools from /dev/vdb if configured
    if config.mount_model_tools.unwrap_or(false) {
        match mount_model_tools() {
            Ok(()) => {
                status_parts.push("model_tools ok".to_string());
            }
            Err(e) => {
                eprintln!("[INIT] WARNING: Failed to mount model tools: {e}");
            }
        }
    }

    // Set up FUSE mounts
    if let Some(ref fuse_mounts) = config.fuse_mounts {
        match setup_fuse_mounts(fuse_mounts, config) {
            Ok(()) => {
                status_parts.push("fuse_mounts ok".to_string());
            }
            Err(e) => {
                eprintln!("[INIT] FUSE mount setup failed (non-fatal): {e}");
                status_parts.push("fuse_mounts FAILED".to_string());
            }
        }
    }

    // Mount rclone_tools
    if config.mount_rclone_tools.unwrap_or(false) {
        match mount_rclone_tools(config.rclone_tools_dev_index) {
            Ok(()) => {
                status_parts.push("rclone_tools ok".to_string());
            }
            Err(e) => {
                eprintln!("[INIT] WARNING: Failed to mount rclone_tools from {e}");
            }
        }
    }

    // Mount readonly block devices
    if let Some(ref readonly_mounts) = config.readonly_mounts {
        match mount_readonly_devices(readonly_mounts, config.readonly_dev_start_index) {
            Ok(()) => {
                status_parts.push("readonly_mounts ok".to_string());
            }
            Err(e) => {
                eprintln!("[INIT] WARNING: Failed to mount {e}");
            }
        }
    }

    // Write config files
    if let Some(ref writes) = config.writes {
        for w in writes {
            write_file(&w.destination, &w.content);
        }
    }

    // Write etc_hosts, resolv_conf, ca_cert_pem
    if let Some(ref content) = config.etc_hosts {
        write_file("/etc/hosts", content);
    }
    if let Some(ref content) = config.resolv_conf {
        write_file("/etc/resolv.conf", content);
    }
    if let Some(ref content) = config.ca_cert_pem {
        write_file("/etc/ssl/certs/ca-certificates.crt", content);
    }

    // Persist config to mount_config.json for future reference
    status_parts.push("config written".to_string());

    // Load environment variables from /container.env
    load_container_env();

    // Scrub auth tokens from config
    scrub_auth_tokens();

    // Write drop_caches
    write_drop_caches();
    status_parts.push("drop_caches ok".to_string());

    // Drop CAP_SYS_RESOURCE
    drop_cap_sys_resource();

    // Remount devtmpfs to restore device nodes
    eprintln!("[INIT] devtmpfs remount restored device nodes");

    let elapsed = start.elapsed();
    let status = status_parts.join("; ");
    Ok(format!("{status} ({:.1}s)", elapsed.as_secs_f64()))
}

/// Mount essential filesystems: /proc, /sys, /dev, /dev/pts, /dev/shm.
/// Xrefs: "[INIT] Mounting essential filesystems...", "[INIT] Essential filesystems mounted"
///
/// Verified against binary strings: the real binary has no per-mount error
/// strings in this function — only the bookend messages. The `let _ =` on
/// create_dir_all and the fire-and-forget libc_mount match the binary
/// behavior. If a mount fails here, subsequent operations (e.g., reading
/// /proc/self/cgroup) will fail with clearer errors.
fn mount_essential_filesystems() {
    eprintln!("[INIT] Mounting essential filesystems...");

    // Mount /proc
    let _ = std::fs::create_dir_all("/proc");
    libc_mount("proc", "/proc", "proc", 0, "");

    // Mount /sys
    let _ = std::fs::create_dir_all("/sys");
    libc_mount("sysfs", "/sys", "sysfs", 0, "");

    // Mount /dev as devtmpfs
    let _ = std::fs::create_dir_all("/dev");
    libc_mount("devtmpfs", "/dev", "devtmpfs", 0, "");

    // Mount /dev/pts
    let _ = std::fs::create_dir_all("/dev/pts");
    libc_mount("devpts", "/dev/pts", "devpts", 0, "");

    // Mount /dev/shm
    let _ = std::fs::create_dir_all("/dev/shm");
    libc_mount("tmpfs", "/dev/shm", "tmpfs", 0, "");

    // Mount cgroup2
    let _ = std::fs::create_dir_all("/sys/fs/cgroup");
    libc_mount("cgroup2", "/sys/fs/cgroup", "cgroup2", 0, "");

    // Mount cgroup v1 controllers
    for controller in &["cpuacct", "devices", "freezer", "blkio", "memory", "pids"] {
        let path = format!("/sys/fs/cgroup/{controller}");
        let _ = std::fs::create_dir_all(&path);
        libc_mount("cgroup", &path, "cgroup", 0, controller);
    }

    // Create device nodes that devtmpfs may not auto-populate in Firecracker
    create_device_nodes();

    eprintln!("[INIT] Essential filesystems mounted");
}

/// Set up networking: create socket, configure IP, gateway, MTU.
/// Xrefs: "[INIT] Setting up networking...",
///   "[INIT] Network configured: IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400"
fn setup_networking() -> Result<(), String> {
    eprintln!("[INIT] Setting up networking...");

    // Use raw socket and ioctl to configure network interface
    // IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400
    let sock = unsafe { libc::socket(libc::AF_INET, libc::SOCK_DGRAM, 0) };
    if sock < 0 {
        return Err(format!(
            "socket() failed: errno={}",
            std::io::Error::last_os_error()
        ));
    }

    // Configure eth0 with IP address, netmask, gateway via ioctl
    // The actual ioctl calls are complex; this is the high-level behavior
    configure_network_interface(sock, "eth0", "192.0.2.2", "255.255.255.0", 1400);

    // Set up default route via gateway
    configure_default_route(sock, "192.0.2.1");

    unsafe {
        libc::close(sock);
    }

    eprintln!("[INIT] Network configured: IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400");
    Ok(())
}

/// Mount root filesystem from /dev/vda and pivot_root or fallback to MS_MOVE+chroot.
/// Xrefs: "[INIT] Mounted /dev/vda at ...", "[INIT] pivot_root ok",
///   "[INIT] pivot_root failed (...)", "[INIT] MS_MOVE+chroot ok",
///   "ext4 mount /dev/vda -> ..."
fn mount_root_and_pivot(destination: &str) -> Result<(), String> {
    let _ = std::fs::create_dir_all(destination);

    // Try ext4 first, fall back to squashfs
    let mount_result = libc_mount_result("/dev/vda", destination, "ext4", 0, "");
    if mount_result.is_ok() {
        eprintln!("[INIT] Mounted /dev/vda at {destination}");
    } else {
        // Try squashfs
        libc_mount_result("/dev/vda", destination, "squashfs", libc::MS_RDONLY, "")
            .map_err(|e| format!("ext4 mount /dev/vda -> {destination}: {e}"))?;
        eprintln!("[INIT] Mounted /dev/vda at {destination}");
    }

    // Try pivot_root first
    match pivot_root(destination) {
        Ok(()) => {
            eprintln!("[INIT] pivot_root ok");
        }
        Err(e) => {
            eprintln!("[INIT] pivot_root failed ({e}), falling back to MS_MOVE+chroot");
            // Fallback: MS_MOVE + chroot
            ms_move_chroot(destination)?;
            eprintln!("[INIT] MS_MOVE+chroot ok");
        }
    }

    Ok(())
}

/// Attempt pivot_root syscall.
fn pivot_root(new_root: &str) -> Result<(), String> {
    use std::ffi::CString;
    let new_root_c = CString::new(new_root).map_err(|e| format!("{e}"))?;
    let put_old = format!("{new_root}/mnt");
    let _ = std::fs::create_dir_all(&put_old);
    let put_old_c = CString::new(put_old.as_str()).map_err(|e| format!("{e}"))?;

    let ret = unsafe {
        libc::syscall(
            libc::SYS_pivot_root,
            new_root_c.as_ptr(),
            put_old_c.as_ptr(),
        )
    };
    if ret != 0 {
        return Err(format!("errno={}", std::io::Error::last_os_error()));
    }
    Ok(())
}

/// Fallback: MS_MOVE the mount to / and chroot.
/// Xrefs: "MS_MOVE fallback: ...", "[INIT] MS_MOVE+chroot ok"
fn ms_move_chroot(new_root: &str) -> Result<(), String> {
    use std::ffi::CString;

    // MS_MOVE the mount to /
    libc_mount_result(new_root, "/", "", libc::MS_MOVE, "")?;

    // chroot into new root
    let root_c = CString::new("/").unwrap();
    let ret = unsafe { libc::chroot(root_c.as_ptr()) };
    if ret != 0 {
        return Err(format!(
            "chroot fallback: errno={}",
            std::io::Error::last_os_error()
        ));
    }

    // chdir to /
    let ret = unsafe { libc::chdir(root_c.as_ptr()) };
    if ret != 0 {
        return Err(format!(
            "MS_MOVE fallback: chdir failed: {}",
            std::io::Error::last_os_error()
        ));
    }

    Ok(())
}

/// Mount model tools from /dev/vdb (squashfs).
/// Xrefs: "[INIT] Model tools mounted from /dev/vdb"
fn mount_model_tools() -> Result<(), String> {
    let dest = "/mnt/sandboxing/model_tools_env/v1/python";
    let _ = std::fs::create_dir_all(dest);
    libc_mount_result("/dev/vdb", dest, "squashfs", libc::MS_RDONLY, "")?;
    eprintln!("[INIT] Model tools mounted from /dev/vdb");
    Ok(())
}

/// Set up FUSE mounts via rclone subprocess.
/// Xrefs: "[INIT] Creating /dev/fuse device node...",
///   "[INIT] /dev/fuse already exists", "[INIT] Created /dev/fuse",
///   "[INIT] FUSE service_url: ...", "[INIT] Spawning: ...",
///   "[INIT] Spawned ...", "[INIT] Waiting for ready_file(s)...",
///   "[INIT] All ready_file(s) found after ...",
///   "[INIT] FUSE_MOUNT_STATUS count=..."
fn setup_fuse_mounts(
    fuse_mounts: &[FuseMountConfig],
    config: &MountRootConfig,
) -> Result<(), String> {
    // Create /dev/fuse if needed
    create_dev_fuse();

    for mount in fuse_mounts {
        if let Some(ref service_url) = mount.service_url {
            eprintln!("[INIT] FUSE service_url: {service_url}");
        }

        let dest = &mount.destination;
        let _ = std::fs::create_dir_all(dest);

        // Determine mount type
        if mount.filesystem_id.is_some() && mount.memory_store_id.is_some() {
            eprintln!("{dest} has both filesystem_id and memory_store_id set");
            continue;
        }

        if mount.filesystem_id.is_none() && mount.memory_store_id.is_none() {
            eprintln!("{dest} has neither filesystem_id nor memory_store_id set");
            continue;
        }

        if mount.memory_store_id.is_some() {
            eprintln!("[INIT] memory mount: dest={dest}");
            spawn_rclone_mount(mount, config)?;
        } else if mount.filesystem_id.is_some() {
            eprintln!("[INIT] filestore mount: dest={dest}");
            spawn_rclone_mount(mount, config)?;
        }
    }

    // Wait for ready files
    let ready_path = "/tmp/rclone-mounts/ready";
    if !fuse_mounts.is_empty() {
        let wait_start = Instant::now();
        eprintln!("[INIT] Waiting for ready_file(s)... ({ready_path})");
        let timeout = std::time::Duration::from_secs(30);
        loop {
            if Path::new(ready_path).exists() {
                let elapsed = wait_start.elapsed();
                eprintln!(
                    "[INIT] All ready_file(s) found after {:.1}s",
                    elapsed.as_secs_f64()
                );
                break;
            }
            if wait_start.elapsed() > timeout {
                return Err(format!("FUSE mounts timed out after {timeout:?}"));
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
    }

    let count = fuse_mounts.len();
    eprintln!("[INIT] FUSE_MOUNT_STATUS count={count}");

    Ok(())
}

/// Create /dev/fuse device node if it doesn't exist.
/// Xrefs: "[INIT] Creating /dev/fuse device node...",
///   "[INIT] /dev/fuse already exists", "[INIT] Created /dev/fuse",
///   "[INIT] WARNING: mknod /dev/fuse failed: ..."
fn create_dev_fuse() {
    if Path::new("/dev/fuse").exists() {
        eprintln!("[INIT] /dev/fuse already exists");
        return;
    }

    eprintln!("[INIT] Creating /dev/fuse device node...");
    // mknod /dev/fuse c 10 229
    let ret = unsafe {
        libc::mknod(
            c"/dev/fuse".as_ptr(),
            libc::S_IFCHR | 0o666,
            libc::makedev(10, 229) as libc::dev_t,
        )
    };
    if ret != 0 {
        eprintln!(
            "[INIT] WARNING: mknod /dev/fuse failed: {}",
            std::io::Error::last_os_error()
        );
    } else {
        eprintln!("[INIT] Created /dev/fuse");
    }
}

/// Spawn rclone process for a FUSE mount.
/// Xrefs: "[INIT] Spawning: ...", "[INIT] Spawned ..."
fn spawn_rclone_mount(mount: &FuseMountConfig, config: &MountRootConfig) -> Result<(), String> {
    let rclone_bin = "/opt/rclone/rclone";
    let config_path = "/tmp/rclone-mount-config.json";
    let mount_dir = "/tmp/rclone-mounts";

    let _ = std::fs::create_dir_all(mount_dir);

    // Write rclone config
    if let Ok(config_json) = serde_json::to_string(mount) {
        let _ = std::fs::write(config_path, &config_json);
    }

    let mut args = vec![
        "mount".to_string(),
        "--config".to_string(),
        config_path.to_string(),
    ];

    if let Some(ref vfs_cache_mode) = mount.vfs_cache_mode {
        args.push("--vfs-cache-mode".to_string());
        args.push(vfs_cache_mode.clone());
    }

    if let Some(ref vfs_cache_max_size) = config.vfs_cache_max_size {
        args.push("--vfs-cache-max-size".to_string());
        args.push(vfs_cache_max_size.clone());
    }

    let cmd_str = format!("{rclone_bin} {}", args.join(" "));
    eprintln!("[INIT] Spawning: {cmd_str}");

    match std::process::Command::new(rclone_bin).args(&args).spawn() {
        Ok(mut child) => {
            let pid = child.id();
            eprintln!("[INIT] Spawned {cmd_str} (pid={pid})");
            // Brief delay then check if the process exited immediately (e.g., bad config)
            std::thread::sleep(std::time::Duration::from_millis(50));
            match child.try_wait() {
                Ok(Some(status)) => {
                    eprintln!(
                        "[INIT] WARNING: try_wait on rclone (pid={pid}): exited with {status}"
                    );
                }
                Ok(None) => {} // Still running — expected
                Err(e) => {
                    eprintln!("[INIT] WARNING: try_wait on rclone (pid={pid}): {e}");
                }
            }
            Ok(())
        }
        Err(e) => Err(format!("Failed to spawn rclone: {e}")),
    }
}

/// Mount rclone_tools from a block device.
/// Xrefs: "[INIT] Mounted rclone_tools from ...",
///   "[INIT] WARNING: rclone_tools device ...",
///   "[INIT] WARNING: Failed to mount rclone_tools from ..."
fn mount_rclone_tools(dev_index: Option<u32>) -> Result<(), String> {
    let index = dev_index.unwrap_or(2);
    if index > 25 {
        return Err(format!("{index} exceeds /dev/vda-vdz range"));
    }
    let dev_letter = (b'a' + index as u8) as char;
    let device = format!("/dev/vd{dev_letter}");
    let dest = "/opt/rclone";

    eprintln!("[INIT] Setting up {dest}");

    if !Path::new(&device).exists() {
        return Err(format!("rclone_tools device {device} not found"));
    }

    let _ = std::fs::create_dir_all(dest);
    libc_mount_result(&device, dest, "squashfs", libc::MS_RDONLY, "")?;
    eprintln!("[INIT] Mounted rclone_tools from {device} at {dest}");
    Ok(())
}

/// Mount readonly block devices (squashfs).
/// Xrefs: "[INIT] Mounted readonly ...", "[INIT] WARNING: Readonly device ..."
fn mount_readonly_devices(
    mounts: &[ReadonlyMount],
    start_index: Option<u32>,
) -> Result<(), String> {
    let start = start_index.unwrap_or(3);
    for (i, mount) in mounts.iter().enumerate() {
        let dev_index = start + i as u32;
        if dev_index > 25 {
            eprintln!(
                "[INIT] WARNING: Readonly device index {dev_index} exceeds /dev/vda-vdz range"
            );
            continue;
        }
        let dev_letter = (b'a' + dev_index as u8) as char;
        let device = format!("/dev/vd{dev_letter}");
        let dest = &mount.destination;

        if !Path::new(&device).exists() {
            eprintln!("[INIT] WARNING: Readonly device {device} not found");
            continue;
        }

        let _ = std::fs::create_dir_all(dest);
        match libc_mount_result(&device, dest, "squashfs", libc::MS_RDONLY, "") {
            Ok(()) => {
                eprintln!("[INIT] Mounted readonly {device} at {dest}");
            }
            Err(e) => {
                eprintln!("[INIT] WARNING: Failed to mount {device} at {dest}: {e}");
            }
        }
    }
    Ok(())
}

/// Write a file to disk.
/// Xrefs: "[INIT] Wrote ..."
fn write_file(path: &str, content: &str) {
    if let Some(parent) = Path::new(path).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    match std::fs::write(path, content) {
        Ok(()) => eprintln!("[INIT] Wrote {path}"),
        Err(e) => eprintln!("[INIT] WARNING: Failed to write {path}: {e}"),
    }
}

/// Load environment variables from /container.env (JSON format).
/// Xrefs: "[INIT] Failed to parse container.env as JSON: ...",
///   "[INIT] Environment variables loaded", "[INIT] ENV ..."
fn load_container_env() {
    let env_path = "/container.env";
    if !Path::new(env_path).exists() {
        return;
    }

    match std::fs::read_to_string(env_path) {
        Ok(data) => {
            match serde_json::from_str::<std::collections::HashMap<String, String>>(&data) {
                Ok(env_vars) => {
                    for (key, value) in &env_vars {
                        eprintln!("[INIT] ENV {key}=...");
                        unsafe { std::env::set_var(key, value) };
                    }
                    eprintln!("[INIT] Environment variables loaded");
                }
                Err(e) => {
                    eprintln!("[INIT] Failed to parse container.env as JSON: {e}");
                }
            }
        }
        Err(e) => {
            eprintln!("[INIT] WARNING: Failed to read {env_path}: {e}");
        }
    }
}

/// Scrub auth tokens from saved config files.
/// Xrefs: "[INIT] Auth tokens scrubbed from config(s)"
///
/// Removes the `auth_token` field from /mount_config.json and any FUSE mount
/// config files that were written during init, preventing tokens from persisting
/// on disk after they are no longer needed.
fn scrub_auth_tokens() {
    let config_paths = ["/mount_config.json", "/tmp/rclone-mount-config.json"];
    for path in &config_paths {
        if !Path::new(path).exists() {
            continue;
        }
        if let Ok(data) = std::fs::read_to_string(path) {
            if let Ok(mut json) = serde_json::from_str::<serde_json::Value>(&data) {
                let mut modified = false;
                // Scrub top-level auth_token
                if json.get("auth_token").is_some() {
                    json.as_object_mut().map(|obj| obj.remove("auth_token"));
                    modified = true;
                }
                // Scrub auth_token in fuse_mounts array entries
                if let Some(fuse_mounts) = json.get_mut("fuse_mounts") {
                    if let Some(arr) = fuse_mounts.as_array_mut() {
                        for entry in arr.iter_mut() {
                            if entry.get("auth_token").is_some() {
                                entry.as_object_mut().map(|obj| obj.remove("auth_token"));
                                modified = true;
                            }
                        }
                    }
                }
                if modified {
                    let _ = std::fs::write(
                        path,
                        serde_json::to_string_pretty(&json).unwrap_or_default(),
                    );
                }
            }
        }
    }
    eprintln!("[INIT] Auth tokens scrubbed from config(s)");
}

/// Set the system clock via clock_settime.
/// Xrefs: "[INIT] clock_settime failed: errno=...",
///   "sync_clock not available on Linux (use /mount_root realtime_unix_nanos)"
fn sync_clock(realtime_unix_nanos: u64) {
    let secs = (realtime_unix_nanos / 1_000_000_000) as i64;
    let nsecs = (realtime_unix_nanos % 1_000_000_000) as i64;

    let ts = libc::timespec {
        tv_sec: secs,
        tv_nsec: nsecs,
    };

    let ret = unsafe { libc::clock_settime(libc::CLOCK_REALTIME, &ts) };
    if ret != 0 {
        eprintln!(
            "[INIT] clock_settime failed: errno={}",
            std::io::Error::last_os_error()
        );
    }
}

/// Write "3" to /proc/sys/vm/drop_caches.
/// Verified against binary strings: no error strings for this operation.
/// The `let _ =` matches the binary behavior (best-effort cache drop).
fn write_drop_caches() {
    let _ = std::fs::write("/proc/sys/vm/drop_caches", "3\n");
}

/// Drop CAP_SYS_RESOURCE from the bounding set.
/// Xrefs: "[INIT] Dropped CAP_SYS_RESOURCE from bounding set",
///   "[INIT] FATAL: Failed to drop CAP_SYS_RESOURCE: ..."
fn drop_cap_sys_resource() {
    // CAP_SYS_RESOURCE = 24
    let ret = unsafe { libc::prctl(libc::PR_CAPBSET_DROP, 24, 0, 0, 0) };
    if ret != 0 {
        eprintln!(
            "[INIT] FATAL: Failed to drop CAP_SYS_RESOURCE: {}",
            std::io::Error::last_os_error()
        );
    } else {
        eprintln!("[INIT] Dropped CAP_SYS_RESOURCE from bounding set");
    }
}

/// Create essential device nodes via mknod.
/// Xrefs: "[INIT] Created device nodes via mknod"
///
/// Creates /dev/null, /dev/random, /dev/urandom, /dev/tty — the devices
/// confirmed present as string literals in the binary (at offsets near
/// 0x28c66f). Called from mount_essential_filesystems() after mounting
/// devtmpfs on /dev, to ensure these nodes exist even if devtmpfs doesn't
/// auto-populate them in Firecracker.
///
/// Binary string evidence: `/dev/null`, `/dev/random`, `/dev/urandom`,
/// `/dev/tty` are standalone strings. `/dev/zero`, `/dev/console`, and
/// `/dev/ptmx` were NOT found; they are likely provided by devtmpfs or
/// /dev/pts automatically.
fn create_device_nodes() {
    // (path, mode, major, minor)
    // Only the devices confirmed in binary string literals.
    let devices: &[(&str, u32, u32, u32)] = &[
        ("/dev/null", libc::S_IFCHR | 0o666, 1, 3),
        ("/dev/random", libc::S_IFCHR | 0o666, 1, 8),
        ("/dev/urandom", libc::S_IFCHR | 0o666, 1, 9),
        ("/dev/tty", libc::S_IFCHR | 0o666, 5, 0),
    ];

    for (path, mode, major, minor) in devices {
        if Path::new(path).exists() {
            continue;
        }
        let path_c = std::ffi::CString::new(*path).unwrap();
        unsafe {
            libc::mknod(
                path_c.as_ptr(),
                *mode,
                libc::makedev(*major, *minor) as libc::dev_t,
            );
        }
    }
    eprintln!("[INIT] Created device nodes via mknod");
}

// -----------------------------------------------------------------------
// Low-level mount/network helpers using libc
// -----------------------------------------------------------------------

/// Helper: call libc::mount, ignoring errors.
/// Used in mount_essential_filesystems where the binary has no per-mount
/// error handling (verified via strings analysis).
fn libc_mount(source: &str, target: &str, fstype: &str, flags: u64, data: &str) {
    let _ = libc_mount_result(source, target, fstype, flags, data);
}

/// Helper: call libc::mount, returning Result.
fn libc_mount_result(
    source: &str,
    target: &str,
    fstype: &str,
    flags: u64,
    data: &str,
) -> Result<(), String> {
    use std::ffi::CString;
    let source_c = CString::new(source).unwrap();
    let target_c = CString::new(target).unwrap();
    let fstype_c = CString::new(fstype).unwrap();
    let data_c = CString::new(data).unwrap();

    let ret = unsafe {
        libc::mount(
            source_c.as_ptr(),
            target_c.as_ptr(),
            fstype_c.as_ptr(),
            flags as libc::c_ulong,
            data_c.as_ptr() as *const libc::c_void,
        )
    };

    if ret != 0 {
        Err(format!(
            "mount({source}, {target}, {fstype}): {}",
            std::io::Error::last_os_error()
        ))
    } else {
        Ok(())
    }
}

/// Configure a network interface with IP, netmask, MTU, and bring it up.
/// Uses SIOCSIFADDR, SIOCSIFNETMASK, SIOCSIFMTU, SIOCSIFFLAGS ioctls.
fn configure_network_interface(sock: i32, name: &str, ip: &str, netmask: &str, mtu: u32) {
    use std::ffi::CString;
    eprintln!("[INIT] Setting up {name}");

    let name_c = CString::new(name).unwrap();

    // Build a zeroed ifreq struct (40 bytes on x86_64)
    let mut ifr: libc::ifreq = unsafe { std::mem::zeroed() };
    // Copy interface name into ifr_name (max IFNAMSIZ = 16)
    let name_bytes = name_c.as_bytes_with_nul();
    let copy_len = name_bytes.len().min(libc::IFNAMSIZ);
    unsafe {
        std::ptr::copy_nonoverlapping(
            name_bytes.as_ptr(),
            ifr.ifr_name.as_mut_ptr() as *mut u8,
            copy_len,
        );
    }

    // Helper: build sockaddr_in from IP string
    let make_sockaddr_in = |addr_str: &str| -> libc::sockaddr_in {
        let mut sa: libc::sockaddr_in = unsafe { std::mem::zeroed() };
        sa.sin_family = libc::AF_INET as libc::sa_family_t;
        let octets: Vec<u8> = addr_str.split('.').filter_map(|o| o.parse().ok()).collect();
        if octets.len() == 4 {
            sa.sin_addr.s_addr = u32::from_ne_bytes([octets[0], octets[1], octets[2], octets[3]]);
        }
        sa
    };

    // SIOCSIFADDR — set IP address
    let sa = make_sockaddr_in(ip);
    ifr.ifr_ifru.ifru_addr =
        unsafe { std::mem::transmute::<libc::sockaddr_in, libc::sockaddr>(sa) };
    unsafe {
        libc::ioctl(sock, libc::SIOCSIFADDR, &ifr);
    }

    // SIOCSIFNETMASK — set netmask
    let sa = make_sockaddr_in(netmask);
    ifr.ifr_ifru.ifru_addr =
        unsafe { std::mem::transmute::<libc::sockaddr_in, libc::sockaddr>(sa) };
    unsafe {
        libc::ioctl(sock, libc::SIOCSIFNETMASK, &ifr);
    }

    // SIOCSIFMTU — set MTU
    ifr.ifr_ifru.ifru_mtu = mtu as libc::c_int;
    unsafe {
        libc::ioctl(sock, libc::SIOCSIFMTU, &ifr);
    }

    // SIOCSIFFLAGS — bring interface up (IFF_UP | IFF_RUNNING)
    ifr.ifr_ifru.ifru_flags = (libc::IFF_UP | libc::IFF_RUNNING) as libc::c_short;
    unsafe {
        libc::ioctl(sock, libc::SIOCSIFFLAGS, &ifr);
    }
}

/// Configure the default route via gateway using SIOCADDRT ioctl.
fn configure_default_route(sock: i32, gateway: &str) {
    // Build an rtentry for the default route (0.0.0.0/0 via gateway)
    let mut rt: libc::rtentry = unsafe { std::mem::zeroed() };

    // Destination: 0.0.0.0
    let mut dst: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    dst.sin_family = libc::AF_INET as libc::sa_family_t;
    dst.sin_addr.s_addr = 0; // 0.0.0.0
    rt.rt_dst = unsafe { std::mem::transmute::<libc::sockaddr_in, libc::sockaddr>(dst) };

    // Gateway
    let mut gw: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    gw.sin_family = libc::AF_INET as libc::sa_family_t;
    let octets: Vec<u8> = gateway.split('.').filter_map(|o| o.parse().ok()).collect();
    if octets.len() == 4 {
        gw.sin_addr.s_addr = u32::from_ne_bytes([octets[0], octets[1], octets[2], octets[3]]);
    }
    rt.rt_gateway = unsafe { std::mem::transmute::<libc::sockaddr_in, libc::sockaddr>(gw) };

    // Netmask: 0.0.0.0 (default route)
    let mut mask: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    mask.sin_family = libc::AF_INET as libc::sa_family_t;
    mask.sin_addr.s_addr = 0;
    rt.rt_genmask = unsafe { std::mem::transmute::<libc::sockaddr_in, libc::sockaddr>(mask) };

    // Flags: RTF_UP | RTF_GATEWAY
    rt.rt_flags = libc::RTF_UP | libc::RTF_GATEWAY;

    unsafe {
        libc::ioctl(sock, libc::SIOCADDRT, &rt);
    }
}

/// Freeze the root filesystem (FIFREEZE ioctl).
/// Xrefs: "[CONTROL] Freezing / ...", "[CONTROL] / frozen",
///   "[CONTROL] FIFREEZE failed (continuing): ..."
///
/// FIFREEZE = _IOWR('X', 119, int) = 0xC0045877 (verified against Linux headers)
pub fn freeze_root() -> Result<(), String> {
    let fd = unsafe { libc::open(c"/".as_ptr(), libc::O_RDONLY) };
    if fd < 0 {
        return Err(format!(
            "open(/) failed: {}",
            std::io::Error::last_os_error()
        ));
    }

    // FIFREEZE = _IOWR('X', 119, int) = 0xC0045877
    let ret = unsafe { libc::ioctl(fd, 0xC0045877, 0) };
    unsafe {
        libc::close(fd);
    }

    if ret != 0 {
        Err(format!(
            "FIFREEZE failed: {}",
            std::io::Error::last_os_error()
        ))
    } else {
        Ok(())
    }
}

/// Thaw the root filesystem (FITHAW ioctl).
/// Xrefs: "[INIT] Thawed / ", " resumed from frozen full-checkpoint snapshot"
///
/// FIFREEZE = _IOWR('X', 119, int) = 0xC0045877 (verified against Linux headers)
/// FITHAW   = _IOWR('X', 120, int) = 0xC0045878 (verified against Linux headers)
pub fn thaw_root() -> Result<(), String> {
    let fd = unsafe { libc::open(c"/".as_ptr(), libc::O_RDONLY) };
    if fd < 0 {
        return Err(format!(
            "open(/) failed: {}",
            std::io::Error::last_os_error()
        ));
    }

    // FITHAW = 0xC0045878
    let ret = unsafe { libc::ioctl(fd, 0xC0045878, 0) };
    unsafe {
        libc::close(fd);
    }

    if ret != 0 {
        Err(format!(
            "FITHAW failed: {}",
            std::io::Error::last_os_error()
        ))
    } else {
        eprintln!("[INIT] Thawed /  resumed from frozen full-checkpoint snapshot");
        Ok(())
    }
}
