//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! WebSocket message handler, serde structs for the CreateProcess/ProcessConnection
//! protocol, and stdin/stdout/stderr forwarding over WebSocket.
//!
//! Functions decompiled from:
//!   create_process_deser:        0x233900..0x23567c  (7548 bytes)
//!   stderr_pipe_handler:         0x141db0..0x1432f0  (5440 bytes)
//!   stdout_pipe_handler:         0x144970..0x145eb0  (5440 bytes)
//!   exit_status_formatter:       0x1bafc0..0x1bb772  (1970 bytes)
//!   ws_message_enum_deser:       0x1275e0..0x12766e  (142 bytes)
//!   process_connection_visitor:  0x1e38a0..0x1e38b9  (25 bytes)
//!   create_process_visitor:      0x1e38e0..0x1e38f9  (25 bytes)
//!
//! String refs at binary offset 0x2b7fad (large blob, io.rs strings):
//!   "src/io.rs (PID ) seconds ) exited with status:"
//!   "[DEBUG] Container name mismatch: expected '', actual ''"
//!   "[DEBUG] Current process map:"
//!   "[DEBUG] Adding process  to cgroup"
//!   "[DEBUG] Process with same ID already running:"
//!   "[DEBUG] Processing reattach request for process_id:"
//!   "[DEBUG] Process not found:"
//!   "[DEBUG] Reattaching to detached process:  with PID"
//!   "[DEBUG] Process already attached:"
//!   "[DEBUG] Failed to get first message: Client closed connection"
//!   "First message should be text json CreateProcess"
//!   "[DEBUG] Restoring cgroup ownership for process id  process group PID"
//!   "[DEBUG] New WebSocket connection from"
//!   "[DEBUG] Successfully started stream for process"
//!   "[DEBUG] process_ws_message returned:"
//!   "[DEBUG] stopping stdout and stderr for process"
//!   "[DEBUG] Handling process cleanup for , pid:"
//!   "[DEBUG] Detaching process:"
//!   "[DEBUG] Reattachable process  is done, dropping handle"
//!   "[DEBUG] Non-reattachable process, killing and removing from map:"
//!   "stop_waiting_tx is already taken"
//!   "exit_status_rx is already taken"
//!   "[DEBUG] Received first request:"
//!   " killed by process_api, removing from map"
//!
//! String refs at binary offset 0x2b8584 (io.rs stdout/stdin/ws messages):
//!   "[DEBUG] started stdout pipe"
//!   "[DEBUG] stdout done"
//!   "[DEBUG] started stderr pipe"
//!   "[DEBUG] stderr done"
//!   "[DEBUG] forward_stdin: Starting stdin forwarding for process"
//!   "[DEBUG] Received shutdown signal for process"
//!   "[DEBUG] Stopping waiting for process"
//!   "[DEBUG] Stop waiting for process"
//!   "[DEBUG] process_ws_message: Starting WebSocket message processing"
//!   "[DEBUG] Spawning wait_for_child_to_exit for process"
//!   "[DEBUG] Finished WebSocket message processing for process"
//!   "[DEBUG] Finished waiting for wait_for_child_to_exit"
//!
//! Server->Client message variant strings at binary offset 0x2b8904:
//!   ProcessCreated, AttachedToProcess, ProcessNotRunning,
//!   ProcessAlreadyAttached, FailedToStartProcess, WithSameIdRunning,
//!   InfraError, ExpectStdOut, StdOutEOF, ExpectStdErr, StdErrEOF,
//!   ProcessExited, ProcessTimedOut, ProcessOutOfMemory,
//!   ContainerOutOfMemory, InvalidSignal, FailedToSendSignal,
//!   SignalSent, ShuttingDown
//!
//! Client->Server message variant strings at binary offset 0x2b8b37:
//!   SendSignal, ExpectStdIn

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::{SinkExt, StreamExt};
use nix::sys::signal::Signal;
use nix::unistd::Pid;
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::process::Command;
use tokio::sync::{Mutex as TokioMutex, broadcast, oneshot};
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::Message;

use crate::cgroup::{self, CgroupController};
use crate::control_server;
use crate::oom_killer::{self, OomChannelMap};
use crate::proc_handle::{self, ExitReason, ProcController, ProcHandle, ProcessInfo};
use crate::state::{self, ProcessMap, ProcessState};

// ---------------------------------------------------------------------------
// Serde structs -- Decompiled from 0x233900..0x23567c and 0x1e38e0/0x1e38a0
// ---------------------------------------------------------------------------

/// Decompiled from 0x1e38e0..0x1e38f9  (25 bytes)  -- serde struct visitor
/// and  0x233900..0x23567c  (7548 bytes) -- full deserializer
/// Xrefs: "struct CreateProcess with 10 elements", "name", "args",
///   "clear_env", "uid", "gid", "reattachable", "allow_process_id_reuse",
///   "timeout", "memory_limit_bytes", "env_vars"
#[derive(Debug, Deserialize)]
pub struct CreateProcess {
    pub name: String,
    pub args: Vec<String>,
    #[serde(default)]
    pub env_vars: Option<HashMap<String, String>>,
    #[serde(default)]
    pub clear_env: Option<bool>,
    #[serde(default)]
    pub uid: Option<u32>,
    #[serde(default)]
    pub gid: Option<u32>,
    #[serde(default)]
    pub reattachable: Option<bool>,
    #[serde(default)]
    pub allow_process_id_reuse: Option<bool>,
    #[serde(default)]
    pub timeout: Option<u64>,
    #[serde(default)]
    pub memory_limit_bytes: Option<u64>,
}

/// Decompiled from 0x1e38a0..0x1e38b9  (25 bytes) -- serde struct visitor
/// Xrefs: "struct ProcessConnection with 3 elements"
#[derive(Debug, Deserialize)]
pub struct ProcessConnection {
    pub process_id: String,
    #[serde(default)]
    pub reattach: Option<bool>,
    #[serde(default)]
    pub expected_container_name: Option<String>,
}

/// Client->Server WebSocket message.
/// Decompiled from 0x1275e0..0x12766e  (142 bytes)
/// Xrefs: "SendSignalExpectStdIn"
#[derive(Debug, Deserialize)]
#[serde(tag = "type")]
pub enum ClientMessage {
    SendSignal { signal: String },
    ExpectStdIn,
    StdInEOF,
}

/// Server->Client WebSocket message type tags.
/// String refs at binary offset 0x2b8904
#[derive(Debug, Serialize)]
#[serde(tag = "type")]
pub enum ServerMessage {
    ProcessCreated { process_id: String, pid: u32 },
    AttachedToProcess { process_id: String, pid: u32 },
    ProcessNotRunning { process_id: String },
    ProcessAlreadyAttached { process_id: String },
    FailedToStartProcess { error: String },
    WithSameIdRunning { process_id: String },
    InfraError { error: String },
    ExpectStdOut,
    StdOutEOF,
    ExpectStdErr,
    StdErrEOF,
    ProcessExited { status: i32, details: String },
    ProcessTimedOut { timeout_secs: u64, details: String },
    ProcessOutOfMemory { limit_bytes: u64, details: String },
    ContainerOutOfMemory { limit_bytes: u64, details: String },
    InvalidSignal { signal: String },
    FailedToSendSignal { error: String },
    SignalSent { signal: String },
    ShuttingDown,
}

// ---------------------------------------------------------------------------
// First-message union: either CreateProcess or ProcessConnection
// ---------------------------------------------------------------------------

/// The first JSON text message on a new WebSocket connection is either
/// a CreateProcess or a ProcessConnection (for reattach).
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum FirstMessage {
    Create(CreateProcess),
    Connect(ProcessConnection),
}

/// Per-connection state wrapping WebSocket sender + process state.
/// Serde visitor at 0x21c2b0..0x21c510 (608 bytes).
/// Fields from disassembly: process_info, proc_handle, controller,
///   stop_waiting_rx, stop_waiting_tx, exit_status_rx, exit_status_tx,
///   oom_killed_rx
#[derive(Debug, Clone)]
pub struct WsStreamHandle {
    tx: Arc<TokioMutex<futures::stream::SplitSink<WebSocketStream<TcpStream>, Message>>>,
    /// Per-connection process info snapshot (populated after CreateProcess).
    pub process_info: Option<ProcessInfo>,
    /// Controller wrapping cgroup + OOM channel.
    pub controller: Option<ProcController>,
}

/// Helper to send a ServerMessage as JSON text over the shared WebSocket sender.
async fn send_msg(
    ws_tx: &WsStreamHandle,
    msg: &ServerMessage,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let json = serde_json::to_string(msg)?;
    let mut tx = ws_tx.tx.lock().await;
    tx.send(Message::text(json)).await?;
    Ok(())
}

/// Helper to send raw binary data over the shared WebSocket sender.
async fn send_binary(
    ws_tx: &WsStreamHandle,
    data: Vec<u8>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut tx = ws_tx.tx.lock().await;
    tx.send(Message::binary(data)).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Stdin forwarding
// ---------------------------------------------------------------------------

/// Forward stdin data to the child process.
/// Xrefs: "[DEBUG] forward_stdin: Starting stdin forwarding for process"
async fn forward_stdin(
    stdin_writer: &mut Option<tokio::process::ChildStdin>,
    data: &[u8],
    process_id: &str,
) {
    if let Some(stdin) = stdin_writer {
        if let Err(e) = stdin.write_all(data).await {
            log::debug!(
                "[DEBUG] stdin write failed for process {process_id} (process likely exited): {e}"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// WebSocket message processing loop (separate function per binary evidence)
// ---------------------------------------------------------------------------

/// Main WebSocket message processing loop for a process.
///
/// String refs at binary offset 0x2b8584:
///   "[DEBUG] process_ws_message: Starting WebSocket message processing"
///   "process_ws_message: Shutting down, terminating"
///   "process_ws_message: Timeout"
///   "process_ws_message: OOM"
///   "process_ws_message: Container OOM"
///   "Failed to receive message:"
///   "Expected binary message after ExpectStdIn"
///   "[DEBUG] Process stream is closed"
///   "[DEBUG] Failed to send response:"
#[allow(clippy::too_many_arguments)] // Matches binary's function signature
async fn process_ws_message(
    ws_tx: &WsStreamHandle,
    mut ws_rx: futures::stream::SplitStream<WebSocketStream<TcpStream>>,
    process_id: &str,
    pid: u32,
    mut stdin_writer: Option<tokio::process::ChildStdin>,
    mut exit_status_rx: oneshot::Receiver<ExitReason>,
    mut shutdown_rx: broadcast::Receiver<()>,
    start_time: Instant,
) -> Result<String, String> {
    log::debug!(
        "[DEBUG] process_ws_message: Starting WebSocket message processing for process {process_id}"
    );

    let mut expecting_stdin = false;
    let mut stdin_started = false;

    let result = loop {
        tokio::select! {
            msg = ws_rx.next() => {
                match msg {
                    Some(Ok(msg)) if msg.is_text() => {
                        if expecting_stdin {
                            log::debug!("Expected binary message after ExpectStdIn");
                            expecting_stdin = false;
                        }
                        let text = msg.into_text().unwrap_or_default();
                        if let Ok(client_msg) = serde_json::from_str::<ClientMessage>(&text) {
                            match client_msg {
                                ClientMessage::SendSignal { signal } => {
                                    handle_send_signal(pid, &signal, ws_tx).await;
                                }
                                ClientMessage::ExpectStdIn => {
                                    if !stdin_started {
                                        log::debug!(
                                            "[DEBUG] forward_stdin: Starting stdin forwarding for process {process_id}"
                                        );
                                        stdin_started = true;
                                    }
                                    expecting_stdin = true;
                                }
                                ClientMessage::StdInEOF => {
                                    stdin_writer = None;
                                }
                            }
                        }
                    }
                    Some(Ok(msg)) if msg.is_binary() => {
                        if !expecting_stdin {
                            log::debug!("Expected binary message after ExpectStdIn");
                        }
                        expecting_stdin = false;
                        forward_stdin(&mut stdin_writer, &msg.into_data(), process_id).await;
                    }
                    Some(Ok(msg)) if msg.is_close() => {
                        log::debug!("[DEBUG] Process stream is closed");
                        break Ok("process_ws_message: Client disconnected".to_string());
                    }
                    Some(Err(e)) => {
                        log::debug!("Failed to receive message: {e}");
                        break Err(format!("process_ws_message: {e}"));
                    }
                    None => {
                        log::debug!("[DEBUG] Process stream is closed");
                        break Ok("process_ws_message: Stream ended".to_string());
                    }
                    _ => {}
                }
            }
            reason = &mut exit_status_rx => {
                log::debug!("[DEBUG] Finished waiting for wait_for_child_to_exit");
                let exit_reason = match reason {
                    Ok(r) => r,
                    Err(_) => ExitReason::KilledByProcessApi,
                };

                let elapsed_secs = start_time.elapsed().as_secs_f64();
                let details = proc_handle::format_exit_reason(pid, &exit_reason, elapsed_secs);

                let send_result = match &exit_reason {
                    ExitReason::Exited { status } => {
                        send_msg(ws_tx, &ServerMessage::ProcessExited {
                            status: *status,
                            details,
                        }).await
                    }
                    ExitReason::Signaled { signal, .. } => {
                        send_msg(ws_tx, &ServerMessage::ProcessExited {
                            status: *signal,
                            details,
                        }).await
                    }
                    ExitReason::TimedOut { timeout_secs } => {
                        send_msg(ws_tx, &ServerMessage::ProcessTimedOut {
                            timeout_secs: *timeout_secs,
                            details,
                        }).await
                    }
                    ExitReason::OutOfMemory { limit_bytes } => {
                        send_msg(ws_tx, &ServerMessage::ProcessOutOfMemory {
                            limit_bytes: *limit_bytes,
                            details,
                        }).await
                    }
                    ExitReason::ContainerOom { limit_bytes } => {
                        send_msg(ws_tx, &ServerMessage::ContainerOutOfMemory {
                            limit_bytes: *limit_bytes,
                            details,
                        }).await
                    }
                    ExitReason::KilledByProcessApi => {
                        send_msg(ws_tx, &ServerMessage::ProcessExited {
                            status: -1,
                            details,
                        }).await
                    }
                };

                if let Err(e) = send_result {
                    log::debug!("[DEBUG] Failed to send response: {e}");
                }

                break match &exit_reason {
                    ExitReason::TimedOut { .. } => Ok("process_ws_message: Timeout".to_string()),
                    ExitReason::OutOfMemory { .. } => Ok("process_ws_message: OOM".to_string()),
                    ExitReason::ContainerOom { .. } => Ok("process_ws_message: Container OOM".to_string()),
                    ExitReason::KilledByProcessApi => Ok("process_ws_message: Killed".to_string()),
                    _ => Ok("process_ws_message: Process exited".to_string()),
                };
            }
            _ = shutdown_rx.recv() => {
                log::debug!("[DEBUG] Received shutdown signal for process {process_id}");
                if let Err(e) = send_msg(ws_tx, &ServerMessage::ShuttingDown).await {
                    log::debug!("[DEBUG] Failed to send response: {e}");
                }
                break Ok("process_ws_message: Shutting down, terminating".to_string());
            }
        }
    };

    log::debug!("[DEBUG] Finished WebSocket message processing for process {process_id}");
    result
}

// ---------------------------------------------------------------------------
// WebSocket connection handler
// ---------------------------------------------------------------------------

/// Handle a new WebSocket connection end-to-end.
///
/// String refs at binary offset 0x2b7fad:
///   "[DEBUG] New WebSocket connection from"
///   "[DEBUG] Failed to get first message: Client closed connection"
///   "First message should be text json CreateProcess"
///   "[DEBUG] process_ws_message: Starting WebSocket message processing"
///   "[DEBUG] Finished WebSocket message processing for process"
#[allow(clippy::too_many_arguments)] // Matches binary's function signature
pub async fn handle_ws_connection(
    ws_stream: WebSocketStream<TcpStream>,
    remote_addr: std::net::SocketAddr,
    proc_map: ProcessMap,
    controller: CgroupController,
    container_memory_limit: Option<u64>,
    oom_polling_period: Duration,
    container_name: Arc<Mutex<Option<String>>>,
    shutdown_tx: broadcast::Sender<()>,
    oom_channels: OomChannelMap,
) {
    log::debug!("[DEBUG] New WebSocket connection from {remote_addr}");

    let (ws_sink, mut ws_rx) = ws_stream.split();
    let ws_tx = WsStreamHandle {
        tx: Arc::new(TokioMutex::new(ws_sink)),
        process_info: None,
        controller: None,
    };

    // Read first message to determine if this is CreateProcess or ProcessConnection
    let first_msg = match ws_rx.next().await {
        Some(Ok(msg)) if msg.is_text() => msg.into_text().unwrap_or_default(),
        Some(Ok(_)) => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::InfraError {
                    error: "First message should be text json CreateProcess".to_string(),
                },
            )
            .await;
            return;
        }
        _ => {
            log::debug!("[DEBUG] Failed to get first message: Client closed connection");
            return;
        }
    };

    log::debug!("[DEBUG] Received first request: {first_msg}");

    // Parse the first message
    let first: FirstMessage = match serde_json::from_str(&first_msg) {
        Ok(m) => m,
        Err(e) => {
            log::debug!("[DEBUG] Failed to parse JSON: {e}");
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::FailedToStartProcess {
                    error: format!("Failed to parse JSON: {e}"),
                },
            )
            .await;
            return;
        }
    };

    match first {
        FirstMessage::Create(create_req) => {
            handle_create_process(
                create_req,
                ws_tx,
                ws_rx,
                proc_map,
                controller,
                container_memory_limit,
                oom_polling_period,
                container_name,
                shutdown_tx,
                oom_channels,
            )
            .await;
        }
        FirstMessage::Connect(conn_req) => {
            handle_process_connection(conn_req, ws_tx, ws_rx, proc_map, container_name).await;
        }
    }
}

/// Handle a CreateProcess request: spawn the process, set up I/O forwarding,
/// and monitor its lifecycle.
///
/// String refs at binary offset 0x2b7fad:
///   "[DEBUG] Adding process to cgroup"
///   "[DEBUG] Process with same ID already running:"
///   "[DEBUG] Successfully started stream for process"
///   "[DEBUG] Spawning wait_for_child_to_exit for process"
///   "[DEBUG] stopping stdout and stderr for process"
///   "[DEBUG] Handling process cleanup for , pid:"
///   "stop_waiting_tx is already taken"
///   "exit_status_rx is already taken"
#[allow(clippy::too_many_arguments)] // Matches binary's function signature
async fn handle_create_process(
    req: CreateProcess,
    mut ws_tx: WsStreamHandle,
    ws_rx: futures::stream::SplitStream<WebSocketStream<TcpStream>>,
    proc_map: ProcessMap,
    controller: CgroupController,
    _container_memory_limit: Option<u64>,
    oom_polling_period: Duration,
    _container_name: Arc<Mutex<Option<String>>>,
    shutdown_tx: broadcast::Sender<()>,
    oom_channels: OomChannelMap,
) {
    // Generate a process ID (use name as the ID)
    let process_id = req.name.clone();

    // Check if a process with the same ID is already running
    if !req.allow_process_id_reuse.unwrap_or(true) && state::process_exists(&proc_map, &process_id)
    {
        log::debug!("[DEBUG] Process with same ID already running: {process_id}");
        let _ = send_msg(
            &ws_tx,
            &ServerMessage::WithSameIdRunning {
                process_id: process_id.clone(),
            },
        )
        .await;
        return;
    }

    // Spawn the process
    let mut cmd = Command::new(&req.name);
    cmd.args(&req.args);

    // Clear environment if requested
    if req.clear_env.unwrap_or(false) {
        cmd.env_clear();
    }

    // Set environment variables
    if let Some(ref env_vars) = req.env_vars {
        for (key, value) in env_vars {
            cmd.env(key, value);
        }
    }

    // Set UID/GID if specified
    if let Some(uid) = req.uid {
        cmd.uid(uid);
    }
    if let Some(gid) = req.gid {
        cmd.gid(gid);
    }

    // Configure stdio
    cmd.stdin(std::process::Stdio::piped());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    // Set process group (setsid)
    unsafe {
        cmd.pre_exec(|| {
            nix::unistd::setsid().map_err(std::io::Error::other)?;
            Ok(())
        });
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            log::debug!("[DEBUG] Failed to spawn process: {e}");
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::FailedToStartProcess {
                    error: format!("Failed to spawn process: {e}"),
                },
            )
            .await;
            return;
        }
    };

    let pid = child.id().expect("child should have PID");
    log::debug!("[DEBUG] Successfully started stream for process {process_id} (PID {pid})");

    // Set up cgroup for the process
    let cgroup_path = if let Some(limit) = req.memory_limit_bytes {
        match cgroup::create_process_cgroup(&controller.base_path, pid).await {
            Ok(path) => {
                log::debug!(
                    "[DEBUG] Adding process {process_id} to cgroup {}",
                    path.display()
                );
                let _ = cgroup::add_process_to_cgroup(&path, pid).await;
                let _ = cgroup::set_memory_limit(&path, controller.version, limit).await;
                Some(path)
            }
            Err(e) => {
                log::debug!("[DEBUG] Failed to create cgroup for {process_id}: {e}");
                None
            }
        }
    } else {
        None
    };

    // Create channels for inter-task communication
    let (exit_status_tx, exit_status_rx) = oneshot::channel();
    let (oom_killed_tx, oom_killed_rx) = oneshot::channel();
    let (stop_waiting_tx, stop_waiting_rx) = oneshot::channel();

    // Create process handle with all channel endpoints stored
    let timeout = req.timeout.map(Duration::from_secs);
    let reattachable = req.reattachable.unwrap_or(false);
    let mut handle = ProcHandle::new(pid, reattachable, timeout, req.memory_limit_bytes);
    handle.memory_cgroup_path = cgroup_path.clone();
    handle.stop_waiting_tx = Some(stop_waiting_tx);
    handle.stop_waiting_rx = Some(stop_waiting_rx);
    handle.exit_status_rx = Some(exit_status_rx);
    handle.exit_status_tx = Some(exit_status_tx);
    handle.oom_killed_tx = Some(oom_killed_tx);
    handle.oom_killed_rx = Some(oom_killed_rx);

    // Insert into process map
    state::insert_process(&proc_map, process_id.clone(), pid, reattachable, handle);

    // Send ProcessCreated response
    let _ = send_msg(
        &ws_tx,
        &ServerMessage::ProcessCreated {
            process_id: process_id.clone(),
            pid,
        },
    )
    .await;

    // Populate per-connection process info and controller on WsStreamHandle
    let process_info = ProcessInfo {
        process_id: process_id.clone(),
        pid,
        reattachable,
        timeout: req.timeout,
        memory_limit_bytes: req.memory_limit_bytes,
        start_time: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
    };
    let cgroup_config = cgroup_path.as_ref().map(|cp| proc_handle::CgroupConfig {
        process_id: process_id.clone(),
        memory_limit_bytes: req.memory_limit_bytes,
        memory_usage_bytes: None,
        memory_cgroup_path: Some(cp.display().to_string()),
        process_group_pid: pid,
        internal_state: "Attached".to_string(),
    });
    ws_tx.process_info = Some(process_info.clone());
    ws_tx.controller = Some(ProcController {
        cgroup: cgroup_config,
        oom_killed_tx: None,
        process_info,
    });

    // Take channels and config from the handle in the map
    let (
        start_time,
        handle_timeout,
        handle_memory_limit,
        exit_status_tx,
        oom_killed_tx,
        oom_killed_rx,
        stop_waiting_rx,
    ) = {
        let mut map = proc_map.lock();
        let entry = map.get_mut(&process_id).expect("just inserted");
        (
            entry.proc_handle.start_time,
            entry.proc_handle.timeout,
            entry.proc_handle.memory_limit_bytes,
            entry
                .proc_handle
                .exit_status_tx
                .take()
                .expect("exit_status_tx is already taken"),
            entry.proc_handle.oom_killed_tx.take(),
            entry.proc_handle.oom_killed_rx.take(),
            entry.proc_handle.stop_waiting_rx.take(),
        )
    };

    // Register OOM channel in the shared map so both per-process and
    // container OOM monitors can signal this process
    if let Some(oom_killed_tx) = oom_killed_tx {
        let mut channels = oom_channels.lock();
        channels.insert(process_id.clone(), oom_killed_tx);
    }

    // Spawn per-process memory monitor if memory limit is set
    if let (Some(limit), Some(cp)) = (handle_memory_limit, &cgroup_path) {
        let oom_shutdown_rx = shutdown_tx.subscribe();
        let oom_channels_clone = oom_channels.clone();
        tokio::spawn(oom_killer::per_process_memory_monitor(
            pid,
            process_id.clone(),
            cp.clone(),
            controller.version,
            limit,
            oom_polling_period,
            oom_channels_clone,
            oom_shutdown_rx,
        ));
    }

    // Spawn wait_for_child_to_exit task
    log::debug!("[DEBUG] Spawning wait_for_child_to_exit for process {process_id}");
    tokio::spawn(proc_handle::wait_for_child_to_exit(
        pid,
        handle_timeout,
        handle_memory_limit,
        cgroup_path.clone(),
        Some(controller.version),
        oom_killed_rx,
        stop_waiting_rx,
        exit_status_tx,
    ));

    // Forward stdout, stderr, and handle stdin
    let stdin = child.stdin.take();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let shutdown_rx = shutdown_tx.subscribe();

    // Spawn stdout forwarder
    // Decompiled from 0x144970..0x145eb0  (5440 bytes)
    // Xrefs: "[DEBUG] started stdout pipe", "[DEBUG] stdout done"
    if let Some(mut stdout) = stdout {
        let ws_tx_clone = ws_tx.clone();
        tokio::spawn(async move {
            log::debug!("[DEBUG] started stdout pipe");
            let mut buf = vec![0u8; 65536];
            loop {
                match stdout.read(&mut buf).await {
                    Ok(0) => break,
                    Ok(n) => {
                        let _ = send_msg(&ws_tx_clone, &ServerMessage::ExpectStdOut).await;
                        let _ = send_binary(&ws_tx_clone, buf[..n].to_vec()).await;
                    }
                    Err(_) => break,
                }
            }
            let _ = send_msg(&ws_tx_clone, &ServerMessage::StdOutEOF).await;
            log::debug!("[DEBUG] stdout done");
        });
    }

    // Spawn stderr forwarder
    // Decompiled from 0x141db0..0x1432f0  (5440 bytes)
    // Xrefs: "[DEBUG] started stderr pipe", "[DEBUG] stderr done"
    if let Some(mut stderr) = stderr {
        let ws_tx_clone = ws_tx.clone();
        tokio::spawn(async move {
            log::debug!("[DEBUG] started stderr pipe");
            let mut buf = vec![0u8; 65536];
            loop {
                match stderr.read(&mut buf).await {
                    Ok(0) => break,
                    Ok(n) => {
                        let _ = send_msg(&ws_tx_clone, &ServerMessage::ExpectStdErr).await;
                        let _ = send_binary(&ws_tx_clone, buf[..n].to_vec()).await;
                    }
                    Err(_) => break,
                }
            }
            let _ = send_msg(&ws_tx_clone, &ServerMessage::StdErrEOF).await;
            log::debug!("[DEBUG] stderr done");
        });
    }

    // Take exit_status_rx from the handle in the map.
    // Xrefs: "exit_status_rx is already taken"
    let exit_status_rx = {
        let mut map = proc_map.lock();
        let entry = map.get_mut(&process_id).expect("just inserted");
        entry
            .proc_handle
            .exit_status_rx
            .take()
            .expect("exit_status_rx is already taken")
    };

    // Run the WebSocket message processing loop (as separate function per binary evidence)
    let result = process_ws_message(
        &ws_tx,
        ws_rx,
        &process_id,
        pid,
        stdin,
        exit_status_rx,
        shutdown_rx,
        start_time,
    )
    .await;

    match &result {
        Ok(msg) => log::debug!("[DEBUG] process_ws_message returned: {msg}"),
        Err(msg) => log::debug!("[DEBUG] process_ws_message failed: {msg}"),
    }

    // Close websocket
    // Xrefs: "Closing websocket", "error closing websocket:"
    log::debug!("Closing websocket");
    {
        let mut tx = ws_tx.tx.lock().await;
        if let Err(e) = tx.close().await {
            log::debug!("error closing websocket: {e}");
        }
    }

    // Process cleanup
    log::debug!("[DEBUG] stopping stdout and stderr for process {process_id}");

    let (entry_reattachable, handle_pgid) = {
        let map = proc_map.lock();
        match map.get(&process_id) {
            Some(entry) => (
                entry.proc_handle.reattachable,
                entry.proc_handle.process_group_pid,
            ),
            None => (false, pid),
        }
    };

    log::debug!("[DEBUG] Handling process cleanup for {process_id}, pid: {pid}");

    if entry_reattachable {
        log::debug!("[DEBUG] Detaching process: {process_id}");
        if let Err(e) = state::detach_process(&proc_map, &process_id) {
            log::error!("[ERROR] Failed to detach process {process_id}: {e}");
        } else {
            log::debug!("[DEBUG] Successfully detached process {process_id}");
        }
        log::debug!("[DEBUG] Reattachable process {process_id} is done, dropping handle");
    } else {
        // Signal the wait task to stop and mark as killed
        {
            let mut map = proc_map.lock();
            if let Some(entry) = map.get_mut(&process_id) {
                log::debug!("[DEBUG] Stopping waiting for process {process_id}");
                match entry.proc_handle.stop_waiting_tx.take() {
                    Some(tx) => {
                        if tx.send(()).is_err() {
                            log::debug!(
                                "[DEBUG] Failed to send stop signal for process {process_id})"
                            );
                        }
                    }
                    None => {
                        log::debug!("stop_waiting_tx is already taken");
                    }
                }
                entry.proc_handle.killed_by_process_api = true;
            }
        }
        log::debug!(
            "[DEBUG] Non-reattachable process, killing and removing from map: {process_id}"
        );
        log::debug!(
            "[DEBUG] Restoring cgroup ownership for process id {process_id} process group PID {handle_pgid}"
        );
        // Transition to Done before removal
        let _ = state::transition_state(
            &proc_map,
            &process_id,
            ProcessState::Attached,
            ProcessState::Done,
        );
        proc_handle::kill_and_wait(pid, cgroup_path.as_ref()).await;
        let removed = state::remove_process(&proc_map, &process_id);
        if let Some(entry) = removed {
            if entry.proc_handle.killed_by_process_api {
                log::debug!("{process_id} killed by process_api, removing from map");
            }
        }
    }

    log::debug!(
        "[DEBUG] After cleaning up process {process_id}, proc_map: {}",
        state::debug_process_map(&proc_map)
    );
}

/// Handle a ProcessConnection (reattach) request.
///
/// String refs at binary offset 0x2b7fad:
///   "[DEBUG] Processing reattach request for process_id:"
///   "[DEBUG] Process not found:"
///   "[DEBUG] Reattaching to detached process: with PID"
///   "[DEBUG] Process already attached:"
async fn handle_process_connection(
    req: ProcessConnection,
    ws_tx: WsStreamHandle,
    _ws_rx: futures::stream::SplitStream<WebSocketStream<TcpStream>>,
    proc_map: ProcessMap,
    container_name: Arc<Mutex<Option<String>>>,
) {
    let process_id = &req.process_id;
    let should_reattach = req.reattach.unwrap_or(true);
    log::debug!(
        "[DEBUG] Processing reattach request for process_id: {process_id}, reattach: {should_reattach}"
    );

    // Check container name if expected
    // Read dynamic container name from shared state (updated by control server)
    let actual_name = control_server::get_container_name(&container_name);
    if let Some(ref expected) = req.expected_container_name {
        if let Some(ref actual) = actual_name {
            if expected != actual {
                log::debug!(
                    "[DEBUG] Container name mismatch: expected '{expected}', actual '{actual}'"
                );
                let _ = send_msg(
                    &ws_tx,
                    &ServerMessage::InfraError {
                        error: format!(
                            "Container name mismatch: Expected container '{expected}', but connected to '{actual}'"
                        ),
                    },
                )
                .await;
                return;
            }
        }
    }

    if !should_reattach {
        // Just query state without reattaching
        let _ = send_msg(
            &ws_tx,
            &ServerMessage::ProcessNotRunning {
                process_id: process_id.clone(),
            },
        )
        .await;
        return;
    }

    // Look up the process state
    let state_result = state::lookup_process(&proc_map, process_id);
    match state_result {
        Err(_) => {
            log::debug!("[DEBUG] Process not found: {process_id}");
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessNotRunning {
                    process_id: process_id.clone(),
                },
            )
            .await;
        }
        Ok(ProcessState::Attached) => {
            log::debug!("[DEBUG] Process already attached: {process_id}");
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessAlreadyAttached {
                    process_id: process_id.clone(),
                },
            )
            .await;
        }
        Ok(ProcessState::Detached) => {
            let pid = {
                let map = proc_map.lock();
                map.get(process_id).map(|e| e.proc_handle.pid).unwrap_or(0)
            };
            log::debug!("[DEBUG] Reattaching to detached process: {process_id} with PID {pid}");

            if let Err(e) = state::attach_process(&proc_map, process_id) {
                let _ = send_msg(&ws_tx, &ServerMessage::InfraError { error: e }).await;
                return;
            }

            let _ = send_msg(
                &ws_tx,
                &ServerMessage::AttachedToProcess {
                    process_id: process_id.clone(),
                    pid,
                },
            )
            .await;
        }
        Ok(ProcessState::Done) => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessNotRunning {
                    process_id: process_id.clone(),
                },
            )
            .await;
        }
    }
}

/// Handle a SendSignal client message.
///
/// String refs at binary offset 0x2b8584:
///   "[DEBUG] Failed to send stop signal for process ), error:"
async fn handle_send_signal(pid: u32, signal_str: &str, ws_tx: &WsStreamHandle) {
    let signal = match parse_signal(signal_str) {
        Some(s) => s,
        None => {
            let _ = send_msg(
                ws_tx,
                &ServerMessage::InvalidSignal {
                    signal: signal_str.to_string(),
                },
            )
            .await;
            return;
        }
    };

    match nix::sys::signal::kill(Pid::from_raw(pid as i32), signal) {
        Ok(()) => {
            let _ = send_msg(
                ws_tx,
                &ServerMessage::SignalSent {
                    signal: signal_str.to_string(),
                },
            )
            .await;
        }
        Err(e) => {
            let _ = send_msg(
                ws_tx,
                &ServerMessage::FailedToSendSignal {
                    error: format!("Failed to send signal: {e}"),
                },
            )
            .await;
        }
    }
}

/// Parse a signal name or number into a nix Signal.
fn parse_signal(s: &str) -> Option<Signal> {
    // Try numeric first
    if let Ok(num) = s.parse::<i32>() {
        return Signal::try_from(num).ok();
    }

    // Try named signals
    match s.to_uppercase().trim_start_matches("SIG") {
        "HUP" => Some(Signal::SIGHUP),
        "INT" => Some(Signal::SIGINT),
        "QUIT" => Some(Signal::SIGQUIT),
        "KILL" => Some(Signal::SIGKILL),
        "TERM" => Some(Signal::SIGTERM),
        "USR1" => Some(Signal::SIGUSR1),
        "USR2" => Some(Signal::SIGUSR2),
        "CONT" => Some(Signal::SIGCONT),
        "STOP" => Some(Signal::SIGSTOP),
        _ => None,
    }
}
