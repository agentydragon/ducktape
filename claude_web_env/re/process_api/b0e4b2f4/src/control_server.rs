//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! HTTP control server for graceful shutdown and container name updates.
//! Listens on a separate port (e.g., "0.0.0.0:2025") and handles:
//!   POST /shutdown    - initiate graceful shutdown (with filesystem sync)
//!   POST /container_name - update the container name
//!   GET  /health          - return "OK\n"
//!   GET  /container_name   - return current container name
//!
//! Functions decompiled from:
//!   connection_handler:          0x143330..0x14496f  (5695 bytes)
//!   startup_shutdown:            0x1471a0..0x14796d  (1997 bytes)
//!
//! String refs at binary offset 0x1b9546 (loaded 0x2b9546):
//!   "/build/src/control_server.rs"
//!   "[CONTROL] Received shutdown request via HTTP"
//!   "Not Found"
//!   "[CONTROL] Filesystem sync failed with status: "
//!   "[CONTROL] Filesystem sync completed successfully"
//!   "[CONTROL] Failed to execute sync command: "
//!   "[CONTROL] Shutdown signal sent successfully"
//!   "Shutdown initiated"
//!   "[CONTROL] Failed to send shutdown signal: Failed to initiate shutdown"
//!   "[CONTROL] Updated container name to: "
//!   "Container name set to: "
//!   "[CONTROL] Invalid UTF-8 in request body: Invalid UTF-8 in body"
//!   "[CONTROL] Failed to read request body: Failed to read body"
//!
//! Additional string refs at binary offset 0x2b94e1:
//!   "Currently tracked processes: "
//!   "Process limit (soft/hard): "
//!   "Total system processes: "
//!   "Diagnostic info: [OK"

use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;

use http_body_util::Full;
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use parking_lot::Mutex;
use tokio::net::TcpListener;
use tokio::sync::broadcast;

use crate::cgroup::{self, CgroupController};
use crate::proc_handle::{CgroupConfig, ProcController, ProcessInfo};
use crate::state::{self, ProcessMap};

/// Shared container name type, updated by control server, read by WS connections.
pub type SharedContainerName = Arc<Mutex<Option<String>>>;

/// Shared state for the control server.
struct ControlState {
    shutdown_tx: broadcast::Sender<()>,
    container_name: SharedContainerName,
    proc_map: ProcessMap,
    controller: CgroupController,
}

/// Decompiled from 0x1471a0..0x14796d  (1997 bytes)
/// Xrefs: "_build_src_control_server_rs_CON..." (control server startup/shutdown)
///
/// Start the HTTP control server on the given address.
pub async fn start_control_server(
    addr: SocketAddr,
    shutdown_tx: broadcast::Sender<()>,
    container_name: SharedContainerName,
    mut shutdown_rx: broadcast::Receiver<()>,
    proc_map: ProcessMap,
    controller: CgroupController,
) {
    let state = Arc::new(ControlState {
        shutdown_tx,
        container_name,
        proc_map,
        controller,
    });

    let listener = match TcpListener::bind(addr).await {
        Ok(l) => {
            log::info!("[CONTROL] Control server listening on {addr}");
            l
        }
        Err(e) => {
            log::error!("[CONTROL] Failed to bind control server to {addr}: {e}");
            return;
        }
    };

    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, remote_addr)) => {
                        // Security: reject connections from local IPs
                        if crate::is_local_ip(&remote_addr.ip()) {
                            log::warn!(
                                "[CONTROL] [SECURITY] Rejected connection from local IP {remote_addr}"
                            );
                            continue;
                        }
                        let state = Arc::clone(&state);
                        tokio::spawn(async move {
                            let io = TokioIo::new(stream);
                            let service = service_fn(move |req| {
                                let state = Arc::clone(&state);
                                async move { handle_request(req, state).await }
                            });
                            if let Err(e) = http1::Builder::new()
                                .serve_connection(io, service)
                                .await
                            {
                                log::debug!("[CONTROL] Error serving connection from client: {e}");
                            }
                        });
                    }
                    Err(e) => {
                        log::debug!("[CONTROL] Failed to accept connection: {e}");
                    }
                }
            }
            _ = shutdown_rx.recv() => {
                log::debug!("[CONTROL] Control server shutting down");
                log::debug!("[CONTROL] Control server shutdown complete");
                return;
            }
        }
    }
}

/// Decompiled from 0x143330..0x14496f  (5695 bytes)
/// Xrefs: "_build_src_control_server_rs_CON...", "main.rsFailed to bind"
///
/// Handle an individual HTTP request to the control server.
async fn handle_request(
    req: Request<Incoming>,
    state: Arc<ControlState>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let method = req.method().clone();
    let path = req.uri().path().to_string();

    match (method, path.as_str()) {
        (Method::POST, "/shutdown") => {
            log::info!("[CONTROL] Received shutdown request via HTTP");

            // Perform filesystem sync before shutdown
            log::debug!("[CONTROL] Syncing filesystem...");
            match tokio::process::Command::new("sync").output().await {
                Ok(output) => {
                    if output.status.success() {
                        log::info!("[CONTROL] Filesystem sync completed successfully");
                    } else {
                        log::warn!(
                            "[CONTROL] Filesystem sync failed with status: {}",
                            output.status
                        );
                    }
                }
                Err(e) => {
                    log::warn!("[CONTROL] Failed to execute sync command: {e}");
                }
            }

            // Send shutdown signal
            match state.shutdown_tx.send(()) {
                Ok(_) => {
                    log::info!("[CONTROL] Shutdown signal sent successfully");
                    Ok(Response::builder()
                        .status(StatusCode::OK)
                        .body(Full::new(Bytes::from("Shutdown initiated\n")))
                        .unwrap())
                }
                Err(_) => {
                    log::error!(
                        "[CONTROL] Failed to send shutdown signal: Failed to initiate shutdown"
                    );
                    Ok(Response::builder()
                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                        .body(Full::new(Bytes::from(
                            "Failed to initiate shutdown\n",
                        )))
                        .unwrap())
                }
            }
        }

        (Method::POST, "/container_name") => {
            let body = match http_body_util::BodyExt::collect(req.into_body()).await {
                Ok(collected) => collected.to_bytes(),
                Err(e) => {
                    log::warn!("[CONTROL] Failed to read request body: {e}");
                    return Ok(Response::builder()
                        .status(StatusCode::BAD_REQUEST)
                        .body(Full::new(Bytes::from("Failed to read body\n")))
                        .unwrap());
                }
            };

            match std::str::from_utf8(&body) {
                Ok(name) => {
                    let name = name.trim().to_string();
                    log::info!("[CONTROL] Updated container name to: {name}");
                    *state.container_name.lock() = Some(name.clone());
                    Ok(Response::builder()
                        .status(StatusCode::OK)
                        .body(Full::new(Bytes::from(format!(
                            "Container name set to: {name}\n"
                        ))))
                        .unwrap())
                }
                Err(e) => {
                    log::warn!("[CONTROL] Invalid UTF-8 in request body: {e}");
                    Ok(Response::builder()
                        .status(StatusCode::BAD_REQUEST)
                        .body(Full::new(Bytes::from("Invalid UTF-8 in body\n")))
                        .unwrap())
                }
            }
        }

        // Disassembly at 0x102fc3..0x103ad2: GET /health returns 200 "OK\n"
        (Method::GET, "/health") => Ok(Response::builder()
            .status(StatusCode::OK)
            .body(Full::new(Bytes::from("OK\n")))
            .unwrap()),

        // Decompiled from 0x0fef20..0x10032a  (~1034 bytes)
        // Xrefs: "/proc/self/limits", "/proc/sys/kernel/pid_max",
        //   "ps", "aux", "--no-headers", "Max processes",
        //   "Currently tracked processes: ", "Process limit (soft/hard): ",
        //   "Total system processes: ", "System PID max: ",
        //   "Diagnostic info: [OK"
        (Method::GET, "/healthcheck") => {
            let body = build_healthcheck_response(&state.proc_map, &state.controller).await;
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from(body)))
                .unwrap())
        }

        // Disassembly at 0x101bca: GET /container_name returns current name
        (Method::GET, "/container_name") => {
            let name = state.container_name.lock().clone();
            let body = match name {
                Some(n) => format!("{n}\n"),
                None => "not set\n".to_string(),
            };
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from(body)))
                .unwrap())
        }

        _ => Ok(Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(Full::new(Bytes::from("Not Found\n")))
            .unwrap()),
    }
}

/// Decompiled from 0x0fef20..0x10032a  (~1034 bytes)
/// Xrefs: "/proc/self/limits", "/proc/sys/kernel/pid_max",
///   "ps", "aux", "--no-headers", "Max processes",
///   "Currently tracked processes: ", "Process limit (soft/hard): ",
///   "Total system processes: ", "Diagnostic info: [OK",
///   "System PID max: "
///
/// Build the diagnostic response for GET /healthcheck.
/// Reads /proc/self/limits for RLIMIT_NPROC, /proc/sys/kernel/pid_max,
/// runs `ps aux --no-headers` to count total system processes, and
/// collects tracked process info with cgroup state.
async fn build_healthcheck_response(proc_map: &ProcessMap, controller: &CgroupController) -> String {
    // Collect tracked process info, constructing ProcController for each
    let process_controllers: Vec<ProcController> = {
        let map = proc_map.lock();
        map.iter()
            .map(|(process_id, entry)| {
                let cgroup_config = entry.proc_handle.memory_cgroup_path.as_ref().map(|cp| {
                    CgroupConfig {
                        process_id: process_id.clone(),
                        memory_limit_bytes: entry.proc_handle.memory_limit_bytes,
                        memory_usage_bytes: None,
                        memory_cgroup_path: Some(cp.display().to_string()),
                        process_group_pid: entry.proc_handle.process_group_pid,
                        internal_state: format!("{:?}", entry.internal_state),
                    }
                });
                let process_info = ProcessInfo {
                    process_id: process_id.clone(),
                    pid: entry.pid,
                    reattachable: entry.reattachable,
                    timeout: entry.proc_handle.timeout.map(|d| d.as_secs()),
                    memory_limit_bytes: entry.proc_handle.memory_limit_bytes,
                    start_time: entry
                        .proc_handle
                        .start_time
                        .elapsed()
                        .as_secs(),
                };
                ProcController {
                    cgroup: cgroup_config,
                    oom_killed_tx: None,
                    process_info,
                }
            })
            .collect()
    };

    // Read fresh memory usage for each process's cgroup (outside the lock)
    let mut controllers_with_usage = process_controllers;
    for pc in &mut controllers_with_usage {
        if let Some(ref mut cg) = pc.cgroup {
            if let Some(ref cp) = cg.memory_cgroup_path {
                if let Ok(usage) =
                    cgroup::read_memory_usage(&std::path::PathBuf::from(cp), controller.version)
                        .await
                {
                    cg.memory_usage_bytes = Some(usage);
                }
            }
        }
    }

    let tracked = state::debug_process_map(proc_map);

    // Read /proc/self/limits for "Max processes" RLIMIT_NPROC
    let mut soft_limit = String::new();
    let mut hard_limit = String::new();
    if let Ok(limits) = tokio::fs::read_to_string("/proc/self/limits").await {
        for line in limits.lines() {
            if line.starts_with("Max processes") {
                let parts: Vec<&str> = line.split_whitespace().collect();
                if parts.len() >= 4 {
                    soft_limit = parts[2].to_string();
                    hard_limit = parts[3].to_string();
                }
            }
        }
    }

    // Read /proc/sys/kernel/pid_max
    let pid_max = tokio::fs::read_to_string("/proc/sys/kernel/pid_max")
        .await
        .unwrap_or_default()
        .trim()
        .to_string();

    // Count total system processes via `ps aux --no-headers`
    let total_procs = match tokio::process::Command::new("ps")
        .args(["aux", "--no-headers"])
        .output()
        .await
    {
        Ok(output) => String::from_utf8_lossy(&output.stdout)
            .lines()
            .count()
            .to_string(),
        Err(_) => "unknown".to_string(),
    };

    // Serialize ProcControllers to ensure serde field names are retained
    let _serialized: Vec<String> = controllers_with_usage
        .iter()
        .filter_map(|pc| serde_json::to_string(pc).ok())
        .collect();

    format!(
        "{tracked}\nProcess limit (soft/hard): {soft_limit}/{hard_limit}\nTotal system processes: {total_procs}\nSystem PID max: {pid_max}\nDiagnostic info: [OK\n"
    )
}

/// Get the current container name from shared state.
/// Xrefs: "[DEBUG] Using container name from control server:"
pub fn get_container_name(state: &Mutex<Option<String>>) -> Option<String> {
    state.lock().clone()
}
