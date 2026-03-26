//! Reverse-engineered from process_api BuildID 91c789ff2a9e647bf7b1914e351f67b89713c4ef
//! release process_api_2026-03-23-22-49
//!
//! Main entry point: CLI argument parsing, optional Firecracker VM init,
//! WebSocket listener (TCP, UDS, or vsock), SIGINT handler,
//! control server initialization, cgroup setup, and task spawning.
//!
//! Changes in 91c789ff vs e409c31a (Binary: 91c789ff):
//!   - /etc/hostname, /etc/hosts, /etc/resolv.conf (nameserver 8.8.8.8) setup at init
//!   - fuse_spawn log prefix replaces fuse_mounts in FUSE setup messages
//!   - /dev/vdb support added alongside /dev/vda for root/model-tools mounts
//!   - Release string updated to process_api_2026-03-23-22-49
//!
//! Source path: /root/code/sandboxing/sandboxing/server/process_api/src/main.rs
//!   (old: /root/src/tree/coworker_did-.../sandboxing/...)
//!
//! String refs (CLI argument definitions):
//!   "process_api", "firecracker_init", "FIRECRACKER_INIT", "firecracker-init",
//!   "listen_uds", "LISTEN_UDS", "listen-uds",
//!   "control_vsock_port", "CONTROL_VSOCK_PORT", "control-vsock-port",
//!   "listen_vsock_port", "LISTEN_VSOCK_PORT", "listen-vsock-port",
//!
//! String refs:
//!   "Failed to bind TCP", "Failed to set UDS socket permissions",
//!   "Invalid control server address format (expected e.g., '0.0.0.0:2025')",
//!   "[DEBUG] SIGINT handler enabled (control server disabled)",
//!   "[INIT] Dropped CAP_SYS_RESOURCE from bounding set"

mod adopter;
mod cgroup;
mod control_server;
mod firecracker_init;
mod io;
mod oom_killer;
mod pid_tree;
mod proc_handle;
mod state;

use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use parking_lot::Mutex;
use tokio::net::TcpListener;
use tokio::sync::broadcast;
use tokio_tungstenite::accept_async;

/// CLI arguments (Binary: 91c789ff — same flags as e409c31a, no new flags).
/// Decompiled from serde visitor at binary offset 0x28ea5c.
#[derive(Parser, Debug)]
#[command(name = "process_api")]
struct Cli {
    /// Address to listen on for WebSocket connections (e.g., "0.0.0.0:2024")
    #[arg(long, env = "ADDR")]
    addr: Option<String>,

    /// Maximum WebSocket buffer size in bytes
    #[arg(long, env = "MAX_WS_BUFFER_SIZE", default_value = "32768")]
    max_ws_buffer_size: usize,

    /// Container-level memory limit in bytes
    #[arg(long, env = "MEMORY_LIMIT_BYTES")]
    memory_limit_bytes: Option<u64>,

    /// CPU shares for cgroup (v1) or cpu.weight (v2)
    #[arg(long, env = "CPU_SHARES")]
    cpu_shares: Option<u64>,

    /// OOM polling period in milliseconds
    #[arg(long, env = "OOM_POLLING_PERIOD_MS", default_value = "100")]
    oom_polling_period_ms: u64,

    /// Force cgroup v2 mode
    #[arg(long, env = "CGROUPV2")]
    cgroupv2: bool,

    /// Control server address for graceful shutdown and container name updates.
    /// When set, SIGINT handler is disabled to prevent duplicate shutdown signals.
    #[arg(long, env = "CONTROL_SERVER_ADDR")]
    control_server_addr: Option<String>,

    /// Block connections from localhost and own interface IPs
    #[arg(long, env = "BLOCK_LOCAL_CONNECTIONS")]
    block_local_connections: bool,

    /// Listen on a Unix domain socket instead of TCP
    #[arg(long, env = "LISTEN_UDS")]
    listen_uds: Option<String>,

    /// Control server vsock port for graceful shutdown and container name updates
    #[arg(long, env = "CONTROL_VSOCK_PORT")]
    control_vsock_port: Option<u32>,

    /// Listen on a vsock port for WebSocket connections (Firecracker)
    #[arg(long, env = "LISTEN_VSOCK_PORT")]
    listen_vsock_port: Option<u32>,

    /// Run as Firecracker VM init (PID 1). Mounts /proc, /sys, /dev, sets up
    /// networking, then either reads /mount_config.json (fresh boot) or waits
    /// for POST /mount_root (snapstart). Also enables the /mount_root endpoint
    /// on the control server. Has no effect on gVisor/runc where the rootfs is
    /// already set up by the host.
    #[arg(long, env = "FIRECRACKER_INIT")]
    firecracker_init: bool,
}

/// Check if an IP address is a local/loopback address.
pub fn is_local_ip(addr: &IpAddr) -> bool {
    match addr {
        IpAddr::V4(v4) => v4.is_loopback() || v4.is_unspecified(),
        IpAddr::V6(v6) => v6.is_loopback() || v6.is_unspecified(),
    }
}

/// Main entry point.
///
/// Changes in 91c789ff:
/// - Release string updated to process_api_2026-03-23-22-49
/// - fuse_spawn log prefix (replaces fuse_mounts) in firecracker_init
#[tokio::main]
async fn main() {
    env_logger::init();

    let cli = Cli::parse();

    // Run Firecracker VM init if --firecracker-init is set
    // This runs BEFORE the async runtime services start
    if cli.firecracker_init {
        firecracker_init::run_firecracker_init();
    }

    // Log version info
    // Binary: 91c789ff
    log::info!("[INFO] process_api release: process_api_2026-03-23-22-49");
    log::info!("[INFO] process_api package version: 0.1.0");

    // Broadcast channel for shutdown signaling
    let (shutdown_tx, _) = broadcast::channel::<()>(16);

    // Set up cgroup with retry loop
    let controller = loop {
        match cgroup::setup_cgroup(cli.cgroupv2).await {
            Ok(c) => {
                log::debug!("[DEBUG] Cgroup setup successful: {c:?}");
                break c;
            }
            Err(e) => {
                log::error!(
                    "Failed to create cgroup for process api: {e}. Sleeping for 10 seconds..."
                );
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        }
    };

    // Set CPU shares if configured
    if let Some(shares) = cli.cpu_shares {
        if let Err(e) =
            cgroup::set_cpu_shares(&controller.base_path, controller.version, shares).await
        {
            log::warn!("Failed to set CPU shares: {e}");
        }
    }

    // Detect container name from /container_info.json (if present).
    let container_name: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(detect_container_name()));

    // Create shared process map
    let proc_map = state::new_process_map();

    // Create shared OOM channel registry
    let oom_channels = oom_killer::new_oom_channel_map();

    // Determine if mount_root endpoint should be enabled on control server
    let mount_root_enabled = cli.firecracker_init;

    // Start control server (TCP, vsock, or none)
    let has_control_server = cli.control_server_addr.is_some() || cli.control_vsock_port.is_some();

    if let Some(ref control_addr_str) = cli.control_server_addr {
        let addr: SocketAddr = match control_addr_str.parse() {
            Ok(a) => a,
            Err(_) => {
                log::error!(
                    "Invalid control server address format (expected e.g., '0.0.0.0:2025'): {control_addr_str}"
                );
                return;
            }
        };

        log::debug!(
            "[DEBUG] Control server enabled on {addr} (SIGINT handler disabled, mount_root={mount_root_enabled})"
        );

        let shutdown_tx_clone = shutdown_tx.clone();
        let shutdown_rx = shutdown_tx.subscribe();
        let container_name_clone = container_name.clone();
        let proc_map_clone = proc_map.clone();
        let controller_clone = controller.clone();

        tokio::spawn(async move {
            control_server::start_control_server(
                addr,
                shutdown_tx_clone,
                container_name_clone,
                shutdown_rx,
                proc_map_clone,
                controller_clone,
                mount_root_enabled,
            )
            .await;
        });
    }

    if let Some(vsock_port) = cli.control_vsock_port {
        log::debug!(
            "[DEBUG] Control server enabled on vsock port {vsock_port} (SIGINT handler disabled, mount_root={mount_root_enabled})"
        );

        let shutdown_tx_clone = shutdown_tx.clone();
        let shutdown_rx = shutdown_tx.subscribe();
        let container_name_clone = container_name.clone();
        let proc_map_clone = proc_map.clone();
        let controller_clone = controller.clone();

        tokio::spawn(async move {
            control_server::start_vsock_control_server(
                vsock_port,
                shutdown_tx_clone,
                container_name_clone,
                shutdown_rx,
                proc_map_clone,
                controller_clone,
                mount_root_enabled,
            )
            .await;
        });
    }

    if !has_control_server {
        // Set up SIGINT handler when no control server is configured
        log::debug!("[DEBUG] SIGINT handler enabled (control server disabled)");

        let shutdown_tx_clone = shutdown_tx.clone();
        tokio::spawn(async move {
            match tokio::signal::ctrl_c().await {
                Ok(()) => {
                    log::info!("[DEBUG] Received SIGINT, initiating shutdown");
                    let _ = shutdown_tx_clone.send(());
                }
                Err(e) => {
                    log::error!("Failed to listen for SIGINT: {e}");
                }
            }
        });
    }

    // Start orphan monitor
    let orphan_shutdown_rx = shutdown_tx.subscribe();
    let orphan_controller = controller.clone();
    let orphan_proc_map = proc_map.clone();
    tokio::spawn(async move {
        adopter::monitor_orphans(orphan_controller, orphan_proc_map, orphan_shutdown_rx).await;
    });

    // Start container-level OOM monitor if memory limit is set
    if let Some(memory_limit) = cli.memory_limit_bytes {
        let oom_shutdown_rx = shutdown_tx.subscribe();
        let oom_controller = controller.clone();
        let oom_proc_map = proc_map.clone();
        let polling = Duration::from_millis(cli.oom_polling_period_ms);
        let oom_channels_clone = oom_channels.clone();

        tokio::spawn(async move {
            oom_killer::container_oom_monitor(
                oom_controller,
                memory_limit,
                polling,
                oom_proc_map,
                oom_shutdown_rx,
                oom_channels_clone,
            )
            .await;
        });
    }

    // Log blocked IPs if configured
    if cli.block_local_connections {
        log::info!("[SECURITY] Blocking connections from local IPs: 127.0.0.1, ::1, 0.0.0.0, ::");
    }

    // Bind the main WebSocket listener (TCP, UDS, or vsock)
    // Priority: vsock > UDS > TCP
    if let Some(vsock_port) = cli.listen_vsock_port {
        // Vsock WebSocket listener
        log::info!(
            "Listening on vsock port: {} with web socket buffer size of {}",
            vsock_port,
            cli.max_ws_buffer_size
        );
        run_vsock_ws_listener(
            vsock_port,
            proc_map,
            controller,
            cli.memory_limit_bytes,
            Duration::from_millis(cli.oom_polling_period_ms),
            container_name,
            shutdown_tx,
            oom_channels,
            cli.block_local_connections,
        )
        .await;
    } else if let Some(ref uds_path) = cli.listen_uds {
        // UDS WebSocket listener
        run_uds_ws_listener(
            uds_path,
            proc_map,
            controller,
            cli.memory_limit_bytes,
            Duration::from_millis(cli.oom_polling_period_ms),
            container_name,
            shutdown_tx,
            oom_channels,
            cli.max_ws_buffer_size,
        )
        .await;
    } else if let Some(ref addr) = cli.addr {
        // TCP WebSocket listener (original path)
        let listener = match TcpListener::bind(addr).await {
            Ok(l) => {
                log::info!(
                    "Listening on: {} with web socket buffer size of {}",
                    addr,
                    cli.max_ws_buffer_size
                );
                l
            }
            Err(e) => {
                log::error!("Failed to bind to {addr}: {e}");
                return;
            }
        };

        let mut shutdown_rx = shutdown_tx.subscribe();
        log::debug!("[DEBUG] got shutdown channel rx");

        // Main WebSocket accept loop
        loop {
            tokio::select! {
                accept = listener.accept() => {
                    match accept {
                        Ok((stream, remote_addr)) => {
                            if cli.block_local_connections && is_local_ip(&remote_addr.ip()) {
                                log::warn!(
                                    "[SECURITY] Rejected WebSocket connection from local IP {remote_addr}"
                                );
                                continue;
                            }

                            log::debug!("New WebSocket connection: {remote_addr}");

                            let proc_map = proc_map.clone();
                            let controller = controller.clone();
                            let container_name = container_name.clone();
                            let shutdown_tx = shutdown_tx.clone();
                            let memory_limit = cli.memory_limit_bytes;
                            let polling = Duration::from_millis(cli.oom_polling_period_ms);
                            let oom_channels = oom_channels.clone();

                            tokio::spawn(async move {
                                match accept_async(stream).await {
                                    Ok(ws_stream) => {
                                        io::handle_ws_connection(
                                            ws_stream,
                                            remote_addr,
                                            proc_map,
                                            controller,
                                            memory_limit,
                                            polling,
                                            container_name,
                                            shutdown_tx,
                                            oom_channels,
                                        )
                                        .await;
                                    }
                                    Err(e) => {
                                        log::debug!("Error during websocket handshake: {e}");
                                    }
                                }
                            });
                        }
                        Err(e) => {
                            log::error!("Error accepting connection: {e}");
                        }
                    }
                }
                _ = shutdown_rx.recv() => {
                    log::info!("[INFO] Received shutdown signal, exiting main loop");
                    log::info!("Performing graceful shutdown...");
                    break;
                }
            }
        }

        graceful_shutdown(&proc_map).await;
    } else {
        log::error!("No listener configured. Provide --addr, --listen-uds, or --listen-vsock-port");
        return;
    }
}

/// Detect the container name from `/container_info.json`.
/// Returns `None` if the file is missing, unparseable, or lacks a `container_name` field.
/// Xrefs: "[DEBUG] Read container name from /container_info.json: ",
///   "[DEBUG] Failed to read /container_info.json: ",
///   "[DEBUG] Failed to parse /container_info.json: ",
///   "[DEBUG] container_name field not found in /container_info.json"
fn detect_container_name() -> Option<String> {
    match std::fs::read_to_string("/container_info.json") {
        Ok(contents) => match serde_json::from_str::<serde_json::Value>(&contents) {
            Ok(val) => match val.get("container_name").and_then(|v| v.as_str()) {
                Some(name) => {
                    log::debug!("[DEBUG] Read container name from /container_info.json: {name}");
                    Some(name.to_string())
                }
                None => {
                    log::debug!("[DEBUG] container_name field not found in /container_info.json");
                    None
                }
            },
            Err(e) => {
                log::debug!("[DEBUG] Failed to parse /container_info.json: {e}");
                None
            }
        },
        Err(e) => {
            log::debug!("[DEBUG] Failed to read /container_info.json: {e}");
            None
        }
    }
}

/// Graceful shutdown: kill all tracked processes.
async fn graceful_shutdown(proc_map: &state::ProcessMap) {
    log::info!("[INFO] Shutting down, killing all tracked processes");
    let processes: Vec<(String, u32)> = {
        let map = proc_map.lock();
        map.iter()
            .map(|(id, entry)| (id.clone(), entry.pid))
            .collect()
    };

    for (process_id, pid) in processes {
        log::debug!("[DEBUG] Killing process {process_id} (PID {pid}) during shutdown");
        proc_handle::kill_and_wait(pid, None).await;
        state::remove_process(proc_map, &process_id);
    }

    log::info!("All connections and monitors closed, shutting down");
    log::info!("[INFO] process_api shutdown complete");
}

/// Run the vsock WebSocket listener.
/// Xrefs: "Listening on vsock port: ...",
///   "[SECURITY] Rejecting vsock connection from non-host CID ...",
///   "Error accepting vsock connection: ..."
///
/// NOTE: The real binary uses tokio-vsock (AF_VSOCK) sockets here, not Unix
/// domain sockets. We use tokio::net::UnixListener as a stand-in because
/// tokio-vsock is not available in our current deps (commented out in
/// BUILD.bazel). When tokio-vsock is added, replace the UnixListener with
/// VsockListener::bind(VMADDR_CID_ANY, port) and validate peer CID == 2.
#[allow(clippy::too_many_arguments)]
async fn run_vsock_ws_listener(
    port: u32,
    proc_map: state::ProcessMap,
    controller: cgroup::CgroupController,
    memory_limit: Option<u64>,
    polling: Duration,
    container_name: Arc<Mutex<Option<String>>>,
    shutdown_tx: broadcast::Sender<()>,
    oom_channels: oom_killer::OomChannelMap,
    _block_local: bool,
) {
    // STUB: Real binary uses tokio-vsock (AF_VSOCK). This is a UDS placeholder
    // that does NOT match the binary's behavior. See BUILD.bazel for missing dep.

    // TODO: Replace with tokio_vsock::VsockListener::bind(VMADDR_CID_ANY, port)
    // when tokio-vsock is available. The real binary:
    //   1. Binds a VsockListener on CID=VMADDR_CID_ANY (u32::MAX), port=`port`
    //   2. Accepts connections, checks peer CID == 2 (host only)
    //   3. Rejects non-host CIDs with "[SECURITY] Rejecting vsock connection from non-host CID ..."
    //   4. Wraps accepted stream in accept_async for WebSocket upgrade
    //   5. Passes to handle_ws_connection (same as TCP/UDS paths)

    // Stand-in: bind a UDS at /tmp/vsock-ws-{port}.sock to allow compilation.
    let uds_path = format!("/tmp/vsock-ws-{port}.sock");
    if std::path::Path::new(&uds_path).exists() {
        let _ = std::fs::remove_file(&uds_path);
    }

    let listener = match tokio::net::UnixListener::bind(&uds_path) {
        Ok(l) => l,
        Err(e) => {
            log::error!(
                "[ERROR] Failed to bind vsock port {port} (UDS stand-in at {uds_path}): {e}"
            );
            return;
        }
    };

    let mut shutdown_rx = shutdown_tx.subscribe();
    log::debug!("[DEBUG] got shutdown channel rx");

    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, _addr)) => {
                        // In the real binary, the peer CID is checked here:
                        //   if peer_cid != 2 {
                        //       log::warn!("[SECURITY] Rejecting vsock connection from non-host CID {peer_cid}");
                        //       continue;
                        //   }
                        log::debug!("New WebSocket connection: vsock");

                        let proc_map = proc_map.clone();
                        let controller = controller.clone();
                        let container_name = container_name.clone();
                        let shutdown_tx = shutdown_tx.clone();
                        let oom_channels = oom_channels.clone();

                        tokio::spawn(async move {
                            let remote_addr: std::net::SocketAddr =
                                "0.0.0.0:0".parse().unwrap();
                            match accept_async(stream).await {
                                Ok(ws_stream) => {
                                    io::handle_ws_connection(
                                        ws_stream,
                                        remote_addr,
                                        proc_map,
                                        controller,
                                        memory_limit,
                                        polling,
                                        container_name,
                                        shutdown_tx,
                                        oom_channels,
                                    )
                                    .await;
                                }
                                Err(e) => {
                                    log::debug!("Error accepting vsock connection: {e}");
                                }
                            }
                        });
                    }
                    Err(e) => {
                        log::error!("Error accepting vsock connection: {e}");
                    }
                }
            }
            _ = shutdown_rx.recv() => {
                log::info!("[INFO] Received shutdown signal, exiting main loop");
                log::info!("Performing graceful shutdown...");
                break;
            }
        }
    }

    graceful_shutdown(&proc_map).await;
}

/// Run the UDS (Unix domain socket) WebSocket listener.
/// Xrefs: "Listening on UDS: ...",
///   "[WARN] Failed to remove existing UDS socket file: ...",
///   "[DEBUG] UDS parent dir ...",
///   "[ERROR] Failed to bind UDS at ...",
///   "Error accepting UDS connection: ..."
#[allow(clippy::too_many_arguments)]
async fn run_uds_ws_listener(
    uds_path: &str,
    proc_map: state::ProcessMap,
    controller: cgroup::CgroupController,
    memory_limit: Option<u64>,
    polling: Duration,
    container_name: Arc<Mutex<Option<String>>>,
    shutdown_tx: broadcast::Sender<()>,
    oom_channels: oom_killer::OomChannelMap,
    _max_ws_buffer_size: usize,
) {
    // Remove existing socket file
    if std::path::Path::new(uds_path).exists() {
        if let Err(e) = std::fs::remove_file(uds_path) {
            log::warn!("[WARN] Failed to remove existing UDS socket file: {e}");
        }
    }

    // Ensure parent directory exists
    if let Some(parent) = std::path::Path::new(uds_path).parent() {
        log::debug!("[DEBUG] UDS parent dir {}", parent.display());
        let _ = std::fs::create_dir_all(parent);
    }

    let listener = match tokio::net::UnixListener::bind(uds_path) {
        Ok(l) => {
            log::info!("Listening on UDS: {uds_path}");
            // Set socket permissions to 0o777
            if let Err(e) =
                std::fs::set_permissions(uds_path, std::fs::Permissions::from_mode(0o777))
            {
                log::error!("Failed to set UDS socket permissions: {e}");
            }
            l
        }
        Err(e) => {
            log::error!("[ERROR] Failed to bind UDS at {uds_path}: {e}");
            return;
        }
    };

    let mut shutdown_rx = shutdown_tx.subscribe();
    log::debug!("[DEBUG] got shutdown channel rx");

    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, _addr)) => {
                        log::debug!("New WebSocket connection from UDS");

                        let proc_map = proc_map.clone();
                        let controller = controller.clone();
                        let container_name = container_name.clone();
                        let shutdown_tx = shutdown_tx.clone();
                        let oom_channels = oom_channels.clone();

                        tokio::spawn(async move {
                            // UDS connections use a dummy remote address
                            let remote_addr: std::net::SocketAddr =
                                "0.0.0.0:0".parse().unwrap();
                            match accept_async(stream).await {
                                Ok(ws_stream) => {
                                    io::handle_ws_connection(
                                        ws_stream,
                                        remote_addr,
                                        proc_map,
                                        controller,
                                        memory_limit,
                                        polling,
                                        container_name,
                                        shutdown_tx,
                                        oom_channels,
                                    )
                                    .await;
                                }
                                Err(e) => {
                                    log::debug!("Error during websocket handshake: {e}");
                                }
                            }
                        });
                    }
                    Err(e) => {
                        log::error!("Error accepting UDS connection: {e}");
                    }
                }
            }
            _ = shutdown_rx.recv() => {
                log::info!("[INFO] Received shutdown signal, exiting main loop");
                log::info!("Performing graceful shutdown...");
                break;
            }
        }
    }

    graceful_shutdown(&proc_map).await;
}

use std::os::unix::fs::PermissionsExt;
