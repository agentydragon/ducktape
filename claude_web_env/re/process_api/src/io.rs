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
//!   "[DEBUG] Restoring cgroup ownership for process id"
//!   "[DEBUG] New WebSocket connection from"
//!   "[DEBUG] Successfully started stream for process"
//!   "[DEBUG] process_ws_message returned:"
//!   "[DEBUG] stopping stdout and stderr for process"
//!   "[DEBUG] Handling process cleanup for , pid:"
//!   "[DEBUG] Detaching process:"
//!   "[DEBUG] Reattachable process  is done, dropping handle"
//!   "[DEBUG] Non-reattachable process, killing and removing from map:"
//!
//! String refs at binary offset 0x2b8584 (io.rs stdout/stdin/ws messages):
//!   "[DEBUG] started stdout pipe"
//!   "[DEBUG] stdout done"
//!   "[DEBUG] started stderr pipe"
//!   "[DEBUG] stderr done"
//!   "exit_status_rx is already taken"
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
use std::time::Duration;

use futures::{SinkExt, StreamExt};
use nix::sys::signal::Signal;
use nix::unistd::Pid;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::process::Command;
use tokio::sync::{broadcast, oneshot, Mutex as TokioMutex};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

use crate::cgroup::{self, CgroupController};
use crate::proc_handle::{self, ExitReason, ProcHandle};
use crate::state::{self, ProcessMap};

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

/// Shared WebSocket sender type, wrapped in Arc<Mutex> for multi-task access.
type WsTx = Arc<TokioMutex<futures::stream::SplitSink<WebSocketStream<TcpStream>, Message>>>;

/// Helper to send a ServerMessage as JSON text over the shared WebSocket sender.
async fn send_msg(ws_tx: &WsTx, msg: &ServerMessage) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let json = serde_json::to_string(msg)?;
    let mut tx = ws_tx.lock().await;
    tx.send(Message::text(json)).await?;
    Ok(())
}

/// Helper to send raw binary data over the shared WebSocket sender.
async fn send_binary(ws_tx: &WsTx, data: Vec<u8>) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut tx = ws_tx.lock().await;
    tx.send(Message::binary(data)).await?;
    Ok(())
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
pub async fn handle_ws_connection(
    ws_stream: WebSocketStream<TcpStream>,
    remote_addr: std::net::SocketAddr,
    proc_map: ProcessMap,
    controller: CgroupController,
    container_memory_limit: Option<u64>,
    oom_polling_period: Duration,
    container_name: Option<String>,
    shutdown_tx: broadcast::Sender<()>,
) {
    log::debug!("[DEBUG] New WebSocket connection from {remote_addr}");

    let (ws_sink, mut ws_rx) = ws_stream.split();
    let ws_tx: WsTx = Arc::new(TokioMutex::new(ws_sink));

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
            )
            .await;
        }
        FirstMessage::Connect(conn_req) => {
            handle_process_connection(
                conn_req,
                ws_tx,
                ws_rx,
                proc_map,
                container_name,
            )
            .await;
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
async fn handle_create_process(
    req: CreateProcess,
    ws_tx: WsTx,
    mut ws_rx: futures::stream::SplitStream<WebSocketStream<TcpStream>>,
    proc_map: ProcessMap,
    controller: CgroupController,
    _container_memory_limit: Option<u64>,
    _oom_polling_period: Duration,
    _container_name: Option<String>,
    shutdown_tx: broadcast::Sender<()>,
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
        unsafe {
            cmd.uid(uid);
        }
    }
    if let Some(gid) = req.gid {
        unsafe {
            cmd.gid(gid);
        }
    }

    // Configure stdio
    cmd.stdin(std::process::Stdio::piped());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    // Set process group (setsid)
    unsafe {
        cmd.pre_exec(|| {
            nix::unistd::setsid().map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
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
                log::debug!("[DEBUG] Adding process {process_id} to cgroup {}", path.display());
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

    // Create process handle
    let timeout = req.timeout.map(Duration::from_secs);
    let handle = ProcHandle::new(
        pid,
        req.reattachable.unwrap_or(false),
        timeout,
        req.memory_limit_bytes,
    );

    // Insert into process map
    state::insert_process(
        &proc_map,
        process_id.clone(),
        pid,
        req.reattachable.unwrap_or(false),
        handle,
    );

    // Send ProcessCreated response
    let _ = send_msg(
        &ws_tx,
        &ServerMessage::ProcessCreated {
            process_id: process_id.clone(),
            pid,
        },
    )
    .await;

    // Set up OOM monitoring channel
    let (_oom_tx, oom_rx) = oneshot::channel();
    let (_stop_tx, stop_rx) = oneshot::channel();

    // Spawn wait_for_child_to_exit task
    log::debug!("[DEBUG] Spawning wait_for_child_to_exit for process {process_id}");
    let wait_handle = tokio::spawn(proc_handle::wait_for_child_to_exit(
        pid,
        timeout,
        req.memory_limit_bytes,
        cgroup_path.clone(),
        Some(controller.version),
        Some(oom_rx),
        Some(stop_rx),
    ));

    // Forward stdout, stderr, and handle stdin
    let stdin = child.stdin.take();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let mut shutdown_rx = shutdown_tx.subscribe();

    // Spawn stdout forwarder
    /// Decompiled from 0x144970..0x145eb0  (5440 bytes)
    /// Xrefs: "[DEBUG] started stdout pipe", "[DEBUG] stdout done"
    if let Some(mut stdout) = stdout {
        let ws_tx_clone = Arc::clone(&ws_tx);
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
    /// Decompiled from 0x141db0..0x1432f0  (5440 bytes)
    /// Xrefs: "[DEBUG] started stderr pipe", "[DEBUG] stderr done"
    if let Some(mut stderr) = stderr {
        let ws_tx_clone = Arc::clone(&ws_tx);
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

    // Main WebSocket message processing loop
    log::debug!(
        "[DEBUG] process_ws_message: Starting WebSocket message processing for process {process_id}"
    );

    let mut stdin_writer = stdin;

    loop {
        tokio::select! {
            msg = ws_rx.next() => {
                match msg {
                    Some(Ok(msg)) if msg.is_text() => {
                        let text = msg.into_text().unwrap_or_default();
                        if let Ok(client_msg) = serde_json::from_str::<ClientMessage>(&text) {
                            match client_msg {
                                ClientMessage::SendSignal { signal } => {
                                    handle_send_signal(pid, &signal, &ws_tx).await;
                                }
                                ClientMessage::ExpectStdIn => {
                                    // Next binary message is stdin data
                                }
                                ClientMessage::StdInEOF => {
                                    // Close stdin
                                    stdin_writer = None;
                                }
                            }
                        }
                    }
                    Some(Ok(msg)) if msg.is_binary() => {
                        let data = msg.into_data();
                        // Write stdin data
                        if let Some(ref mut stdin) = stdin_writer {
                            if let Err(e) = stdin.write_all(&data).await {
                                log::debug!(
                                    "[DEBUG] stdin write failed for process {process_id} \
                                     (process likely exited): {e}"
                                );
                            }
                        }
                    }
                    Some(Ok(msg)) if msg.is_close() => {
                        break;
                    }
                    None => {
                        break;
                    }
                    _ => {}
                }
            }
            _ = shutdown_rx.recv() => {
                log::debug!("[DEBUG] Received shutdown signal for process {process_id}");
                let _ = send_msg(&ws_tx, &ServerMessage::ShuttingDown).await;
                break;
            }
        }
    }

    // Wait for the child process to finish
    log::debug!("[DEBUG] stopping stdout and stderr for process {process_id}");

    let exit_reason = match wait_handle.await {
        Ok(reason) => reason,
        Err(_) => ExitReason::KilledByProcessApi,
    };
    log::debug!("[DEBUG] Finished waiting for wait_for_child_to_exit");

    // Send exit status message
    let elapsed = proc_handle::format_exit_reason(pid, &exit_reason, 0.0);
    match &exit_reason {
        ExitReason::Exited { status } => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessExited {
                    status: *status,
                    details: elapsed,
                },
            )
            .await;
        }
        ExitReason::TimedOut { timeout_secs } => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessTimedOut {
                    timeout_secs: *timeout_secs,
                    details: elapsed,
                },
            )
            .await;
        }
        ExitReason::OutOfMemory { limit_bytes } => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessOutOfMemory {
                    limit_bytes: *limit_bytes,
                    details: elapsed,
                },
            )
            .await;
        }
        ExitReason::ContainerOom { limit_bytes } => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ContainerOutOfMemory {
                    limit_bytes: *limit_bytes,
                    details: elapsed,
                },
            )
            .await;
        }
        _ => {
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessExited {
                    status: -1,
                    details: elapsed,
                },
            )
            .await;
        }
    }

    // Clean up process from map
    log::debug!(
        "[DEBUG] Handling process cleanup for {process_id}, pid: {pid}"
    );

    let reattachable = {
        let map = proc_map.lock();
        map.get(&process_id)
            .map(|e| e.reattachable)
            .unwrap_or(false)
    };

    if reattachable {
        log::debug!("[DEBUG] Detaching process: {process_id}");
        if let Err(e) = state::detach_process(&proc_map, &process_id) {
            log::error!("[ERROR] Failed to detach process {process_id}: {e}");
        } else {
            log::debug!("[DEBUG] Successfully detached process {process_id}");
        }
        log::debug!(
            "[DEBUG] Reattachable process {process_id} is done (stdout/stderr closed, process exited), dropping handle"
        );
    } else {
        log::debug!(
            "[DEBUG] Non-reattachable process, killing and removing from map: {process_id}"
        );
        proc_handle::kill_and_wait(pid, cgroup_path.as_ref()).await;
        state::remove_process(&proc_map, &process_id);
    }

    log::debug!(
        "[DEBUG] Finished WebSocket message processing for process {process_id}"
    );
    log::debug!("[DEBUG] After cleaning up process {process_id}, proc_map: {}", state::debug_process_map(&proc_map));
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
    ws_tx: WsTx,
    _ws_rx: futures::stream::SplitStream<WebSocketStream<TcpStream>>,
    proc_map: ProcessMap,
    container_name: Option<String>,
) {
    let process_id = &req.process_id;
    log::debug!("[DEBUG] Processing reattach request for process_id: {process_id}");

    // Check container name if expected
    if let Some(ref expected) = req.expected_container_name {
        if let Some(ref actual) = container_name {
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
        Ok(crate::state::ProcessState::Attached) => {
            log::debug!("[DEBUG] Process already attached: {process_id}");
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::ProcessAlreadyAttached {
                    process_id: process_id.clone(),
                },
            )
            .await;
        }
        Ok(crate::state::ProcessState::Detached) => {
            let pid = {
                let map = proc_map.lock();
                map.get(process_id).map(|e| e.pid).unwrap_or(0)
            };
            log::debug!(
                "[DEBUG] Reattaching to detached process: {process_id} with PID {pid}"
            );

            if let Err(e) = state::attach_process(&proc_map, process_id) {
                let _ = send_msg(
                    &ws_tx,
                    &ServerMessage::InfraError { error: e },
                )
                .await;
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
        Ok(crate::state::ProcessState::Done) => {
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
async fn handle_send_signal(pid: u32, signal_str: &str, ws_tx: &WsTx) {
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
