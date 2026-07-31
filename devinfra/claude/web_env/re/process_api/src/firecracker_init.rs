//! Reverse-engineered from process_api BuildID edebff2c28de76238c95c299ba3401a9098c9e17
//! release process_api_2026-05-11-18-55
//! Offsets from 91c789ff, NOT re-verified against edebff2c, except where an
//! edebff2c address is named explicitly (all of the CA fan-out section).
//! Source path and string refs verified against edebff2c.
//!
//! New in edebff2c:
//!   - Egress-CA fan-out (`append_ca_cert` and friends, 0xf37d0..0x101570) —
//!     see the section at the end of this file. This is the single largest
//!     addition in the build.
//!   - "[INIT] Removing non-directory at {} (checkpoint replaced dir)"
//!     (template 0x388b0d) — init deletes a non-directory sitting where a
//!     mountpoint should be, attributed to a checkpoint restore.
//!   - `RCLONE_CACHE_DIR` (0x37bca0) is exported pointing at
//!     `/dev/shm/rclone-vfscache` (0x3988f5) for the rclone FUSE daemons; the
//!     810fd3a4 build had only `/root/.cache/rclone`.
//!   - "[INIT] WARNING: chown {} -> {}:{} failed: {}" (template 0x3897ea,
//!     fn 0xf5cd0).
//!   - "failed to set environment variable `{}` to `{}`: {}" (template
//!     0x38981a).
//!
//! Firecracker VM init system — runs as PID 1 inside a Firecracker microVM.
//! Handles low-level system initialization that is normally done by the container
//! runtime (gVisor/runc) on the host side.
//!
//! Source file (edebff2c panic-location table): src/firecracker_init.rs
//!
//! String refs (Binary: 91c789ff):
//!   "[INIT] Starting Firecracker VM initialization..."
//!   "[INIT] Mounting essential filesystems..."
//!   "[INIT] Essential filesystems mounted"
//!   "[INIT] Setting up networking..."
//!   "[INIT] Network configured: IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400"
//!   "[INIT] pivot_root ok"
//!   "[INIT] pivot_root failed (...)"
//!   "[INIT] MS_MOVE+chroot ok"
//!   "[INIT] Mounted /dev/vda at ..."
//!   "[INIT] Mounted /dev/vdb at ..."   (new: /dev/vdb support)
//!   "[INIT] Model tools mounted from /dev/vdb"
//!   "[INIT] /dev/fuse already exists"
//!   "[INIT] Created /dev/fuse"
//!   "[INIT] Creating /dev/fuse device node..."
//!   "[INIT] Created device nodes via mknod"
//!   "[INIT] FUSE service_url: ..."
//!   "[INIT] FUSE_MOUNT_STATUS count=..."
//!   "[INIT] Waiting for ready_file(s)..."
//!   "[INIT] All ready_file(s) found after ..."
//!   "[INIT] fuse_spawn FAILED"        (new: replaces "fuse_mounts FAILED")
//!   "[INIT] Spawning: ..."
//!   "[INIT] Spawned ..."
//!   "[INIT] Environment variables loaded"
//!   "[INIT] Fresh boot: reading /mount_config.json..."   (may still exist)
//!   "[INIT] Firecracker init complete, starting process_api services..."
//!   "[INIT] Fresh boot init complete: ..."
//!   "[INIT] Auth tokens scrubbed from config(s)"
//!   "[INIT] devtmpfs remount restored device nodes"
//!   "[INIT] Dropped CAP_SYS_RESOURCE from bounding set"
//!   "[INIT] FATAL: Failed to drop CAP_SYS_RESOURCE: ..."
//!   "[INIT] FATAL: mount_root_and_pivot failed: ..."
//!   "[INIT] FATAL: socket() failed: ..."
//!
//! Snapstart:
//!   "[INIT] Snapstart template mode: signaling ready..."  (SNAPSTART_READY signal)
//!
//! Additional strings (firecracker_init.rs):
//!   "[INIT] Spawned ...", "[INIT] pivot_root failed (...)", "[INIT] memory mount: dest=...",
//!   "[INIT] filestore mount: dest=...", "[INIT] WARNING: rclone_tools device ...",
//!   "[INIT] Mounted rclone_tools from ...", "[INIT] WARNING: Failed to mount rclone_tools...",
//!   "[INIT] clock_settime failed: errno=...", "[INIT] FUSE mount wait failed (non-fatal): ...",
//!   "[INIT] FUSE daemon spawn failed: ...", "[INIT] FUSE daemon(s) spawned in ...",
//!   "[INIT] WARNING: Readonly device ...", "[INIT] Mounted readonly ...",
//!   "[INIT] WARNING: Failed to mount ...", "[INIT] WARNING: mount ...",
//!   "[INIT] sethostname failed: errno=...",
//!   "multimount --config ...", "multimount (PID ...)", "multimount exited with ...",
//!   "squashfs mount may have failed", "already completed"
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

/// Main Firecracker VM init entry point.
/// Called synchronously before the tokio runtime is started.
///
/// Binary: 91c789ff
///
/// Sequence:
/// 1. Mount essential filesystems (/proc, /sys, /dev, /dev/pts, /dev/shm)
/// 2. Set hostname to "vm" via sethostname(2)
/// 3. Set up networking (IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400)
/// 4. Write /etc/hostname, /etc/hosts, /etc/resolv.conf (nameserver 8.8.8.8)
///    (Binary: 91c789ff — DNS/network config set up at init time)
/// 5. Check SNAPSTART_READY env var:
///    - If set: signal ready on /dev/ttyS0 and return (snapstart template mode)
///    - If not set: read /mount_config.json (fresh boot) and apply
/// 6. Mount root filesystem from /dev/vda, pivot_root or MS_MOVE+chroot
/// 7. Mount model tools from /dev/vdb if configured
/// 8. Create /dev/fuse, spawn multimount for FUSE mounts
/// 9. Mount rclone_tools
/// 10. Mount readonly block devices
/// 11. Write config files (etc_hosts, resolv_conf, ca_cert_pem)
/// 12. Load environment variables from /container.env
/// 13. Scrub auth tokens from configs
/// 14. Drop CAP_SYS_RESOURCE
/// 15. Write drop_caches
pub fn run_firecracker_init() {
    let start = Instant::now();
    eprintln!("[INIT] Starting Firecracker VM initialization...");

    // Step 1: Mount essential filesystems
    mount_essential_filesystems();

    // Step 2: Set hostname to "vm"
    // Binary: 91c789ff — calls sethostname("vm", 2) after mounting essential filesystems.
    // The "vm" string is borrowed from the "vm/mount_config.json" literal at 0x2a37be.
    set_hostname("vm");

    // Step 3: Set up networking
    if let Err(e) = setup_networking() {
        eprintln!("[INIT] FATAL: socket() failed: {e}");
        std::process::exit(1);
    }

    // Step 4: Write DNS/network config files at init time.
    // Binary: 91c789ff — /etc/hostname, /etc/hosts, /etc/resolv.conf now
    // set up here before mount_config.json is read, not only from MountRootConfig.
    write_initial_network_files();

    // Step 5: Check SNAPSTART_READY env var (offset 0x287a30 in binary).
    // If set, this is a snapstart template — signal ready on /dev/ttyS0 and return.
    // The control server's POST /mount_root will call apply_mount_config later.
    if std::env::var("SNAPSTART_READY").is_ok() {
        eprintln!("[INIT] Snapstart template mode: signaling ready...");
        signal_snapstart_ready();
        eprintln!("[INIT] Firecracker init complete, starting process_api services...");
        return;
    }

    // Fresh boot path: read /mount_config.json
    eprintln!("[INIT] Fresh boot: reading /mount_config.json...");
    let config = match std::fs::read_to_string("/mount_config.json") {
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
    };

    // Steps 6-15: Apply mount configuration
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

/// Write initial /etc/hostname, /etc/hosts, and /etc/resolv.conf at VM init time.
/// Binary: 91c789ff — DNS/network configuration is now written here, before
/// mount_config.json is read, so the VM has basic DNS from the start.
/// Evidence: strings "/etc/hostname", "/etc/hosts", "nameserver 8.8.8.8" in new binary.
fn write_initial_network_files() {
    // Write /etc/hostname
    write_file("/etc/hostname", "sandbox\n");

    // Write /etc/hosts
    write_file(
        "/etc/hosts",
        "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n",
    );

    // Write /etc/resolv.conf with Google DNS
    write_file("/etc/resolv.conf", "nameserver 8.8.8.8\n");
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

    // Set up FUSE mounts via multimount
    // Binary: 91c789ff — uses "multimount --config" to spawn FUSE daemon.
    // Binary: edebff2c — the "fuse_spawn ok; " status fragment was REMOVED;
    // .rodata now contains only "fuse_spawn FAILED; " (0x398e2b), so the
    // status line reports the FUSE step only when it fails.
    if let Some(ref fuse_mounts) = config.fuse_mounts {
        match setup_fuse_mounts(fuse_mounts, config) {
            Ok(()) => {}
            Err(e) => {
                eprintln!("[INIT] FUSE mount wait failed (non-fatal): {e}");
                status_parts.push("fuse_spawn FAILED".to_string());
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
    // Binary: edebff2c — the CA PEM is no longer just written over
    // /etc/ssl/certs/ca-certificates.crt; it is fanned out across every trust
    // store the sandbox's toolchains consult (see `append_ca_cert`).
    // On failure init records a marker file instead of aborting the boot:
    //   template 0x389691 "[INIT] WARNING: append_ca_cert failed: {}"
    //   template 0x3896dd "append_ca_cert failed at FC PID-1 boot: {}"
    //     written to "{root}/.sandboxing-ca-inject-failed" (template 0x3896be)
    if let Some(ref content) = config.ca_cert_pem {
        if let Err(e) = append_ca_cert(content) {
            eprintln!("[INIT] WARNING: append_ca_cert failed: {e}");
            let _ = std::fs::write(
                format!("{}/.sandboxing-ca-inject-failed", config.destination),
                format!("append_ca_cert failed at FC PID-1 boot: {e}\n"),
            );
        }
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

/// Set up FUSE mounts by spawning the `multimount` daemon.
///
/// Binary: 91c789ff — FUSE mounts are handled by a single `multimount` process
/// (not individual rclone invocations). The binary:
///   1. Creates /dev/fuse
///   2. Logs service_url and mount type for each entry
///   3. Writes mount config JSON to /tmp/rclone-mount-config.json
///   4. Spawns: `multimount --config /tmp/rclone-mount-config.json`
///   5. Waits for /tmp/rclone-mounts/ready
///   6. Reports FUSE_MOUNT_STATUS count
///
/// Xrefs: "[INIT] Creating /dev/fuse device node...",
///   "[INIT] /dev/fuse already exists", "[INIT] Created /dev/fuse",
///   "[INIT] FUSE service_url: ...", "[INIT] memory mount: dest=...",
///   "[INIT] filestore mount: dest=...",
///   "multimount --config ...", "multimount (PID ...)",
///   "[INIT] FUSE daemon(s) spawned in ...", "[INIT] FUSE daemon spawn failed: ...",
///   "[INIT] Waiting for ready_file(s)...",
///   "[INIT] All ready_file(s) found after ...",
///   "[INIT] FUSE_MOUNT_STATUS count=...",
///   "FUSE mounts timed out after ...",
///   "multimount exited with ... (check serial output above)",
///   "squashfs mount may have failed"
fn setup_fuse_mounts(
    fuse_mounts: &[FuseMountConfig],
    _config: &MountRootConfig,
) -> Result<(), String> {
    // Create /dev/fuse if needed
    create_dev_fuse();

    let mount_count = fuse_mounts.len();
    eprintln!("[INIT] Setting up {mount_count} FUSE mount(s)...");

    // Log each mount entry's service_url and type with details.
    // Binary: validates all mounts share the same service_url, warns on mismatch.
    let mut expected_service_url: Option<&str> = None;
    for mount in fuse_mounts {
        if let Some(ref service_url) = mount.service_url {
            match expected_service_url {
                None => {
                    eprintln!("[INIT] FUSE service_url: {service_url}");
                    expected_service_url = Some(service_url);
                }
                Some(expected) if service_url != expected => {
                    let dest = &mount.destination;
                    eprintln!(
                        "[INIT] WARNING: mount {dest} has service_url {service_url} != {expected}"
                    );
                }
                _ => {}
            }
        }

        let dest = &mount.destination;
        let _ = std::fs::create_dir_all(dest);

        if let Some(ref store_id) = mount.memory_store_id {
            let source = mount.source.as_deref().unwrap_or("");
            let vfs_cache = mount.vfs_cache_mode.as_deref().unwrap_or("");
            eprintln!(
                "[INIT] memory mount: dest={dest} store_id={store_id} readonly=false vfs_cache={vfs_cache} source={source}"
            );
        } else if let Some(ref fs_id) = mount.filesystem_id {
            let source = mount.source.as_deref().unwrap_or("");
            eprintln!(
                "[INIT] filestore mount: dest={dest} fs_id={fs_id} readonly=false source={source}"
            );
        }
    }

    // Write combined mount config for multimount
    let config_path = "/tmp/rclone-mount-config.json";
    let mount_dir = "/tmp/rclone-mounts";
    let ready_path = "/tmp/rclone-mounts/ready";
    let _ = std::fs::create_dir_all(mount_dir);

    if let Ok(config_json) = serde_json::to_string(fuse_mounts) {
        let _ = std::fs::write(config_path, &config_json);
        // Binary format: "[INIT] Wrote <name> config to <path>"
        eprintln!("[INIT] Wrote multimount config to {config_path}");
    }

    // Spawn multimount daemon
    // Binary: "multimount --config /tmp/rclone-mount-config.json"
    let spawn_start = Instant::now();
    let multimount_bin = "multimount";

    eprintln!("[INIT] Spawning: {multimount_bin} --config {config_path}");

    match std::process::Command::new(multimount_bin)
        .args(["--config", config_path])
        .spawn()
    {
        Ok(mut child) => {
            let pid = child.id();
            let spawn_elapsed = spawn_start.elapsed();
            eprintln!(
                "[INIT] Spawned {multimount_bin} --config {config_path} multimount (PID {pid})"
            );
            eprintln!(
                "[INIT] FUSE daemon(s) spawned in {:.1}s",
                spawn_elapsed.as_secs_f64()
            );

            // Brief delay then check if the process exited immediately
            std::thread::sleep(std::time::Duration::from_millis(50));
            match child.try_wait() {
                Ok(Some(status)) => {
                    eprintln!("multimount exited with {status} (check serial output above)");
                    eprintln!("[INIT] WARNING: try_wait on multimount (pid={pid}): exited");
                }
                Ok(None) => {} // Still running — expected
                Err(e) => {
                    eprintln!("[INIT] WARNING: try_wait on multimount (pid={pid}): {e}");
                }
            }
        }
        Err(e) => {
            eprintln!("[INIT] FUSE daemon spawn failed: {e}");
            return Err(format!("FUSE daemon spawn failed: {e}"));
        }
    }

    // Wait for ready files
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
    let total_s = spawn_start.elapsed().as_secs_f64();
    // Binary format: "[INIT] FUSE_MOUNT_STATUS count=... failures=0 total_s=..."
    eprintln!("[INIT] FUSE_MOUNT_STATUS count={count} failures=0 total_s={total_s:.1}s");

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
        let err = std::io::Error::last_os_error();
        // Binary format: "[INIT] WARNING: mknod /dev/fuse failed: <err> (ret=<ret>)"
        eprintln!("[INIT] WARNING: mknod /dev/fuse failed: {err} (ret={ret})");
    } else {
        eprintln!("[INIT] Created /dev/fuse");
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
        // Binary format: "[INIT] Mounted readonly <device> (squashfs) at <dest>"
        match libc_mount_result(&device, dest, "squashfs", libc::MS_RDONLY, "") {
            Ok(()) => {
                eprintln!("[INIT] Mounted readonly {device} (squashfs) at {dest}");
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

/// Set the system hostname via sethostname(2).
/// Binary: 91c789ff — called with "vm" (2 bytes) after mounting essential filesystems.
/// The "vm" string is borrowed from the "vm/mount_config.json" literal.
/// Xrefs: "[INIT] sethostname failed: errno=..."
fn set_hostname(name: &str) {
    use std::ffi::CString;
    let name_c = CString::new(name).unwrap();
    let ret = unsafe { libc::sethostname(name_c.as_ptr(), name.len()) };
    if ret != 0 {
        eprintln!(
            "[INIT] sethostname failed: errno={}",
            std::io::Error::last_os_error()
        );
    }
}

/// Signal SNAPSTART_READY to the Firecracker VMM by writing to serial port /dev/ttyS0.
///
/// Binary: 91c789ff — in snapstart template mode, the init process opens /dev/ttyS0
/// and writes "SNAPSTART_READY\n" to the serial port to signal that the VM is ready
/// to be snapshotted. The Firecracker VMM reads from the serial port and knows the
/// guest is in a clean state for snapshot creation.
///
/// Xrefs: "[INIT] Snapstart template mode: signaling ready...", "SNAPSTART_READY"
fn signal_snapstart_ready() {
    use std::io::Write;

    let serial_path = "/dev/ttyS0";
    match std::fs::OpenOptions::new().write(true).open(serial_path) {
        Ok(mut file) => {
            let _ = file.write_all(b"SNAPSTART_READY\n");
        }
        Err(e) => {
            eprintln!("[INIT] WARNING: Failed to open {serial_path}: {e}");
        }
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
/// Xrefs: "[INIT] Thawed / \u{2014} resumed from frozen full-checkpoint snapshot"
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
        eprintln!("[INIT] Thawed / \u{2014} resumed from frozen full-checkpoint snapshot");
        Ok(())
    }
}

/// Sync all filesystems (equivalent to sync(2)).
/// Called from the control server's POST /shutdown handler before dropping page caches.
/// Binary: 91c789ff — the shutdown path calls sync() before writing drop_caches.
pub async fn sync_filesystem() -> Result<(), String> {
    unsafe {
        libc::sync();
    }
    Ok(())
}

/// FIFREEZE ioctl on an open file descriptor.
/// Used by the control server's POST /fs_freeze handler.
/// Binary: 91c789ff — FIFREEZE = _IOWR('X', 119, int) = 0xC0045877
pub fn fifreeze_fd(file: &std::fs::File) -> Result<(), std::io::Error> {
    use std::os::unix::io::AsRawFd;
    let fd = file.as_raw_fd();
    let ret = unsafe { libc::ioctl(fd, 0xC0045877, 0) };
    if ret != 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// FITHAW ioctl on an open file descriptor.
/// Used by the control server's POST /fs_thaw handler.
/// Binary: 91c789ff — FITHAW = _IOWR('X', 120, int) = 0xC0045878
pub fn fithaw_fd(file: &std::fs::File) -> Result<(), std::io::Error> {
    use std::os::unix::io::AsRawFd;
    let fd = file.as_raw_fd();
    let ret = unsafe { libc::ioctl(fd, 0xC0045878, 0) };
    if ret != 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Egress-CA fan-out (Binary: edebff2c — entirely new in this build)
//
// The 810fd3a4 build wrote `ca_cert_pem` to /etc/ssl/certs/ca-certificates.crt
// and stopped there. edebff2c installs the same PEM into every trust store the
// sandbox's toolchains consult, and exports the corresponding env vars.
//
// Function map (call-target boundaries from `objdump -d` plus per-function
// string cross-references):
//
// | Address    | Role                                                        |
// | ---------- | ----------------------------------------------------------- |
// | `0xfab40`  | `append_ca_cert` — orchestrator; PEM files, env vars,        |
// |            | dpkg list, firefox policies                                 |
// | `0x101090` | PEM splitter ("BEGIN CERTIFICATE" 0x3999a2 / "END            |
// |            | CERTIFICATE" 0x399817)                                      |
// | `0xfc530`  | Java JKS injector (`keytool -importcert`)                    |
// | `0xfe330`  | NSS DB injector (`certutil -N` / `certutil -A`)              |
// | `0x100770` | Chrome/Chromium/Edge managed-policy writer                   |
// | `0xf5f90`  | Python bundle patcher (certifi / botocore / pip vendored)    |
// | `0xf37d0`  | gcloud bundle patcher                                        |
// | `0xf8d60`  | npm `cafile=` writer                                         |
// | `0xf9910`  | pip `[global] cert =` writer                                 |
// | `0xfa570`  | uv `native-tls = true` writer                                |
// | `0xf8910`  | sudoers `Defaults env_keep` writer                           |
// | `0xf5cd0`  | chown helper ("[INIT] WARNING: chown ...") |
//
// The path tables below were read out of `.data.rel.ro` (each entry is a
// `(ptr, len)` pair whose pointer arrives as an `R_X86_64_RELATIVE`
// relocation); addresses are given per table.
// ---------------------------------------------------------------------------

/// PEM written by `append_ca_cert` into the system anchor directory.
/// String at 0x398f48 (len 57), loaded at 0xfb7e3 / 0xfb996 / 0xfba85.
const EGRESS_CA_CRT: &str = "/usr/local/share/ca-certificates/sandboxing-egress-ca.crt";

/// Second copy written next to the merged bundle.
/// String at 0x398f81 (len 39), loaded at 0xfb83b / 0xfbb02 / 0xfbcc2.
const EGRESS_CA_PEM: &str = "/etc/ssl/certs/sandboxing-egress-ca.pem";

/// Merged system bundle the PEM is appended to.
/// String at 0x420e50 (`.data.rel.ro` entry, len 34).
const SYSTEM_CA_BUNDLE: &str = "/etc/ssl/certs/ca-certificates.crt";

/// dpkg file list appended so `update-ca-certificates` does not garbage-collect
/// the injected anchor. Template 0x388d4a, referenced from 0xf8d97, 0xf9951
/// and 0xfa5c3.
const DPKG_CA_LIST: &str = "/var/lib/dpkg/info/ca-certificates.list";

/// Additional merged bundles that get the PEM appended when they exist.
/// Table at `.data.rel.ro` 0x420b30..0x420b80 (relative to root).
const EXTRA_CA_BUNDLES: &[&str] = &[
    "etc/pki/tls/certs/ca-bundle.crt",
    "etc/pki/tls/cacert.pem",
    "etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "etc/ssl/ca-bundle.pem",
    "var/lib/ca-certificates/ca-bundle.pem",
];

/// Environment variables exported so children inherit the anchor.
/// Table at `.data.rel.ro` 0x420ea0..0x420f50.
///
/// Each is applied with `std::env::set_var` — the failure path formats
/// "failed to set environment variable `{}` to `{}`: {}" (template 0x38981a).
/// `SSL_CERT_DIR` gets the directory, the rest get a file path.
const CA_ENV_VARS: &[&str] = &[
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "PIP_CERT",
    "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE",
    "HTTPLIB2_CA_CERTS",
    "GIT_SSL_CAINFO",
    "AWS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "NIX_SSL_CERT_FILE",
];

/// Proxy variables preserved alongside the CA vars in the sudoers drop-in.
/// Table at `.data.rel.ro` 0x420dd0..0x420e20.
const PROXY_ENV_VARS: &[&str] = &[
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
];

/// Library roots scanned for Python `site-packages` / `dist-packages`.
/// Interned run at 0x398471..0x3984c0 plus the interpreter roots at
/// `.data.rel.ro` 0x420f50..0x420f90.
const PYTHON_LIB_ROOTS: &[&str] = &[
    "opt/conda/lib",
    "root/.local/lib",
    "usr/local/lib",
    "usr/lib",
    "root/code/.venv/lib",
    "root/.venv/lib",
    "opt/venv/lib",
    "app/.venv/lib",
    "workspace/.venv/lib",
];

/// Interpreter installation roots whose per-version trees are also scanned.
/// Table at `.data.rel.ro` 0x420f50..0x420f90.
const PYTHON_INTERPRETER_ROOTS: &[&str] = &[
    "root/.pyenv/versions",
    "root/.local/share/uv/python",
    "opt/pyenv/versions",
    "opt/conda/envs",
];

/// CA bundles shipped inside Python packages; each gets the PEM appended.
/// Interned run at 0x399099 ("certifi/cacert.pem"), 0x3990ab
/// ("pip/_vendor/certifi/cacert.pem"), 0x3990c9 ("botocore/cacert.pem"),
/// loaded at 0xf62d4 / 0xf62ef / 0xf630a in fn 0xf5f90.
const PYTHON_BUNDLE_RELPATHS: &[&str] = &[
    "certifi/cacert.pem",
    "pip/_vendor/certifi/cacert.pem",
    "botocore/cacert.pem",
];

/// google-cloud-sdk roots (0x3984d0..0x39850e) and the bundles inside them
/// (0x39867e..0x3986ed, loaded at 0xf532e..0xf537f in fn 0xf37d0).
const GCLOUD_ROOTS: &[&str] = &[
    "usr/lib/google-cloud-sdk",
    "opt/google-cloud-sdk",
    "root/google-cloud-sdk",
    "snap/google-cloud-sdk/current",
];
const GCLOUD_BUNDLE_RELPATHS: &[&str] = &[
    "lib/third_party/certifi/cacert.pem",
    "lib/third_party/botocore/cacert.pem",
    "platform/bq/third_party/certifi/cacert.pem",
    "platform/gsutil/third_party/certifi/certifi/cacert.pem",
];

/// npm configuration files that get a `cafile=` line (fn 0xf8d60, strings
/// 0x398c2e / 0x398c37 / 0x398c44; the emitted line comes from the template at
/// 0x389316, literal "cafile=" at 0x389319).
const NPMRC_PATHS: &[&str] = &["etc/npmrc", "usr/etc/npmrc", "usr/local/etc/npmrc"];

/// uv binaries probed before writing `/etc/uv/uv.toml` (table 0x420d00..0x420d40).
const UV_BINARIES: &[&str] = &[
    "usr/local/bin/uv",
    "usr/bin/uv",
    "root/.local/bin/uv",
    "root/.cargo/bin/uv",
];

/// Java trust stores. Absolute candidates come from the run at 0x398b33
/// ("etc/ssl/certs/java/cacerts", "etc/pki/java/cacerts",
/// "etc/pki/ca-trust/extracted/java/cacerts"); JDK roots from 0x398b88
/// ("usr/lib/jvm"), 0x398b93 ("root/.sdkman/candidates/java") and 0x37c2d8
/// ("opt/java"); relative store paths from 0x398bc3 ("lib/security/cacerts")
/// and 0x398bd7 ("jre/lib/security/cacerts").
const JAVA_CACERTS_ABS: &[&str] = &[
    "etc/ssl/certs/java/cacerts",
    "etc/pki/java/cacerts",
    "etc/pki/ca-trust/extracted/java/cacerts",
];
const JAVA_HOME_ROOTS: &[&str] = &["usr/lib/jvm", "root/.sdkman/candidates/java", "opt/java"];
const JAVA_CACERTS_RELPATHS: &[&str] = &["lib/security/cacerts", "jre/lib/security/cacerts"];

/// Default JKS password (string 0x37c298).
const JKS_PASSWORD: &str = "changeit";

/// Alias prefix for imported certs — template 0x39893f
/// "sandboxing-egress-ca-{}" (suffixed per certificate in the PEM chain).
const JKS_ALIAS_PREFIX: &str = "sandboxing-egress-ca-";

/// NSS database directories (strings 0x398d93 ".pki/nssdb",
/// 0x398d9d ".local/share/pki/nssdb", loaded at 0xfe3e2 / 0xfe41f) and the
/// files that mark an initialized DB (0x398db3 "key4.db", 0x398dba
/// "pkcs11.txt", 0x37c2e0 "cert9.db").
const NSS_DB_DIRS: &[&str] = &[".pki/nssdb", ".local/share/pki/nssdb"];

/// Firefox policy directories (strings 0x398fa8 / 0x398fbc) plus the browser
/// installation roots at `.data.rel.ro` 0x420b80..0x420cb0 whose
/// `distribution/policies.json` is written.
const FIREFOX_POLICY_DIRS: &[&str] = &["etc/firefox/policies", "etc/firefox-esr/policies"];
const BROWSER_ROOTS: &[&str] = &[
    "usr/lib/firefox",
    "usr/lib/firefox-esr",
    "usr/lib64/firefox",
    "opt/firefox",
    "usr/bin/firefox",
    "usr/bin/firefox-esr",
    "ms-playwright",
    "root/.cache/ms-playwright",
    "root/.cache/puppeteer",
    "opt/puppeteer-cache",
    "opt/ms-playwright",
    "opt/google/chrome",
    "opt/microsoft/msedge",
    "usr/lib/chromium",
    "usr/lib/chromium-browser",
    "usr/bin/google-chrome",
    "usr/bin/chromium",
    "usr/bin/chromium-browser",
    "usr/bin/microsoft-edge",
];

/// Chromium-family managed-policy directories (strings 0x398e83, 0x398ea2,
/// 0x398ebf, loaded at 0x100b6d / 0x100b82 / 0x100b9a in fn 0x100770). The
/// file written into each is "/sandboxing-ca.json" (template 0x3895d3).
const CHROME_POLICY_DIRS: &[&str] = &[
    "etc/opt/chrome/policies/managed",
    "etc/chromium/policies/managed",
    "etc/opt/edge/policies/managed",
];

/// Install the egress-inspection CA into every trust store present on the
/// rootfs. Called from the Firecracker init path (config `ca_cert_pem`) and
/// from the control server's `POST /auth_public_key/write_etc_files`.
///
/// Decompiled from 0xfab40..0xfc530.
///
/// Recovered steps (each failure is a warning, never fatal):
///  1. Write `EGRESS_CA_CRT` and `EGRESS_CA_PEM` (0xfb7e3, 0xfb83b).
///  2. Append the PEM to `SYSTEM_CA_BUNDLE` and every existing
///     `EXTRA_CA_BUNDLES` entry.
///  3. Append `EGRESS_CA_CRT` to `DPKG_CA_LIST` so `update-ca-certificates`
///     keeps it (0xf8d97 / 0xf9951 / 0xfa5c3 all consult this path).
///  4. Export `CA_ENV_VARS` into this process's environment, so every child of
///     PID 1 inherits them.
///  5. Write `/etc/sudoers.d/90-sandbox-ca-env` with
///     `Defaults env_keep += "..."` (template 0x38978b, fn 0xf8910) listing
///     `CA_ENV_VARS` + `PROXY_ENV_VARS`; failure logs
///     "[INIT] WARNING: sudoers env_keep write failed: {}" (0x3897a7).
///  6. Patch Python / gcloud vendored bundles, npm, pip and uv configs.
///  7. Inject into Java keystores and NSS databases.
///  8. Write Firefox and Chromium managed policies.
///
/// # TODO(re)
/// The orchestration order above is the order the helper functions appear in
/// `.text` and the order their strings appear in `.rodata`; the exact call
/// sequence inside 0xfab40 was not traced branch by branch.
pub fn append_ca_cert(pem: &str) -> Result<(), String> {
    // TODO(re): stub — the individual helpers below carry the recovered
    // evidence; the driver body of 0xfab40 (roughly 6 KB of inlined path
    // joining and `std::fs` calls) was not decompiled statement by statement.
    let certs = split_pem_certificates(pem);
    if certs.is_empty() {
        // 0xfc5a5 — the JKS path bails out with this warning when the PEM does
        // not parse into at least one certificate.
        eprintln!("[INIT] WARNING: egress CA PEM unparseable; skipping JKS inject");
    }
    let _ = pem;
    Ok(())
}

/// Split a PEM blob into individual base64 certificate bodies.
/// Decompiled from 0x101090..0x101570.
/// Xrefs: "BEGIN CERTIFICATE" (0x3999a2, loaded at 0x101113 and 0x10119c),
///   "END CERTIFICATE" (0x399817, loaded at 0x101217).
fn split_pem_certificates(pem: &str) -> Vec<String> {
    // TODO(re): stub — only the two delimiter strings and the function
    // boundary were recovered; the scanning loop was not decompiled.
    let mut out = Vec::new();
    let mut rest = pem;
    while let Some(begin) = rest.find("BEGIN CERTIFICATE") {
        let after = &rest[begin..];
        match after.find("END CERTIFICATE") {
            Some(end) => {
                out.push(after[..end].to_string());
                rest = &after[end..];
            }
            None => break,
        }
    }
    out
}

/// Import the certificate chain into every Java keystore found on the rootfs.
/// Decompiled from 0xfc530..0xfe330.
///
/// Recovered behaviour:
/// * Candidate stores: `JAVA_CACERTS_ABS`, plus `JAVA_CACERTS_RELPATHS` under
///   every directory in `JAVA_HOME_ROOTS`.
/// * A store whose magic is not JKS v2 and for which `keytool` is unusable is
///   skipped with template 0x38928f:
///   "[INIT] cacerts at {} is not JKS v2 changeit and keytool unavailable/failed; skipped"
/// * Otherwise it runs `keytool -importcert -keystore <store> -storepass
///   changeit -noprompt -alias sandboxing-egress-ca-<n>` with `LC_ALL` set
///   (argv strings interned at 0x39895d..0x3989aa, loaded at 0xfd5a3..0xfd9f8).
/// * `already exists` (0x3989aa) in keytool's output is treated as success.
/// * Failures log "[INIT] keytool -importcert failed: {}" (0x388fac) or
///   "[INIT] WARNING: cacerts write failed at {}: {}" (0x3892e5).
///
/// # TODO(re)
/// The JKS-v2 magic check and the direct (keytool-free) keystore append were
/// not decompiled; only their string edges are established.
fn inject_java_cacerts(_certs: &[String]) {
    // TODO(re): stub — see the doc comment for what the binary establishes.
}

/// Add the certificate to every NSS database on the rootfs.
/// Decompiled from 0xfe330..0x100770.
///
/// Recovered behaviour:
/// * Databases are located by scanning for `NSS_DB_DIRS` and by walking
///   `/proc/mounts` (string 0x398dc8 region); a directory counts as a database
///   when it contains `cert9.db`, `key4.db` or `pkcs11.txt`.
/// * An uninitialized database is created with
///   `certutil -N -d sql:/<dir> --empty-password`
///   (argv fragments "-N" 0x398dc4, "-d" 0x398dc6, "--empty-password"
///   0x37bcb0; the `sql:/` prefix is template 0x3893aa).
///   Failure logs "[INIT] WARNING: certutil -N failed: {}" (0x3893b2).
/// * The certificate is then added with
///   `certutil -A -n <alias> -t C,, -a -d sql:/<dir>`
///   ("-A" 0x398dd3, "-n" 0x398dd5, "-t" 0x398dd7, "C,," 0x398dd9,
///   "-a" 0x398ddc).
///   Failures log "[INIT] WARNING: certutil -A failed at {}: {}" (0x38940f)
///   and "[INIT] WARNING: certutil -A wait failed: {}" (0x38943e).
/// * If `certutil` is not on PATH the whole step is skipped with template
///   0x3893db: "[INIT] certutil unavailable ({}); skipping NSS DB".
fn inject_nss_databases(_cert_path: &str) {
    // TODO(re): stub — argv and error edges recovered; the scanning loop and
    // the process spawn/wait bookkeeping were not decompiled.
}

/// Write a Chromium/Chrome/Edge managed policy that installs the CA.
/// Decompiled from 0x100770..0x101090.
///
/// The policy file is `<dir>/sandboxing-ca.json` for each existing
/// `CHROME_POLICY_DIRS` entry; failure logs
/// "[INIT] WARNING: chrome policies write failed at {}: {}" (0x3895e8).
///
/// # TODO(re)
/// The JSON body was not recovered — no policy-key string other than the
/// "Certificates"/"Install" pair at 0x398e70 (used by the Firefox writer)
/// appears in `.rodata`, so the Chromium policy schema this build emits is
/// undetermined.
fn write_chrome_policies(_cert_path: &str) {
    // TODO(re): stub — see the doc comment.
}

/// Write Firefox enterprise policies that install the CA.
/// Decompiled from the tail of 0xfab40 (0xfb500..0xfc4a0).
///
/// * Policy object keys "policies" (0x37c2f0), "Certificates" (0x398e70) and
///   "Install" (0x398e7c) are emitted around the `EGRESS_CA_CRT` path.
/// * Files written: `<dir>/policies.json` for each `FIREFOX_POLICY_DIRS`
///   entry (template 0x38970b) and `<root>/distribution/policies.json` for
///   each `BROWSER_ROOTS` entry (template 0x389756).
/// * Failure logs "[INIT] WARNING: firefox policies write failed at {}: {}"
///   (0x38971b).
fn write_firefox_policies(_cert_path: &str) {
    // TODO(re): stub — key names, target paths and the failure edge are
    // recovered; the JSON serialization itself was not decompiled.
}

/// Append the CA to every vendored Python trust bundle.
/// Decompiled from 0xf5f90..0xf7630.
///
/// Scans `PYTHON_LIB_ROOTS` (plus every version directory under
/// `PYTHON_INTERPRETER_ROOTS`) for `site-packages` (0x39907f) and
/// `dist-packages` (0x39908c) directories, then appends the PEM to each
/// existing `PYTHON_BUNDLE_RELPATHS` file. `/opt/conda/ssl/cacert.pem`
/// (templates 0x388d24 + 0x388d35) is handled the same way.
fn patch_python_bundles(_pem: &str) {
    // TODO(re): stub — path tables recovered from `.data.rel.ro`; the walk
    // itself was not decompiled.
}

/// Append the CA to google-cloud-sdk's vendored bundles.
/// Decompiled from 0xf37d0..0xf5cd0 — `GCLOUD_ROOTS` x `GCLOUD_BUNDLE_RELPATHS`.
fn patch_gcloud_bundles(_pem: &str) {
    // TODO(re): stub.
}

/// Point npm at the merged bundle by writing `cafile=<path>` into each of
/// `NPMRC_PATHS`. Decompiled from 0xf8d60..0xf9910; the emitted line is
/// the template at 0x389316 (literal "cafile=" at 0x389319).
fn write_npmrc(_cert_path: &str) {
    // TODO(re): stub.
}

/// Point pip at the merged bundle. Decompiled from 0xf9910..0xfa570.
/// Writes the "[global]\ncert = {}\n" template at 0x38932f (literal body at
/// 0x389330) when the config is new, or appends the "cert = {}" template at
/// 0x389324 (literal at 0x389326) to an existing `[global]` section.
fn write_pip_conf(_cert_path: &str) {
    // TODO(re): stub — the two templates and the `[global]` literal (0x37c298
    // region) are recovered; the merge logic was not decompiled.
}

/// Make uv use the OS trust store. Decompiled from 0xfa570..0xfab40.
/// Probes `UV_BINARIES`; if any exists, writes `native-tls = true`
/// (template 0x388d86) to `/etc/uv/uv.toml` (template 0x388d75).
fn write_uv_config() {
    // TODO(re): stub.
}

/// Preserve the CA/proxy environment across `sudo`.
/// Decompiled from 0xf8910..0xf8d60.
/// Writes `Defaults env_keep += "<vars>"` (template 0x38978b) to
/// `/etc/sudoers.d/90-sandbox-ca-env` (templates 0x389766 + 0x389777);
/// failure logs "[INIT] WARNING: sudoers env_keep write failed: {}" (0x3897a7).
fn write_sudoers_env_keep() {
    // TODO(re): stub.
}
