//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Main entry point: CLI argument parsing, WebSocket listener, SIGINT handler,
//! control server initialization, cgroup setup, and task spawning.
//!
//! Functions decompiled from:
//!   main_async_entry:            0x2273c0..0x232177  (44471 bytes)
//!   cli_builder:                 0x209200..0x20ca80  (14464 bytes)
//!   cli_parser_init:             0x20d0c0..0x21199e  (18654 bytes)
//!   container_name_detection:    0x2089f0..0x2091fe  (2062 bytes)
//!   socket_bind_listen:          0x13f6a0..0x13faff  (1119 bytes)
//!
//! String refs at binary offset 0x2b9546 (control_server.rs strings overlap):
//!   "Invalid control server address format"
//!   "[DEBUG] Control server enabled on (SIGINT handler disabled)"
//!   "[DEBUG] SIGINT handler enabled (control server disabled)"
//!   "New WebSocket connection:"
//!   "[SECURITY] Rejected WebSocket connection from local IP"
//!   "Error accepting connection:"
//!   "Error during websocket handshake:"
//!   "main.rsFailed to bind"
//!
//! String refs at binary offset 0x1b9b00 (CLI argument definitions):
//!   "process_api", "block_local_connections", "SIGINT",
//!   "CONTROL_SERVER_ADDR", "memory_limit_bytes", "oom_polling_period_ms",
//!   "addr", "cpu_shares", "max_ws_buffer_size", "Cli",
//!   "MAX_WS_BUFFER_SIZE", "max-ws-buffer-size", "32768",
//!   "MEMORY_LIMIT_BYTES", "memory-limit-bytes",
//!   "CPU_SHARES", "cpu-shares",
//!   "OOM_POLLING_PERIOD_MS", "oom-polling-period-ms", "100",
//!   "CONTROL_SERVER_ADDR", "control-server-addr",
//!   "Control server address (e.g., \"0.0.0.0:2025\")",
//!   "When set, SIGINT handler is disabled to prevent duplicate shutdown signals",
//!   "BLOCK_LOCAL_CONNECTIONS", "block-local-connections",
//!   "Block connections from localhost and own interface IPs"
//!
//! String refs at binary offset 0x2b9934 (container name detection):
//!   "[DEBUG] Using container name from control server:"
//!   "/container_info.json"
//!   "container_name"
//!   "[DEBUG] Failed to read container..."
//!   "[DEBUG] Failed to parse container..."
//!   "[DEBUG] Read container name from..."
//!   "[DEBUG] container_name field not..."
//!
//! String refs at binary offset 0x2b9924 (signal handling):
//!   "Caught signal !"
//!
//! String refs from cli_parser_init (0x20d0c0):
//!   "[INFO] process_api release"
//!   "[INFO] process_api package versi..."
//!   "Failed to create cgroup for proc..."

mod adopter;
mod cgroup;
mod control_server;
mod io;
mod oom_killer;
mod pid_tree;
mod proc_handle;
mod state;

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::time::Duration;

use clap::Parser;
use tokio::net::TcpListener;
use tokio::sync::broadcast;
use tokio_tungstenite::accept_async;

/// Decompiled from 0x209200..0x20ca80  (14464 bytes)
/// Xrefs: "block_local_connections", "SIGINT", "CONTROL_SERVER_ADDR",
///   "memory_limit_bytes", "oom_polling_period_ms", "addr", "cpu_shares",
///   "max_ws_buffer_size", "Cli", "process_api"
#[derive(Parser, Debug)]
#[command(name = "process_api")]
struct Cli {
    /// Address to listen on for WebSocket connections (e.g., "0.0.0.0:8080")
    #[arg(long, env = "ADDR")]
    addr: String,

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
}

/// Decompiled from 0x2089f0..0x2091fe  (2062 bytes)
/// Xrefs: "[DEBUG] Using container name from control server:",
///   "/container_info.json", "container_name",
///   "[DEBUG] Failed to read container...", "[DEBUG] Read container name from..."
async fn detect_container_name() -> Option<String> {
    // Try to read container name from /container_info.json
    match tokio::fs::read_to_string("/container_info.json").await {
        Ok(contents) => {
            match serde_json::from_str::<serde_json::Value>(&contents) {
                Ok(json) => {
                    if let Some(name) = json.get("container_name").and_then(|v| v.as_str()) {
                        log::debug!("[DEBUG] Read container name from /container_info.json: {name}");
                        return Some(name.to_string());
                    }
                    log::debug!("[DEBUG] container_name field not found in /container_info.json");
                }
                Err(e) => {
                    log::debug!("[DEBUG] Failed to parse container info JSON: {e}");
                }
            }
        }
        Err(e) => {
            log::debug!("[DEBUG] Failed to read container info: {e}");
        }
    }

    None
}

/// Check if an IP address is a local/loopback address.
fn is_local_ip(addr: &IpAddr) -> bool {
    match addr {
        IpAddr::V4(v4) => v4.is_loopback() || v4.is_unspecified(),
        IpAddr::V6(v6) => v6.is_loopback() || v6.is_unspecified(),
    }
}

/// Decompiled from 0x2273c0..0x232177  (44471 bytes)  — main async runtime entry
/// and 0x20d0c0..0x21199e  (18654 bytes) — CLI parser/init
/// and 0x13f6a0..0x13faff  (1119 bytes)  — socket bind/listen
///
/// Xrefs: "Invalid control server address format",
///   "[DEBUG] Control server enabled on (SIGINT handler disabled)",
///   "[DEBUG] SIGINT handler enabled (control server disabled)",
///   "New WebSocket connection:", "[SECURITY] Rejected WebSocket connection from local IP",
///   "Error accepting connection:", "Error during websocket handshake:",
///   "[INFO] process_api release", "[INFO] process_api package versi...",
///   "Failed to create cgroup for proc..."
#[tokio::main]
async fn main() {
    env_logger::init();

    let cli = Cli::parse();

    // Log version info
    log::info!("[INFO] process_api release process_api_2026-02-02-04-57");
    log::info!("[INFO] process_api package version 0.1.0");

    // Broadcast channel for shutdown signaling
    let (shutdown_tx, _) = broadcast::channel::<()>(16);

    // Set up cgroup
    let controller = match cgroup::setup_cgroup(cli.cgroupv2).await {
        Ok(c) => {
            log::debug!("[DEBUG] Cgroup setup successful: {:?}", c);
            c
        }
        Err(e) => {
            log::error!("Failed to create cgroup for process_api: {e}");
            // Continue without cgroup support
            cgroup::CgroupController {
                version: cgroup::CgroupVersion::V2,
                base_path: std::path::PathBuf::from("/sys/fs/cgroup/process_api"),
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

    // Detect container name
    let container_name = detect_container_name().await;

    // Create shared process map
    let proc_map = state::new_process_map();

    // Start control server if configured
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
            "[DEBUG] Control server enabled on {addr} (SIGINT handler disabled)"
        );

        let shutdown_tx_clone = shutdown_tx.clone();
        let shutdown_rx = shutdown_tx.subscribe();
        let proc_map_clone = proc_map.clone();

        tokio::spawn(async move {
            control_server::start_control_server(
                addr,
                shutdown_tx_clone,
                proc_map_clone,
                shutdown_rx,
            )
            .await;
        });
    } else {
        // Set up SIGINT handler when no control server is configured
        log::debug!("[DEBUG] SIGINT handler enabled (control server disabled)");

        let shutdown_tx_clone = shutdown_tx.clone();
        tokio::spawn(async move {
            match tokio::signal::ctrl_c().await {
                Ok(()) => {
                    log::info!("Caught signal SIGINT!");
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

        tokio::spawn(async move {
            oom_killer::container_oom_monitor(
                oom_controller,
                memory_limit,
                polling,
                oom_proc_map,
                oom_shutdown_rx,
                std::collections::HashMap::new(),
            )
            .await;
        });
    }

    // Bind the main WebSocket listener
    /// Decompiled from 0x13f6a0..0x13faff  (1119 bytes)
    /// Xrefs: "main.rsFailed to bind"
    let listener = match TcpListener::bind(&cli.addr).await {
        Ok(l) => {
            log::info!("Listening on: {}", cli.addr);
            l
        }
        Err(e) => {
            log::error!("Failed to bind to {}: {e}", cli.addr);
            return;
        }
    };

    let mut shutdown_rx = shutdown_tx.subscribe();

    // Main WebSocket accept loop
    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, remote_addr)) => {
                        // Security: block local connections if configured
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
                break;
            }
        }
    }

    // Graceful shutdown: kill all tracked processes
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
        state::remove_process(&proc_map, &process_id);
    }

    log::info!("[INFO] process_api shutdown complete");
}
