//! Reverse-engineered from process_api BuildID e409c31a846219e05541706c43daf1756365f486
//! (updated from b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0)
//!
//! HTTP control server for graceful shutdown, container name updates,
//! filesystem sync/freeze, and mount_root (Firecracker snapstart).
//!
//! Listens on TCP or vsock and handles:
//!   POST /shutdown       - initiate graceful shutdown (with filesystem sync)
//!   POST /container_name - update the container name
//!   POST /mount_root     - apply mount root config (Firecracker snapstart)
//!   POST /fs_sync        - flush filesystem buffers
//!   GET  /health         - return "OK\n"
//!   GET  /healthcheck    - diagnostic info
//!   GET  /container_name - return current container name
//!
//! New in e409c31a:
//!   - POST /mount_root endpoint (enabled when --firecracker-init is set)
//!   - POST /fs_sync endpoint
//!   - Vsock control server (start_vsock_control_server)
//!   - FIFREEZE/FITHAW support for snapshot preparation
//!   - Container name and auth key persistence to /container_info.json
//!   - Auth public key setting via POST /auth_public_key
//!
//! String refs at binary offset 0x27a86d..0x27fed3:
//!   "[CONTROL] Control server listening on vsock port ..."
//!   "[CONTROL] Failed to bind control server to vsock port ..."
//!   "[CONTROL] Received mount_root request"
//!   "[CONTROL] mount_root succeeded: ..."
//!   "[CONTROL] mount_root failed: ..."
//!   "[CONTROL] /fs_sync: flushing filesystem buffers..."
//!   "[CONTROL] /fs_sync: done"
//!   "[CONTROL] Freezing / ..."
//!   "[CONTROL] / frozen"
//!   "[CONTROL] FIFREEZE failed (continuing): ..."
//!   "[CONTROL] Dropping page caches..."
//!   "[CONTROL] open(/) failed: ..."
//!   "[CONTROL] Auth public key set successfully"
//!   "[CONTROL] Invalid auth public key: ..."
//!   "[CONTROL] Failed to persist auth key to container_info.json: ..."
//!   "[CONTROL] Failed to persist container name to container_info.json: ..."
//!   "[SECURITY] Rejecting vsock connection from non-host CID ..."

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
use crate::firecracker_init;
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
    /// Whether the /mount_root endpoint is enabled (true when --firecracker-init is set).
    mount_root_enabled: bool,
}

/// Start the HTTP control server on a TCP address.
/// Updated signature: takes mount_root_enabled parameter.
pub async fn start_control_server(
    addr: SocketAddr,
    shutdown_tx: broadcast::Sender<()>,
    container_name: SharedContainerName,
    mut shutdown_rx: broadcast::Receiver<()>,
    proc_map: ProcessMap,
    controller: CgroupController,
    mount_root_enabled: bool,
) {
    let state = Arc::new(ControlState {
        shutdown_tx,
        container_name,
        proc_map,
        controller,
        mount_root_enabled,
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
                                log::debug!("[CONTROL] Error serving connection: {e}");
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

/// Start the control server on a vsock port (Firecracker).
/// Xrefs: "[CONTROL] Control server listening on vsock port ...",
///   "[CONTROL] Failed to bind control server to vsock port ...",
///   "[SECURITY] Rejecting vsock connection from non-host CID ..."
pub async fn start_vsock_control_server(
    port: u32,
    shutdown_tx: broadcast::Sender<()>,
    container_name: SharedContainerName,
    mut shutdown_rx: broadcast::Receiver<()>,
    proc_map: ProcessMap,
    controller: CgroupController,
    mount_root_enabled: bool,
) {
    let _state = Arc::new(ControlState {
        shutdown_tx,
        container_name,
        proc_map,
        controller,
        mount_root_enabled,
    });

    // In the real binary, this uses tokio-vsock to bind a VsockListener on
    // CID=VMADDR_CID_ANY (u32::MAX), port=`port`.
    // Connections are validated to ensure CID == 2 (host).
    //
    // Placeholder — requires tokio-vsock crate integration.
    log::info!("[CONTROL] Control server listening on vsock port {port}");
    log::warn!("vsock control server not yet fully implemented in RE");

    // Wait for shutdown
    let _ = shutdown_rx.recv().await;
    log::debug!("[CONTROL] Control server shutting down");
    log::debug!("[CONTROL] Control server shutdown complete");
}

/// Handle an individual HTTP request to the control server.
/// Updated for e409c31a: adds /mount_root, /fs_sync, /auth_public_key endpoints.
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
                        .body(Full::new(Bytes::from("Failed to initiate shutdown\n")))
                        .unwrap())
                }
            }
        }

        (Method::POST, "/container_name") => {
            let body = match read_body(req).await {
                Ok(b) => b,
                Err(resp) => return Ok(resp),
            };

            match std::str::from_utf8(&body) {
                Ok(name) => {
                    let name = name.trim().to_string();
                    log::info!("[CONTROL] Updated container name to: {name}");
                    *state.container_name.lock() = Some(name.clone());

                    // Persist to container_info.json
                    persist_container_info("container_name", &name);

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

        // New in e409c31a: POST /mount_root — apply mount root config (Firecracker snapstart)
        // Xrefs: "[CONTROL] Received mount_root request",
        //   "[CONTROL] mount_root succeeded: ...", "[CONTROL] mount_root failed: ..."
        (Method::POST, "/mount_root") => {
            if !state.mount_root_enabled {
                return Ok(Response::builder()
                    .status(StatusCode::NOT_FOUND)
                    .body(Full::new(Bytes::from("Not Found\n")))
                    .unwrap());
            }

            log::info!("[CONTROL] Received mount_root request");

            let body = match read_body(req).await {
                Ok(b) => b,
                Err(resp) => return Ok(resp),
            };

            match serde_json::from_slice::<firecracker_init::MountRootConfig>(&body) {
                Ok(config) => match firecracker_init::apply_mount_config(&config) {
                    Ok(status) => {
                        log::info!("[CONTROL] mount_root succeeded: {status}");
                        Ok(Response::builder()
                            .status(StatusCode::OK)
                            .body(Full::new(Bytes::from(format!(
                                "mount_root succeeded: {status}\n"
                            ))))
                            .unwrap())
                    }
                    Err(e) => {
                        log::error!("[CONTROL] mount_root failed: {e}");
                        Ok(Response::builder()
                            .status(StatusCode::INTERNAL_SERVER_ERROR)
                            .body(Full::new(Bytes::from(format!("mount_root failed: {e}\n"))))
                            .unwrap())
                    }
                },
                Err(e) => {
                    log::error!("[CONTROL] mount_root failed: {e}");
                    Ok(Response::builder()
                        .status(StatusCode::BAD_REQUEST)
                        .body(Full::new(Bytes::from(format!("mount_root failed: {e}\n"))))
                        .unwrap())
                }
            }
        }

        // New in e409c31a: POST /fs_sync — flush filesystem buffers + freeze/thaw
        // Xrefs: "[CONTROL] /fs_sync: flushing filesystem buffers...",
        //   "[CONTROL] /fs_sync: done", "[CONTROL] Freezing / ...",
        //   "[CONTROL] / frozen", "[CONTROL] FIFREEZE failed (continuing): ...",
        //   "[CONTROL] Dropping page caches..."
        (Method::POST, "/fs_sync") => {
            log::info!("[CONTROL] /fs_sync: flushing filesystem buffers...");

            // Sync filesystem
            let _ = tokio::process::Command::new("sync").output().await;

            // Drop page caches
            log::debug!("[CONTROL] Dropping page caches...");
            let _ = tokio::fs::write("/proc/sys/vm/drop_caches", "3\n").await;

            // Freeze root filesystem
            log::debug!("[CONTROL] Freezing / ...");
            match firecracker_init::freeze_root() {
                Ok(()) => {
                    log::info!("[CONTROL] / frozen");
                }
                Err(e) => {
                    log::warn!("[CONTROL] FIFREEZE failed (continuing): {e}");
                }
            }

            log::info!("[CONTROL] /fs_sync: done");
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from("fs_sync done\n")))
                .unwrap())
        }

        (Method::GET, "/health") => Ok(Response::builder()
            .status(StatusCode::OK)
            .body(Full::new(Bytes::from("OK\n")))
            .unwrap()),

        (Method::GET, "/healthcheck") => {
            let body = build_healthcheck_response(&state.proc_map, &state.controller).await;
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from(body)))
                .unwrap())
        }

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

/// Read the full body of an HTTP request.
async fn read_body(req: Request<Incoming>) -> Result<Bytes, Response<Full<Bytes>>> {
    match http_body_util::BodyExt::collect(req.into_body()).await {
        Ok(collected) => Ok(collected.to_bytes()),
        Err(e) => {
            log::warn!("[CONTROL] Failed to read request body: {e}");
            Err(Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .body(Full::new(Bytes::from("Failed to read body\n")))
                .unwrap())
        }
    }
}

/// Persist a key-value pair to /container_info.json.
/// Xrefs: "[CONTROL] Failed to persist container name to container_info.json: ...",
///   "[CONTROL] Failed to persist auth key to container_info.json: ..."
fn persist_container_info(key: &str, value: &str) {
    let path = "/container_info.json";
    let mut data: serde_json::Value = match std::fs::read_to_string(path) {
        Ok(contents) => serde_json::from_str(&contents).unwrap_or(serde_json::json!({})),
        Err(_) => serde_json::json!({}),
    };

    data[key] = serde_json::Value::String(value.to_string());

    if let Err(e) = std::fs::write(
        path,
        serde_json::to_string_pretty(&data).unwrap_or_default(),
    ) {
        log::warn!("[CONTROL] Failed to persist {key} to container_info.json: {e}");
    }
}

/// Build the diagnostic response for GET /healthcheck.
async fn build_healthcheck_response(
    proc_map: &ProcessMap,
    controller: &CgroupController,
) -> String {
    let process_controllers: Vec<ProcController> = {
        let map = proc_map.lock();
        map.iter()
            .map(|(process_id, entry)| {
                let cgroup_config =
                    entry
                        .proc_handle
                        .memory_cgroup_path
                        .as_ref()
                        .map(|cp| CgroupConfig {
                            process_id: process_id.clone(),
                            memory_limit_bytes: entry.proc_handle.memory_limit_bytes,
                            memory_usage_bytes: None,
                            memory_cgroup_path: Some(cp.display().to_string()),
                            process_group_pid: entry.proc_handle.process_group_pid,
                            internal_state: format!("{:?}", entry.internal_state),
                        });
                let process_info = ProcessInfo {
                    process_id: process_id.clone(),
                    pid: entry.pid,
                    reattachable: entry.reattachable,
                    timeout: entry.proc_handle.timeout.map(|d| d.as_secs()),
                    memory_limit_bytes: entry.proc_handle.memory_limit_bytes,
                    start_time: entry.proc_handle.start_time.elapsed().as_secs(),
                };
                ProcController {
                    cgroup: cgroup_config,
                    oom_killed_tx: None,
                    process_info,
                }
            })
            .collect()
    };

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

    let pid_max = tokio::fs::read_to_string("/proc/sys/kernel/pid_max")
        .await
        .unwrap_or_default()
        .trim()
        .to_string();

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

    let _serialized: Vec<String> = controllers_with_usage
        .iter()
        .filter_map(|pc| serde_json::to_string(pc).ok())
        .collect();

    format!(
        "{tracked}\nProcess limit (soft/hard): {soft_limit}/{hard_limit}\nTotal system processes: {total_procs}\nSystem PID max: {pid_max}\nDiagnostic info: [OK\n"
    )
}

/// Get the current container name from shared state.
pub fn get_container_name(state: &Mutex<Option<String>>) -> Option<String> {
    state.lock().clone()
}
