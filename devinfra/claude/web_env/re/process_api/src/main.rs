// RE code: allow dead code and clippy pedantry — incomplete reconstruction by design.
#![allow(dead_code, unused_variables, unused_imports)]
#![allow(
    clippy::let_and_return,
    clippy::manual_memcpy,
    clippy::doc_overindented_list_items
)]
//! Reverse-engineered from process_api BuildID edebff2c28de76238c95c299ba3401a9098c9e17
//! release process_api_2026-05-11-18-55
//!
//! Main entry point: CLI argument parsing, optional Firecracker VM init,
//! WebSocket listener (TCP, UDS, vsock, or dial-uds), SIGINT handler,
//! control server initialization, cgroup setup, and task spawning.
//!
//! CLI surface is unchanged in edebff2c: the clap definition blob at
//! 0x39abaf..0x39b2c0 matches 810fd3a4's at 0x2a9126 flag for flag
//! (--addr, --max-ws-buffer-size, --memory-limit-bytes, --cpu-shares,
//! --oom-polling-period-ms, --cgroupv2, --control-server-addr,
//! --block-local-connections, --listen-uds, --dial-uds, --listen-vsock-port,
//! --control-vsock-port, --firecracker-init), including help text.
//!
//! New in edebff2c: `graceful_shutdown` bounds its wait with a 1-second grace
//! period and warns to stderr when tasks remain (template 0x38c516,
//! emitted from the shutdown driver at 0x154810; the three inlined copies are
//! at 0x15838e, 0x180044 and 0x1b8c7e).
//!
//! Source path (edebff2c panic-location table): src/main.rs — edebff2c builds
//! with --remap-path-prefix, so every application module now appears as a bare
//! src/*.rs path instead of 810fd3a4's
//! /root/src/tree/marcus-process-api/sandboxing/sandboxing/server/process_api/ prefix.
//!
//! String refs (CLI argument definitions):
//!   "process_api", "firecracker_init", "FIRECRACKER_INIT", "firecracker-init",
//!   "listen_uds", "LISTEN_UDS", "listen-uds",
//!   "dial_uds", "DIAL_UDS", "dial-uds",
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
mod platform;
mod proc_handle;
mod state;
mod ws_compression;

use std::net::{IpAddr, SocketAddr};
use std::os::unix::fs::PermissionsExt;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use parking_lot::Mutex;
use tokio::net::TcpListener;
use tokio::sync::broadcast;
use tokio_tungstenite::accept_async;
use tokio_vsock::{VMADDR_CID_ANY, VsockAddr, VsockListener};

/// CLI arguments (Binary: edebff2c).
/// Decompiled from the clap definition blob at 0x39abaf..0x39b2c0; unchanged
/// from 810fd3a4's 0x2a9126 blob apart from its address.
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

    /// Dial out to a host-side UDS bridge instead of binding a listener.
    /// Used with gVisor --host-uds=open, where bind() on gofer-backed paths
    /// falls back to a sentry-synthetic dentry the host cannot reach. Each
    /// dialed connection is handed to the WS server handshake.
    /// Binary: 810fd3a4 — new flag.
    #[arg(long, env = "DIAL_UDS")]
    dial_uds: Option<String>,

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
/// Binary: edebff2c — release string "process_api_2026-05-11-18-55" (0x39aaf0).
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
    // Binary: edebff2c — "[INFO] process_api release: ..." at 0x39aaf0
    log::info!("[INFO] process_api release: process_api_2026-05-11-18-55");
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

    // Bind the main WebSocket listener (TCP, UDS, dial-uds, or vsock)
    // Priority: vsock > dial-uds > UDS > TCP
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
    } else if let Some(ref dial_path) = cli.dial_uds {
        // Dial-UDS WebSocket listener (Binary: 810fd3a4 — new)
        log::debug!("[DEBUG] --dial-uds enabled: {dial_path}");
        run_dial_uds_ws_listener(
            dial_path,
            proc_map,
            controller,
            cli.memory_limit_bytes,
            Duration::from_millis(cli.oom_polling_period_ms),
            container_name,
            shutdown_tx,
            oom_channels,
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
        log::error!(
            "No listener configured. Provide --addr, --listen-uds, --dial-uds, or --listen-vsock-port"
        );
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

    // Binary: edebff2c — after the shutdown broadcast, the driver waits one
    // second for outstanding tasks and, if any survive, prints to stderr:
    //   "[WARN] {} task(s) still alive after 1s shutdown grace, aborting"
    // (template 0x38c516; the "stderr" target string is 0x39e358, loaded with
    // len 6 at each of the three inlined copies).
    // TODO(re): the source of the surviving-task count (a JoinSet length or a
    // task-tracker counter, read from 0x98(%r13) at 0x158344) was not traced.
    let still_alive = remaining_task_count();
    if still_alive > 0 {
        eprintln!("[WARN] {still_alive} task(s) still alive after 1s shutdown grace, aborting");
    }

    log::info!("[INFO] process_api shutdown complete");
}

/// Number of spawned tasks still running after the shutdown grace period.
///
/// STUB: returns a plausible default. The binary reads this count from
/// `0x98(%r13)` in the shutdown driver (fn 0x154810) — the owning structure was
/// not identified, so the accounting mechanism is unknown.
fn remaining_task_count() -> usize {
    0
}

/// Run the vsock WebSocket listener.
/// Binary: 810fd3a4 — real tokio-vsock 0.7.2 implementation.
/// Xrefs: "Listening on vsock port: ...",
///   "[SECURITY] Rejecting vsock connection from non-host CID ...",
///   "Error accepting vsock connection: ..."
///
/// Binds a VsockListener on CID=VMADDR_CID_ANY, validates peer CID == 2 (host).
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
    let listener = match VsockListener::bind(VsockAddr::new(VMADDR_CID_ANY, port)) {
        Ok(l) => l,
        Err(e) => {
            log::error!("[ERROR] Failed to bind vsock port {port}: {e}");
            return;
        }
    };

    let mut shutdown_rx = shutdown_tx.subscribe();
    log::debug!("[DEBUG] got shutdown channel rx");

    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, peer_addr)) => {
                        let peer_cid = peer_addr.cid();
                        if peer_cid != 2 {
                            log::warn!(
                                "[SECURITY] Rejecting vsock connection from non-host CID {peer_cid}"
                            );
                            continue;
                        }
                        log::debug!("New WebSocket connection: vsock cid: {peer_cid}");

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

/// Run the dial-UDS WebSocket listener (Binary: 810fd3a4 — new).
/// Instead of binding a listener, dials OUT to a host-side UDS bridge.
/// Used with gVisor --host-uds=open where bind() on gofer-backed paths
/// falls back to a sentry-synthetic dentry the host cannot reach.
/// Each dialed connection is handed to the WS server handshake (direction
/// reversal: the Router on the other side sends the HTTP Upgrade).
///
/// Xrefs: "[DEBUG] --dial-uds enabled: ...",
///   "[DEBUG] dial-uds: ...", "[DEBUG] dial-uds not ready (...)",
///   "New WebSocket connection: dial-uds",
///   "dial-uds ws handshake error: ...",
///   "parent dir absent", ".no_bridge sentinel present",
///   "RouterPlugin pending), retrying",
///   "restored on a server without dp_mtls. TCP :2024 remains available."
#[allow(clippy::too_many_arguments)]
async fn run_dial_uds_ws_listener(
    uds_path: &str,
    proc_map: state::ProcessMap,
    controller: cgroup::CgroupController,
    memory_limit: Option<u64>,
    polling: Duration,
    container_name: Arc<Mutex<Option<String>>>,
    shutdown_tx: broadcast::Sender<()>,
    oom_channels: oom_killer::OomChannelMap,
) {
    let mut shutdown_rx = shutdown_tx.subscribe();

    // Check for .no_bridge sentinel — if present, dial-uds is not available
    // (e.g., restored on a server without dp_mtls).
    let no_bridge_path = format!("{uds_path}.no_bridge");
    if std::path::Path::new(&no_bridge_path).exists() {
        log::info!(
            ".no_bridge sentinel present — restored on a server without dp_mtls. TCP :2024 remains available."
        );
        // Fall through to shutdown wait — TCP listener is the fallback.
        let _ = shutdown_rx.recv().await;
        graceful_shutdown(&proc_map).await;
        return;
    }

    loop {
        // Check parent dir exists
        if let Some(parent) = std::path::Path::new(uds_path).parent() {
            if !parent.exists() {
                log::debug!("[DEBUG] dial-uds not ready (parent dir absent)");
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(500)) => { continue; }
                    _ = shutdown_rx.recv() => { break; }
                }
            }
        }

        // Dial out to the UDS path
        log::debug!("[DEBUG] dial-uds: {uds_path}");
        match tokio::net::UnixStream::connect(uds_path).await {
            Ok(stream) => {
                log::debug!("New WebSocket connection: dial-uds");

                let proc_map = proc_map.clone();
                let controller = controller.clone();
                let container_name = container_name.clone();
                let shutdown_tx = shutdown_tx.clone();
                let oom_channels = oom_channels.clone();

                tokio::spawn(async move {
                    let remote_addr: std::net::SocketAddr = "0.0.0.0:0".parse().unwrap();
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
                            log::debug!("dial-uds ws handshake error: {e}");
                        }
                    }
                });
            }
            Err(e) => {
                // Connection not ready — RouterPlugin may still be pending
                log::debug!("[DEBUG] dial-uds not ready ({e}, RouterPlugin pending), retrying");
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(500)) => { continue; }
                    _ = shutdown_rx.recv() => { break; }
                }
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
