//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! HTTP control server for graceful shutdown and container name updates.
//! Listens on a separate port (e.g., "0.0.0.0:2025") and handles:
//!   POST /shutdown    - initiate graceful shutdown (with filesystem sync)
//!   POST /container_name - update the container name
//!   GET  /healthcheck - return diagnostic info
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

use crate::state::ProcessMap;

/// Shared container name type, updated by control server, read by WS connections.
pub type SharedContainerName = Arc<Mutex<Option<String>>>;

/// Shared state for the control server.
struct ControlState {
    shutdown_tx: broadcast::Sender<()>,
    container_name: SharedContainerName,
    proc_map: ProcessMap,
}

/// Decompiled from 0x1471a0..0x14796d  (1997 bytes)
/// Xrefs: "_build_src_control_server_rs_CON..." (control server startup/shutdown)
///
/// Start the HTTP control server on the given address.
pub async fn start_control_server(
    addr: SocketAddr,
    shutdown_tx: broadcast::Sender<()>,
    proc_map: ProcessMap,
    container_name: SharedContainerName,
    mut shutdown_rx: broadcast::Receiver<()>,
) {
    let state = Arc::new(ControlState {
        shutdown_tx,
        container_name,
        proc_map,
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
                    Ok((stream, _remote_addr)) => {
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

        (Method::GET, "/healthcheck") => {
            let diag = crate::state::debug_process_map(&state.proc_map);
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from(format!(
                    "Diagnostic info: [OK]\n{diag}\n"
                ))))
                .unwrap())
        }

        _ => Ok(Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(Full::new(Bytes::from("Not Found\n")))
            .unwrap()),
    }
}

/// Get the current container name from shared state.
/// Xrefs: "[DEBUG] Using container name from control server:"
pub fn get_container_name(state: &Mutex<Option<String>>) -> Option<String> {
    state.lock().clone()
}
