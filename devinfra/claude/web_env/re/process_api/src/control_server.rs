//! Reverse-engineered from process_api BuildID edebff2c28de76238c95c299ba3401a9098c9e17
//! release process_api_2026-05-11-18-55
//!
//! HTTP control server for graceful shutdown, container name updates,
//! filesystem freeze/thaw, and mount_root (Firecracker).
//!
//! Listens on TCP or vsock and handles:
//!   POST /shutdown                        - sync, drop caches, send shutdown signal
//!   POST /container_name                  - update the container name
//!   POST /mount_root                      - apply mount root config (Firecracker)
//!   POST /fs_freeze                       - FIFREEZE root filesystem (Binary: 810fd3a4)
//!   POST /fs_thaw                         - FITHAW root filesystem (Binary: 810fd3a4)
//!   POST /auth_public_key/write_etc_files - set Ed25519 auth key, write /etc/hosts + /etc/resolv.conf
//!   POST /sync_clock                      - clock sync (stub: "not available on Linux")
//!   GET  /health                          - diagnostic healthcheck (process state)
//!   GET  /container_name                  - return current container name
//!
//! Endpoint path verification (objdump):
//!   /fs_freeze  - 10-byte comparison: movabs 0x656572665f73662f ("/fs_free"), then
//!                 xor 0x657a ("ze") at offset 8. NOT "/fs_free".
//!   /fs_thaw    - 8-byte comparison: movabs 0x776168745f73662f ("/fs_thaw")
//!   /shutdown   - 9-byte comparison: 8-byte movabs + 1-byte xor 0x6e ("n")
//!   /sync_clock - 11-byte comparison: overlapping 8-byte movabs pairs
//!   /health     - 7-byte comparison: overlapping 4-byte pairs
//!   /container_name - 15-byte comparison: overlapping 8-byte movabs pairs
//!   /mount_root - 11-byte comparison: overlapping 8-byte movabs pairs
//!
//! String refs (Binary: 810fd3a4):
//!   "[CONTROL] Control server listening on ..."
//!   "[CONTROL] Control server listening on vsock port ..."
//!   "[CONTROL] Failed to bind control server to ..."
//!   "[CONTROL] Failed to bind control server to vsock port ..."
//!   "[CONTROL] [SECURITY] Rejected connection from ..."
//!   "[CONTROL] [SECURITY] Rejected connection from non-host CID ..."
//!   "[CONTROL] Error serving connection: ..."
//!   "[CONTROL] Failed to accept connection: ..."
//!   "[CONTROL] Failed to read request body: ..."
//!   "[CONTROL] Received shutdown request via HTTP"
//!   "[CONTROL] Dropping page caches..."
//!   "[CONTROL] Shutdown signal sent successfully"
//!   "[CONTROL] Failed to send shutdown signal: ..."
//!   "[CONTROL] Updated container name to: ..."
//!   "[CONTROL] Invalid UTF-8 in request body: ..."
//!   "[CONTROL] Failed to persist container name to container_info.json: ..."
//!   "[CONTROL] Received mount_root request"
//!   "[CONTROL] mount_root succeeded: ..."
//!   "[CONTROL] mount_root failed: ..."
//!   "[CONTROL] mount_root task panicked: ..."
//!   "[CONTROL] /fs_freeze: freezing / ..."
//!   "[CONTROL] /fs_freeze: done (frozen ... host must call /fs_thaw after resume)"
//!   "[CONTROL] /fs_freeze: FIFREEZE failed, returning 500"
//!   "[CONTROL] Freezing / ..."
//!   "[CONTROL] / frozen"
//!   "[CONTROL] / already frozen (EBUSY)"
//!   "[CONTROL] FIFREEZE failed: ..."
//!   "[CONTROL] open(/) failed: ..."
//!   "[CONTROL] /fs_thaw: thawing / ..."
//!   "[CONTROL] /fs_thaw: done"
//!   "[CONTROL] /fs_thaw: failed, returning 500"
//!   "[CONTROL] / thawed"
//!   "[CONTROL] / was not frozen (EINVAL), nothing to thaw"
//!   "[CONTROL] FITHAW failed: ..."
//!   "[CONTROL] open(/) for thaw failed: ..."
//!   "[CONTROL] Auth public key set successfully"
//!   "[CONTROL] Invalid auth public key: ..."
//!   "[CONTROL] /write_etc_files: hosts N bytes, resolv N bytes"
//!   "[CONTROL] /write_etc_files: write failed: ..."
//!   "[CONTROL] Failed to persist auth key to container_info.json: ..."
//!   "[CONTROL] FUSE mount wait failed (non-fatal): ..."
//!   "[CONTROL] Control server shutting down"
//!   "[CONTROL] Control server shutdown complete"

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
use tokio_vsock::{VMADDR_CID_ANY, VsockAddr, VsockListener};

use crate::cgroup::{self, CgroupController};
use crate::firecracker_init;
use crate::proc_handle::{Cgroup, ProcController, ProcessInfo};
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
                                "[CONTROL] [SECURITY] Rejected connection from {remote_addr}"
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
/// Binary: 810fd3a4 — real tokio-vsock 0.7.2 implementation.
/// Xrefs: "[CONTROL] Control server listening on vsock port ...",
///   "[CONTROL] Failed to bind control server to vsock port ...",
///   "[CONTROL] [SECURITY] Rejected connection from non-host CID ..."
pub async fn start_vsock_control_server(
    port: u32,
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

    let listener = match VsockListener::bind(VsockAddr::new(VMADDR_CID_ANY, port)) {
        Ok(l) => {
            log::info!("[CONTROL] Control server listening on vsock port {port}");
            l
        }
        Err(e) => {
            log::error!("[CONTROL] Failed to bind control server to vsock port {port}: {e}");
            return;
        }
    };

    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, peer_addr)) => {
                        let peer_cid = peer_addr.cid();
                        if peer_cid != 2 {
                            log::warn!(
                                "[CONTROL] [SECURITY] Rejected connection from non-host CID {peer_cid}"
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

/// Handle an individual HTTP request to the control server.
/// Binary: 810fd3a4 — endpoints: /shutdown, /container_name (GET+POST), /mount_root,
///   /fs_freeze, /fs_thaw, /auth_public_key/write_etc_files, /sync_clock, /health.
async fn handle_request(
    req: Request<Incoming>,
    state: Arc<ControlState>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let method = req.method().clone();
    let path = req.uri().path().to_string();

    match (method, path.as_str()) {
        // POST /shutdown — 9-byte path (verified by objdump at 0x19c327).
        // Sequence: log, sync filesystem, drop page caches, send shutdown signal.
        // Xrefs: "[CONTROL] Received shutdown request via HTTP",
        //   "[CONTROL] Dropping page caches...",
        //   "[CONTROL] Shutdown signal sent successfully",
        //   "[CONTROL] Failed to send shutdown signal: ..."
        (Method::POST, "/shutdown") => {
            log::info!("[CONTROL] Received shutdown request via HTTP");

            // Sync filesystem
            let _ = firecracker_init::sync_filesystem().await;

            // Drop page caches (write "3" to /proc/sys/vm/drop_caches)
            log::info!("[CONTROL] Dropping page caches...");
            let _ = tokio::fs::write("/proc/sys/vm/drop_caches", "3\n").await;

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

                    // Persist container name to /container_info.json
                    if let Err(e) = persist_container_info("container_name", &name) {
                        log::warn!(
                            "[CONTROL] Failed to persist container name to container_info.json: {e}"
                        );
                    }

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

        // POST /mount_root — 11-byte path (verified by objdump at 0x19c579).
        // Apply mount root config (Firecracker snapstart). Spawns as a task; panics
        // are caught and reported.
        // Xrefs: "[CONTROL] Received mount_root request",
        //   "[CONTROL] mount_root succeeded: ...", "[CONTROL] mount_root failed: ...",
        //   "[CONTROL] mount_root task panicked: ...",
        //   "[CONTROL] FUSE mount wait failed (non-fatal): ..."
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
                Ok(config) => {
                    // mount_root is spawned as a task; panics are caught.
                    let result =
                        tokio::task::spawn(
                            async move { firecracker_init::apply_mount_config(&config) },
                        )
                        .await;

                    match result {
                        Ok(Ok(status)) => {
                            log::info!("[CONTROL] mount_root succeeded: {status}");
                            Ok(Response::builder()
                                .status(StatusCode::OK)
                                .body(Full::new(Bytes::from(format!(
                                    "mount_root succeeded: {status}\n"
                                ))))
                                .unwrap())
                        }
                        Ok(Err(e)) => {
                            log::error!("[CONTROL] mount_root failed: {e}");
                            Ok(Response::builder()
                                .status(StatusCode::INTERNAL_SERVER_ERROR)
                                .body(Full::new(Bytes::from(format!("mount_root failed: {e}\n"))))
                                .unwrap())
                        }
                        Err(e) => {
                            log::error!("[CONTROL] mount_root task panicked: {e}");
                            Ok(Response::builder()
                                .status(StatusCode::INTERNAL_SERVER_ERROR)
                                .body(Full::new(Bytes::from(format!(
                                    "mount_root task panicked: {e}\n"
                                ))))
                                .unwrap())
                        }
                    }
                }
                Err(e) => {
                    log::error!("[CONTROL] mount_root failed: {e}");
                    Ok(Response::builder()
                        .status(StatusCode::BAD_REQUEST)
                        .body(Full::new(Bytes::from(format!("mount_root failed: {e}\n"))))
                        .unwrap())
                }
            }
        }

        // Binary: 810fd3a4 — POST /fs_freeze (10-byte path, verified by objdump).
        // Path comparison at 0x19c1de: movabs "/fs_free" + xor "ze" at offset 8.
        // Xrefs: "[CONTROL] /fs_freeze: freezing / ...",
        //   "[CONTROL] /fs_freeze: done (frozen ... host must call /fs_thaw after resume)",
        //   "[CONTROL] /fs_freeze: FIFREEZE failed, returning 500",
        //   "[CONTROL] Freezing / ...", "[CONTROL] / frozen",
        //   "[CONTROL] / already frozen (EBUSY)", "[CONTROL] FIFREEZE failed: ...",
        //   "[CONTROL] open(/) failed: ..."
        (Method::POST, "/fs_freeze") => {
            log::info!("[CONTROL] /fs_freeze: freezing / ...");

            // Open root filesystem for FIFREEZE ioctl
            let root_fd = match std::fs::File::open("/") {
                Ok(f) => f,
                Err(e) => {
                    log::error!("[CONTROL] open(/) failed: {e}");
                    return Ok(Response::builder()
                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                        .body(Full::new(Bytes::from("freeze failed\n")))
                        .unwrap());
                }
            };

            // FIFREEZE ioctl
            log::debug!("[CONTROL] Freezing / ...");
            match firecracker_init::fifreeze_fd(&root_fd) {
                Ok(()) => {
                    log::info!("[CONTROL] / frozen");
                }
                Err(e) if e.raw_os_error() == Some(libc::EBUSY) => {
                    log::info!("[CONTROL] / already frozen (EBUSY)");
                }
                Err(e) => {
                    log::error!("[CONTROL] FIFREEZE failed: {e}");
                    log::error!("[CONTROL] /fs_freeze: FIFREEZE failed, returning 500");
                    return Ok(Response::builder()
                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                        .body(Full::new(Bytes::from("freeze failed\n")))
                        .unwrap());
                }
            }

            log::info!(
                "[CONTROL] /fs_freeze: done (frozen \u{2014} host must call /fs_thaw after resume)"
            );
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from("frozen\n")))
                .unwrap())
        }

        // Binary: 810fd3a4 — POST /fs_thaw (8-byte path, verified by objdump).
        // Path comparison at 0x19b855: movabs "/fs_thaw", cmp.
        // Xrefs: "[CONTROL] /fs_thaw: thawing / ...",
        //   "[CONTROL] /fs_thaw: done", "[CONTROL] /fs_thaw: failed, returning 500",
        //   "[CONTROL] / thawed", "[CONTROL] / was not frozen (EINVAL), nothing to thaw",
        //   "[CONTROL] FITHAW failed: ...", "[CONTROL] open(/) for thaw failed: ..."
        (Method::POST, "/fs_thaw") => {
            log::info!("[CONTROL] /fs_thaw: thawing / ...");

            // Open root filesystem for FITHAW ioctl
            let root_fd = match std::fs::File::open("/") {
                Ok(f) => f,
                Err(e) => {
                    log::error!("[CONTROL] open(/) for thaw failed: {e}");
                    log::error!("[CONTROL] /fs_thaw: failed, returning 500");
                    return Ok(Response::builder()
                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                        .body(Full::new(Bytes::from("thaw failed\n")))
                        .unwrap());
                }
            };

            // FITHAW ioctl
            match firecracker_init::fithaw_fd(&root_fd) {
                Ok(()) => {
                    log::info!("[CONTROL] / thawed");
                }
                Err(e) if e.raw_os_error() == Some(libc::EINVAL) => {
                    log::info!("[CONTROL] / was not frozen (EINVAL), nothing to thaw");
                }
                Err(e) => {
                    log::error!("[CONTROL] FITHAW failed: {e}");
                    log::error!("[CONTROL] /fs_thaw: failed, returning 500");
                    return Ok(Response::builder()
                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                        .body(Full::new(Bytes::from("thaw failed\n")))
                        .unwrap());
                }
            }

            log::info!("[CONTROL] /fs_thaw: done");
            Ok(Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from("thawed\n")))
                .unwrap())
        }

        // Binary: 810fd3a4 — POST /auth_public_key/write_etc_files
        // Sets auth public key AND writes /etc/hosts + /etc/resolv.conf from request body.
        // Fields confirmed by string evidence: process_id_reuse, allow_process_id,
        // memory_limit_bytes, plus etc_hosts and resolv_conf from MountRootConfig fields.
        // Xrefs: "[CONTROL] Auth public key set successfully",
        //   "[CONTROL] Invalid auth public key: ...",
        //   "[CONTROL] /write_etc_files: hosts N bytes, resolv N bytes",
        //   "[CONTROL] /write_etc_files: write failed: ...",
        //   "[CONTROL] Failed to persist auth key to container_info.json: ..."
        (Method::POST, "/auth_public_key/write_etc_files") => {
            let body = match read_body(req).await {
                Ok(b) => b,
                Err(resp) => return Ok(resp),
            };

            #[derive(serde::Deserialize)]
            struct AuthWriteEtcFilesRequest {
                /// Raw Ed25519 public key, base64-encoded (32 bytes).
                pub_key: Option<String>,
                /// Allow reuse of process IDs.
                #[serde(default)]
                process_id_reuse: Option<bool>,
                /// Specific process ID to allow.
                #[serde(default)]
                allow_process_id: Option<String>,
                /// Per-connection memory limit in bytes.
                #[serde(default)]
                memory_limit_bytes: Option<u64>,
                /// Content for /etc/hosts.
                #[serde(default)]
                etc_hosts: Option<String>,
                /// Content for /etc/resolv.conf.
                #[serde(default)]
                resolv_conf: Option<String>,
                /// Binary: edebff2c — PEM of the egress-inspection CA, fanned
                /// out by `firecracker_init::append_ca_cert`.
                /// Evidence: "struct EtcFiles with 3 elements" (0x39a00c, was
                /// "with 2 elements" in 810fd3a4) and the field-name run
                /// "process" "hosts" "ca_cert" at 0x399f8a.
                #[serde(default)]
                ca_cert: Option<String>,
            }

            match serde_json::from_slice::<AuthWriteEtcFilesRequest>(&body) {
                Ok(req_body) => {
                    // Set auth public key if present
                    if let Some(ref key_b64) = req_body.pub_key {
                        match base64_decode_key(key_b64) {
                            Ok(key_bytes) if key_bytes.len() == 32 => {
                                log::info!("[CONTROL] Auth public key set successfully");
                                if let Err(e) = persist_container_info("auth_public_key", key_b64) {
                                    log::warn!(
                                        "[CONTROL] Failed to persist auth key to container_info.json: {e}"
                                    );
                                }
                            }
                            Ok(key_bytes) => {
                                let msg = format!(
                                    "Auth public key must be exactly 32 bytes (raw Ed25519), got {}",
                                    key_bytes.len()
                                );
                                log::warn!("[CONTROL] Invalid auth public key: {msg}");
                                return Ok(Response::builder()
                                    .status(StatusCode::BAD_REQUEST)
                                    .body(Full::new(Bytes::from(format!("{msg}\n"))))
                                    .unwrap());
                            }
                            Err(e) => {
                                log::warn!("[CONTROL] Invalid auth public key: {e}");
                                return Ok(Response::builder()
                                    .status(StatusCode::BAD_REQUEST)
                                    .body(Full::new(Bytes::from(format!(
                                        "Invalid auth public key: {e}\n"
                                    ))))
                                    .unwrap());
                            }
                        }
                    }

                    // Write /etc/hosts and /etc/resolv.conf
                    let hosts_len = req_body.etc_hosts.as_ref().map_or(0, |s| s.len());
                    let resolv_len = req_body.resolv_conf.as_ref().map_or(0, |s| s.len());

                    if let Some(ref hosts) = req_body.etc_hosts {
                        if let Err(e) = std::fs::write("/etc/hosts", hosts) {
                            log::error!("[CONTROL] /write_etc_files: write failed: {e}");
                            return Ok(Response::builder()
                                .status(StatusCode::INTERNAL_SERVER_ERROR)
                                .body(Full::new(Bytes::from(format!(
                                    "/write_etc_files: write failed: {e}\n"
                                ))))
                                .unwrap());
                        }
                    }
                    if let Some(ref resolv) = req_body.resolv_conf {
                        if let Err(e) = std::fs::write("/etc/resolv.conf", resolv) {
                            log::error!("[CONTROL] /write_etc_files: write failed: {e}");
                            return Ok(Response::builder()
                                .status(StatusCode::INTERNAL_SERVER_ERROR)
                                .body(Full::new(Bytes::from(format!(
                                    "/write_etc_files: write failed: {e}\n"
                                ))))
                                .unwrap());
                        }
                    }

                    // Binary: edebff2c — the CA PEM is fanned out through the
                    // same helper the Firecracker init path uses. On failure
                    // the request is rejected (template 0x38b117):
                    //   "[CONTROL] /write_etc_files: append_ca_cert failed: {}"
                    // and the response body is "append_ca_cert: {}\n"
                    // (template 0x38b150).
                    let mut ca_cert_desc = String::from("none");
                    if let Some(ref pem) = req_body.ca_cert {
                        match firecracker_init::append_ca_cert(pem) {
                            Ok(()) => ca_cert_desc = format!("{} bytes", pem.len()),
                            Err(e) => {
                                log::error!(
                                    "[CONTROL] /write_etc_files: append_ca_cert failed: {e}"
                                );
                                return Ok(Response::builder()
                                    .status(StatusCode::INTERNAL_SERVER_ERROR)
                                    .body(Full::new(Bytes::from(format!("append_ca_cert: {e}\n"))))
                                    .unwrap());
                            }
                        }
                    }

                    // Binary: edebff2c template 0x38b164 — the old build logged
                    // only "hosts {} bytes, resolv {} bytes".
                    log::info!(
                        "[CONTROL] /write_etc_files: hosts {hosts_len} bytes, resolv {resolv_len} bytes, ca_cert {ca_cert_desc}"
                    );

                    Ok(Response::builder()
                        .status(StatusCode::OK)
                        .body(Full::new(Bytes::from("Auth public key set\n")))
                        .unwrap())
                }
                Err(e) => {
                    log::warn!("[CONTROL] Invalid auth public key: {e}");
                    Ok(Response::builder()
                        .status(StatusCode::BAD_REQUEST)
                        .body(Full::new(Bytes::from(format!("Invalid request: {e}\n"))))
                        .unwrap())
                }
            }
        }

        // GET /health — returns diagnostic healthcheck (process state).
        // Binary has no "/healthcheck" string; only "/health" (7 bytes, verified by objdump
        // at 0x19bc0b: overlapping 4-byte comparisons "/hea" + "alth").
        // Calls complex response builder (0xd2190) with process state serialization.
        (Method::GET, "/health") => {
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

        // POST /sync_clock — 11-byte path (verified by objdump at 0x19c59d).
        // Binary: 810fd3a4 — now implemented (was stub in 91c789ff).
        // Parses realtime_unix_nanos from request body, calls clock_settime.
        // Xrefs: "[CONTROL] Clock synced (unix_nanos=",
        //   "[CONTROL] clock_settime failed: "
        (Method::POST, "/sync_clock") => {
            let body = match read_body(req).await {
                Ok(b) => b,
                Err(resp) => return Ok(resp),
            };

            // Parse unix nanos from body (JSON with realtime_unix_nanos field,
            // or plain integer)
            let unix_nanos: u64 = match serde_json::from_slice::<serde_json::Value>(&body) {
                Ok(val) => val
                    .get("realtime_unix_nanos")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0),
                Err(_) => {
                    // Try parsing as plain integer
                    std::str::from_utf8(&body)
                        .ok()
                        .and_then(|s| s.trim().parse().ok())
                        .unwrap_or(0)
                }
            };

            if unix_nanos == 0 {
                return Ok(Response::builder()
                    .status(StatusCode::BAD_REQUEST)
                    .body(Full::new(Bytes::from("Missing realtime_unix_nanos\n")))
                    .unwrap());
            }

            let secs = (unix_nanos / 1_000_000_000) as i64;
            let nsecs = (unix_nanos % 1_000_000_000) as i64;
            let ts = libc::timespec {
                tv_sec: secs,
                tv_nsec: nsecs,
            };

            let ret = unsafe { libc::clock_settime(libc::CLOCK_REALTIME, &ts) };
            if ret == 0 {
                log::info!("[CONTROL] Clock synced (unix_nanos={unix_nanos})");
                Ok(Response::builder()
                    .status(StatusCode::OK)
                    .body(Full::new(Bytes::from(format!(
                        "Clock synced (unix_nanos={unix_nanos})\n"
                    ))))
                    .unwrap())
            } else {
                let errno = std::io::Error::last_os_error();
                log::error!("[CONTROL] clock_settime failed: {errno}");
                Ok(Response::builder()
                    .status(StatusCode::INTERNAL_SERVER_ERROR)
                    .body(Full::new(Bytes::from(format!(
                        "clock_settime failed: {errno}\n"
                    ))))
                    .unwrap())
            }
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

/// Decode a base64-encoded key (standard or URL-safe, with or without padding).
/// Binary: 810fd3a4 — used for Ed25519 public key validation in /auth_public_key/write_etc_files.
fn base64_decode_key(s: &str) -> Result<Vec<u8>, String> {
    // Try standard base64 first, then URL-safe
    let result = {
        let s_padded = if s.len() % 4 != 0 {
            let pad = 4 - (s.len() % 4);
            format!("{s}{}", "=".repeat(pad))
        } else {
            s.to_string()
        };
        // Use simple character substitution for URL-safe variant
        let standard = s_padded.replace('-', "+").replace('_', "/");
        match openssl_decode(&standard) {
            Ok(b) => Ok(b),
            Err(_) => openssl_decode(&s_padded),
        }
    };
    result
}

/// Minimal base64 decode using only std.
fn openssl_decode(s: &str) -> Result<Vec<u8>, String> {
    // Validate alphabet and decode
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";
    let clean: Vec<u8> = s
        .bytes()
        .filter(|b| *b != b'\n' && *b != b'\r' && *b != b' ')
        .collect();
    for &b in &clean {
        if !alphabet.contains(&b) {
            return Err(format!("Invalid base64 character: {}", b as char));
        }
    }
    // Decode
    let mut out = Vec::with_capacity(clean.len() * 3 / 4);
    let mut buf = [0u8; 4];
    let mut i = 0;
    while i + 3 < clean.len() {
        for j in 0..4 {
            buf[j] = clean[i + j];
        }
        let v: [u8; 3] = decode_block(buf);
        let pad = clean[i..i + 4].iter().filter(|&&b| b == b'=').count();
        out.extend_from_slice(&v[..3 - pad]);
        i += 4;
    }
    Ok(out)
}

fn decode_block(buf: [u8; 4]) -> [u8; 3] {
    let lookup = |b: u8| -> u8 {
        match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => 0,
        }
    };
    let a = lookup(buf[0]);
    let b = lookup(buf[1]);
    let c = lookup(buf[2]);
    let d = lookup(buf[3]);
    [(a << 2) | (b >> 4), (b << 4) | (c >> 2), (c << 6) | d]
}

/// Build the diagnostic response for GET /health.
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
                        .map(|cp| Cgroup {
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
                    cpu_timeout: entry.proc_handle.cpu_timeout.map(|d| d.as_secs()),
                    memory_limit_bytes: entry.proc_handle.memory_limit_bytes,
                    start_time: entry.proc_handle.start_time.elapsed().as_secs(),
                    start_wallclock_micros: 0, // TODO(re): track wall-clock start
                    cmd_summary: String::new(), // TODO(re): track command summary
                    stdin_bytes: 0,            // TODO(re): track I/O byte counts
                    stdout_bytes: 0,
                    stderr_bytes: 0,
                    trace_emitted: false, // TODO(re): track trace state
                    trace_outcome: None,
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

    // Binary: 810fd3a4 — process limit (ps aux --no-headers, /proc/sys/kernel/pid_max)
    // removed from healthcheck response. Only tracked process state is returned.

    let _serialized: Vec<String> = controllers_with_usage
        .iter()
        .filter_map(|pc| serde_json::to_string(pc).ok())
        .collect();

    format!("{tracked}\nDiagnostic info: [OK\n")
}

/// Persist a key-value pair to `/container_info.json`.
/// Reads the existing file (if present), updates the key, and writes back.
///
/// Binary: edebff2c — the path literal is now the absolute
/// "/container_info.json" at 0x39aa90. 810fd3a4 interned the *relative*
/// "../container_info.json" (0x2a9007) and took suffixes of it; the leading
/// ".." is absent from edebff2c's .rodata entirely, so this build writes the
/// file at the filesystem root rather than one level above the cwd.
/// Also reads "container_name" field on startup for initial container name.
/// Xrefs: "[CONTROL] Failed to persist container name to container_info.json: ",
///   "[CONTROL] Failed to persist auth key to container_info.json: ",
///   "[DEBUG] Read container name from /container_info.json: ",
///   "[DEBUG] Failed to parse /container_info.json: ",
///   "[DEBUG] Failed to read /container_info.json: ",
///   "[DEBUG] container_name field not found in /container_info.json"
fn persist_container_info(key: &str, value: &str) -> Result<(), String> {
    let path = "/container_info.json";
    let mut obj = match std::fs::read_to_string(path) {
        Ok(contents) => serde_json::from_str::<serde_json::Value>(&contents)
            .unwrap_or_else(|_| serde_json::json!({})),
        Err(_) => serde_json::json!({}),
    };
    obj[key] = serde_json::json!(value);
    std::fs::write(
        path,
        serde_json::to_string_pretty(&obj).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

/// Get the current container name from shared state.
pub fn get_container_name(state: &Mutex<Option<String>>) -> Option<String> {
    state.lock().clone()
}
