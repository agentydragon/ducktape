//! Reverse-engineered from process_api BuildID edebff2c28de76238c95c299ba3401a9098c9e17
//! release process_api_2026-05-11-18-55
//!
//! WebSocket message handler, serde structs for the CreateProcess/ProcessConnection
//! protocol, JWT authentication, and stdin/stdout/stderr forwarding over WebSocket.
//!
//! Offsets below from 91c789ff (NOT re-verified against edebff2c) except where
//! an edebff2c address is named explicitly.
//! String refs and serde fields verified against edebff2c.
//!
//! edebff2c function addresses (from call-target boundaries in `objdump -d`):
//!   handle_ws:                   0x1428f0..0x1492d0
//!     Owns the whole connection: accept, JWT-or-JSON dispatch, capability
//!     negotiation, per-stream zstd encoder construction (0x147d45 stdout,
//!     0x1487a6 stderr), cleanup.
//!   process_ws_message:          0x14bc40..0x153610
//!     WS message loop; also holds the inlined inbound zstd decoder
//!     (0x14bee3..0x14bff9).
//!   stderr pipe_to_ws:           0x4a1f0..0x4bb60   ("[DEBUG] started stderr pipe")
//!   stdout pipe_to_ws:           0x4c310..0x4dc80
//!   ProcessConnection deser:     0x15cc30..0x15e400
//!   CreateProcess deser:         0x186c00..0x188e70
//!   ServerMessage JSON encoder:  0x1856e0..0x185d00 (string escaper 0x1850c0)
//!
//! Previously verified against 91c789ff (objdump + strings -t x cross-referencing):
//!
//!   handle_ws_connection:        0x135c00..0x13c279  (26234 bytes)
//!     Monolithic async fn: WS accept, JWT-or-JSON first-byte dispatch,
//!     CreateProcess handling, ProcessConnection/reattach handling, cleanup.
//!     JWT flow at 0x136c00..0x137800 (inlined). First-byte dispatch at 0x13704f:
//!       0x7b ('{') -> JSON path (CreateProcess or ProcessConnection)
//!       0x65 ('e') -> JWT path (eyJ... token, then second message for CreateProcess)
//!       else -> error "expected '{' (JSON) or 'e' (JWT)"
//!   process_ws_message:          0x13fb00..0x144889  (19849 bytes)
//!     Main WS message processing loop (select on ws_rx, exit_status, shutdown).
//!   stderr_pipe_handler:         0x461d0..0x47404   (4660 bytes)
//!   stdout_pipe_handler:         0x47db0..0x48fee   (4670 bytes)
//!   create_process_deser:        0x23270..0x240da   (3562 bytes)
//!   create_process_visitor:      0x170740..0x170757  (24 bytes)
//!   process_connection_visitor:   0x1707a0..0x1707b7  (24 bytes)
//!
//! String refs (JWT authentication flow, at 0x2a4542..0x2a45ff):
//!   "auth_public_key"
//!   "Empty first message"
//!   "[DEBUG] Received ProcessConnection JSON (no JWT)"
//!   "[DEBUG] Received JWT token, verifying..."
//!   "[DEBUG] No auth public key loaded, accepting JWT without verification"
//!   "Client closed connection"
//!   "Client closed connection after JWT"
//!   "First message should be text json CreateProcess"
//!   "Second message after JWT should be text json CreateProcess"
//!
//! String refs (JWT validation errors, at 0x295300..0x295900):
//!   "Invalid JWT claims: "
//!   "Invalid JWT signature"
//!   "JWT token has expired"
//!   "JWT key error: "
//!   "JWT decode error: "
//!   "JWT authentication failed: "
//!   "[DEBUG] JWT verified successfully: sub='"
//!   "[DEBUG] JWT verification failed: "
//!   "[WARN] Failed to load auth key: "
//!   "Unexpected first byte '...' in first message"
//!   "expected '{' (JSON) or 'e' (JWT)"
//!
//! String refs (io.rs debug/error strings, at 0x295800..0x296d00):
//!   "[DEBUG] New WebSocket connection from"
//!   "[DEBUG] Received process connection request:"
//!   "[DEBUG] process_ws_message returned:"
//!   "[DEBUG] process_ws_message failed:"
//!   "[DEBUG] Failed to get first message:"
//!   "[DEBUG] Failed to get ProcessConnection after JWT:"
//!   "[SECURITY] Rejected WebSocket connection from local IP"
//!
//! String refs (io.rs debug/error strings, at 0x2a4400..0x2a4900):
//!   "[DEBUG] Container name mismatch: expected '', actual ''"
//!   "[DEBUG] Adding process  to cgroup"
//!   "[DEBUG] Process with same ID already running:"
//!   "[DEBUG] Processing reattach request for process_id:"
//!   "[DEBUG] Process not found:"
//!   "[DEBUG] Reattaching to detached process:  with PID"
//!   "[DEBUG] Process already attached:"
//!   "[DEBUG] Restoring cgroup ownership for process id  process group PID"
//!   "[DEBUG] Successfully started stream for process"
//!   "[DEBUG] stopping stdout and stderr for process"
//!   "[DEBUG] Handling process cleanup for , pid:"
//!   "[DEBUG] Detaching process:"
//!   "[DEBUG] Reattachable process  is done, dropping handle"
//!   "[DEBUG] Non-reattachable process, killing and removing from map:"
//!   "stop_waiting_tx is already taken"
//!   "exit_status_rx is already taken"
//!   " killed by process_api, removing from map"
//!
//! String refs (stdout/stdin/ws messages, at 0x2a44d0..0x2a4900):
//!   "[DEBUG] started stdout pipe"          (0x2a4512)
//!   "[DEBUG] stdout done"                  (0x2a452e)
//!   "[DEBUG] started stderr pipe"          (0x2a44d1)
//!   "[DEBUG] stderr done"                  (0x2a44fe)
//!   "[DEBUG] stdout/stderr EOF"
//!   "[DEBUG] forward_stdin: Starting stdin forwarding for process"  (0x295b55)
//!   "[DEBUG] process_ws_message: Starting WebSocket message processing for process"  (0x295e87)
//!   "[DEBUG] Finished WebSocket message processing for process"  (0x2a4868)
//!
//! Server->Client message variant strings (edebff2c: interned run at
//! 0x39a8b3..0x39aa3e, exact order as laid out in the binary):
//!   ConnectionCapabilities(supports_trace, supports_zstd), ProcessCreated,
//!   AttachedToProcess, AttachedToProcessV2, ProcessNotRunning,
//!   ProcessAlreadyAttached, FailedToStartProcess, WithSameIdRunning,
//!   InfraError, ExpectStdOut, StdOutEOF, ExpectStdErr, StdErrEOF,
//!   ProcessExited, ProcessTimedOut, ProcessCpuTimedOut, ProcessOutOfMemory,
//!   ContainerOutOfMemory, InvalidSignal, FailedToSendSignal, SignalSent,
//!   ShuttingDown, SendSignal, ExpectStdIn, TraceEvent
//!
//! Server->Client message variant strings (at 0x2a4cb7..0x2a4d00):
//!   ConnectionCapabilities(supports_trace), ProcessCreated, AttachedToProcess,
//!   AttachedToProcessV2, ProcessNotRunning, ProcessAlreadyAttached,
//!   FailedToStartProcess, WithSameIdRunning, InfraError,
//!   ExpectStdOut, StdOutEOF, ExpectStdErr, StdErrEOF,
//!   ProcessExited, ProcessTimedOut, ProcessOutOfMemory,
//!   ContainerOutOfMemory, InvalidSignal, FailedToSendSignal,
//!   SignalSent, ShuttingDown
//!
//! Server->Client message variant strings (at 0x2a5bb7..0x2a5c00):
//!   AlreadyClosed, IoWriteBufferFull, AttackAttemptUrl, HttpFormatIpSocket
//!
//! Server->Client message variant strings (at 0x2a64c8):
//!   KeepAlive, Closed
//!
//! Server->Client message variant strings (at 0x287aa0):
//!   ProcessCreatedV2
//!
//! Client->Server message variant strings (at 0x2a4e04):
//!   SendSignal, ExpectStdIn, TraceEvent
//!
//! New serde structs (Binary: 91c789ff):
//!   "struct TraceEventMsg with 5 elements" — fields: process, host, sph, cat, dur_us
//!   "struct ProcessConnection with 4 elements" — added want_trace_events field
//!   "struct TokenClaims with 3 elements"
//!   "struct ClaimsForValidation with 5 elements"
//!
//! Event type:
//!   container_shutdown

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::{SinkExt, StreamExt};
use nix::sys::signal::Signal;
use nix::unistd::Pid;
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
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
// Serde structs -- Verified at 0x23270..0x240da and 0x170740/0x1707a0
// ---------------------------------------------------------------------------

/// Deserializer at edebff2c 0x186c00..0x188e70.
/// Xrefs: "struct CreateProcess with 11 elements" (0x39a02b), "name", "args",
///   "clear_env", "uid", "gid", "timeout", "cpu_timeout", "reattachable",
///   "allow_process_id_reuse", "memory_limit_bytes", "env_vars"
///
/// Binary: edebff2c raised the element count 10 -> 11 by adding `cpu_timeout`.
/// The field-name compare is at 0x186fa5..0x186fc6:
///   `movabs $0x656d69745f757063,%rcx` ("cpu_time")
///   `movabs $0x74756f656d69745f,%rdx` ("_timeout")
/// i.e. an 11-byte overlapping compare against "cpu_timeout", matched
/// immediately before "reattachable" (0x6863617474616572 at 0x18708a).
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
    /// Wall-clock timeout, in seconds.
    #[serde(default)]
    pub timeout: Option<u64>,
    /// Binary: edebff2c — CPU-time budget in seconds, enforced from the
    /// process cgroup's `cpu.stat` `usage_usec` counter (see
    /// `proc_handle::read_cpu_usage_usec`). Independent of `timeout`, which
    /// stays wall-clock.
    #[serde(default)]
    pub cpu_timeout: Option<u64>,
    #[serde(default)]
    pub memory_limit_bytes: Option<u64>,
}

/// Deserializer at edebff2c 0x15cc30..0x15e400.
/// Xrefs: "struct ProcessConnection with 5 elements" (0x39a074); field-name
///   literals loaded at 0x15ddd5 ("process_id", len 10), 0x15dd9f
///   ("expected_container_name", len 23), 0x15ddf2 ("want_trace_events",
///   len 17), 0x15de0f ("accept_zstd", len 11).
/// Binary: edebff2c raised the element count 4 -> 5 by adding `accept_zstd`.
#[derive(Debug, Deserialize)]
pub struct ProcessConnection {
    pub process_id: String,
    #[serde(default)]
    pub reattach: Option<bool>,
    #[serde(default)]
    pub expected_container_name: Option<String>,
    /// Binary: 91c789ff — request trace events for this connection.
    #[serde(default)]
    pub want_trace_events: Option<bool>,
    /// Binary: edebff2c — client can decode zstd-compressed binary frames.
    /// When set, `handle_ws` builds a `ws_compression::StreamEncoder` per
    /// output stream (0x147d45 / 0x1487a6) and answers with
    /// `ConnectionCapabilities { supports_zstd: true, .. }`.
    #[serde(default)]
    pub accept_zstd: Option<bool>,
}

/// Trace event message (used in both client->server and server->client TraceEvent).
/// Xrefs: "struct TraceEventMsg with 5 elements"
/// Fields from string block at 0x2a48d6: process, host, sph, cat, dur_us
#[derive(Debug, Deserialize, Serialize)]
pub struct TraceEventMsg {
    pub process: String,
    pub host: String,
    pub sph: String,
    pub cat: String,
    pub dur_us: u64,
}

/// Client->Server WebSocket message.
/// Variant strings at 0x2a4e04: "SendSignalExpectStdInTraceEvent"
/// Binary: 91c789ff adds TraceEvent variant (carries TraceEventMsg payload).
#[derive(Debug, Deserialize)]
#[serde(tag = "type")]
pub enum ClientMessage {
    SendSignal {
        signal: String,
    },
    ExpectStdIn,
    StdInEOF,
    /// Binary: 91c789ff — client sends trace events for profiling.
    TraceEvent(TraceEventMsg),
}

/// Server->Client WebSocket message type tags.
/// Variant strings at 0x2a4cb7 (ConnectionCapabilities..ShuttingDown),
///   0x2a5bb7 (AlreadyClosed..HttpFormatIpSocket),
///   0x2a64c8 (KeepAlive, Closed), 0x287aa0 (ProcessCreatedV2).
/// Binary: 91c789ff adds: ConnectionCapabilities, AttachedToProcessV2,
///   ProcessCreatedV2, KeepAlive, Closed, AlreadyClosed, IoWriteBufferFull,
///   AttackAttemptUrl, HttpFormatIpSocket, TraceEvent.
#[derive(Debug, Serialize)]
#[serde(tag = "type")]
pub enum ServerMessage {
    /// Binary: 91c789ff — reports whether trace events are supported.
    /// edebff2c adds `supports_zstd`.
    ///
    /// Serializer decompiled from 0x185440..0x1854ff: the encoder emits
    /// `"supports_trace":` then `true`/`false` (0x1854ca writes the literal
    /// `true`, 0x185459 writes `fals`+`e`), a `,`, then `"supports_zstd"`
    /// (string 0x39a8d7, len 0xd loaded at 0x185480) and its bool.
    ConnectionCapabilities {
        supports_trace: bool,
        supports_zstd: bool,
    },
    ProcessCreated {
        process_id: String,
        pid: u32,
    },
    /// Binary: 91c789ff — extended version of ProcessCreated.
    /// String at 0x287aa0.
    ProcessCreatedV2 {
        process_id: String,
        pid: u32,
    },
    AttachedToProcess {
        process_id: String,
        pid: u32,
    },
    /// Binary: 91c789ff — extended version of AttachedToProcess.
    /// String at 0x2a4cfa.
    AttachedToProcessV2 {
        process_id: String,
        pid: u32,
    },
    ProcessNotRunning {
        process_id: String,
    },
    ProcessAlreadyAttached {
        process_id: String,
    },
    FailedToStartProcess {
        error: String,
    },
    WithSameIdRunning {
        process_id: String,
    },
    InfraError {
        error: String,
    },
    ExpectStdOut,
    StdOutEOF,
    ExpectStdErr,
    StdErrEOF,
    ProcessExited {
        status: i32,
        details: String,
    },
    ProcessTimedOut {
        timeout_secs: u64,
        details: String,
    },
    /// Binary: edebff2c — the process exceeded its `cpu_timeout` CPU budget.
    /// Variant tag "ProcessCpuTimedOut" at 0x39a9b2 (loaded at 0x1858e2 in the
    /// ServerMessage encoder and 0x1324f2). The `cpu_timeout_secs` key is the
    /// 16-byte string at 0x37bd20, emitted at 0x150c48.
    ProcessCpuTimedOut {
        cpu_timeout_secs: u64,
        details: String,
    },
    ProcessOutOfMemory {
        limit_bytes: u64,
        details: String,
    },
    ContainerOutOfMemory {
        limit_bytes: u64,
        details: String,
    },
    InvalidSignal {
        signal: String,
    },
    FailedToSendSignal {
        error: String,
    },
    SignalSent {
        signal: String,
    },
    ShuttingDown,
    /// Binary: 91c789ff — server echoes trace events back.
    TraceEvent(TraceEventMsg),
    /// Binary: 91c789ff — periodic keepalive sent to idle connections.
    KeepAlive,
    /// Binary: 91c789ff — connection was closed by the remote side.
    Closed,
    /// Binary: 91c789ff — attempted operation on already-closed connection.
    AlreadyClosed,
    /// Binary: 91c789ff — write buffer full; client is too slow consuming output.
    IoWriteBufferFull,
    /// Binary: 91c789ff — request URL looks like an attack attempt.
    AttackAttemptUrl {
        url: String,
    },
    /// Binary: 91c789ff — HTTP request used IP:port socket format.
    HttpFormatIpSocket,
}

/// Container-level event types sent out-of-band.
/// Binary: 91c789ff — "container_shutdown" event type added.
#[derive(Debug, Serialize)]
#[serde(tag = "event")]
pub enum ContainerEvent {
    /// Binary: 91c789ff — broadcast when the container is shutting down.
    #[serde(rename = "container_shutdown")]
    ContainerShutdown,
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

/// Type-erased WebSocket sink. Allows `WsStreamHandle` to work with any
/// underlying transport (TcpStream, UnixStream, vsock, etc.).
type DynWsSink =
    Box<dyn futures::Sink<Message, Error = tokio_tungstenite::tungstenite::Error> + Unpin + Send>;

/// Per-connection state wrapping WebSocket sender + process state.
/// Fields from disassembly: process_info, proc_handle, controller,
///   stop_waiting_rx, stop_waiting_tx, exit_status_rx, exit_status_tx,
///   oom_killed_rx, want_trace_events
#[derive(Clone)]
pub struct WsStreamHandle {
    tx: Arc<TokioMutex<DynWsSink>>,
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

/// Type-erased WebSocket receive stream.
type DynWsRx = Box<
    dyn futures::Stream<Item = Result<Message, tokio_tungstenite::tungstenite::Error>>
        + Unpin
        + Send,
>;

/// Main WebSocket message processing loop for a process.
/// Binary: 0x13fb00..0x144889  (19849 bytes)
///
/// String refs at 0x295e87 and 0x2a4604..0x2a4868:
///   "[DEBUG] process_ws_message: Starting WebSocket message processing"
///   "process_ws_message: Shutting down"
///   "process_ws_message: Timeout"
///   "process_ws_message: OOM"
///   "process_ws_message: Container OOM"
///   "failed to read message"
///   "Expected binary message after ExpectStdIn"
///   "[DEBUG] Finished WebSocket message processing for process"
#[allow(clippy::too_many_arguments)] // Matches binary's function signature
async fn process_ws_message(
    ws_tx: &WsStreamHandle,
    mut ws_rx: DynWsRx,
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
                        match serde_json::from_str::<ClientMessage>(&text) {
                            Ok(client_msg) => match client_msg {
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
                                ClientMessage::TraceEvent(_trace) => {
                                    // TraceEvent messages from the client are
                                    // received but not processed server-side.
                                }
                            },
                            Err(e) => {
                                // Binary: 810fd3a4
                                log::debug!("[DEBUG] bad control msg from ws: {text}");
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
                    // Binary: edebff2c — new arm. Tag string "ProcessCpuTimedOut"
                    // (0x39a9b2), payload key "cpu_timeout_secs" (0x37bd20,
                    // emitted at 0x150c48).
                    ExitReason::CpuTimedOut { cpu_timeout_secs } => {
                        send_msg(ws_tx, &ServerMessage::ProcessCpuTimedOut {
                            cpu_timeout_secs: *cpu_timeout_secs,
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
                    // Binary: edebff2c — "process_ws_message: CpuTimeout"
                    // (interned run at 0x399d98, immediately after
                    // "process_ws_message: Timeout").
                    ExitReason::CpuTimedOut { .. } => {
                        Ok("process_ws_message: CpuTimeout".to_string())
                    }
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
/// Binary: 0x135c00..0x13c279  (26234 bytes) — monolithic async fn.
///
/// The first-byte dispatch at 0x13704f determines the protocol path:
///   '{' (0x7b) -> JSON: parse as CreateProcess or ProcessConnection directly.
///   'e' (0x65) -> JWT: validate token, then read a SECOND message for CreateProcess.
///   other     -> error "expected '{' (JSON) or 'e' (JWT)"
///
/// JWT flow (inlined at 0x136c00..0x137800):
///   1. Read first WS text message
///   2. Inspect first byte: '{' or 'e'
///   3. If 'e' (JWT path):
///      a. "[DEBUG] Received JWT token, verifying..."
///      b. If no auth_public_key loaded:
///         "[DEBUG] No auth public key loaded, accepting JWT without verification"
///      c. Else verify signature, check expiry, validate claims
///      d. "[DEBUG] JWT verified successfully: sub='...'" or
///         "[DEBUG] JWT verification failed: ..."
///      e. Read SECOND message from WebSocket
///      f. If client closed: "Client closed connection after JWT"
///      g. Parse second message: "Second message after JWT should be text json CreateProcess"
///   4. If '{' (JSON path):
///      a. Parse as ProcessConnection -> "[DEBUG] Received ProcessConnection JSON (no JWT)"
///      b. Or parse as CreateProcess
///
/// String refs at 0x295f46, 0x2a4542..0x2a46a7:
///   "[DEBUG] New WebSocket connection from"
///   "Empty first message"
///   "[DEBUG] Received ProcessConnection JSON (no JWT)"
///   "[DEBUG] Received JWT token, verifying..."
///   "[DEBUG] No auth public key loaded, accepting JWT without verification"
///   "Client closed connection"
///   "Client closed connection after JWT"
///   "First message should be text json CreateProcess"
///   "Second message after JWT should be text json CreateProcess"
///   "[DEBUG] Failed to get ProcessConnection after JWT:"
///   "[SECURITY] Rejected WebSocket connection from local IP"
///   "expected '{' (JSON) or 'e' (JWT)"
#[allow(clippy::too_many_arguments)] // Matches binary's function signature
pub async fn handle_ws_connection<S>(
    ws_stream: WebSocketStream<S>,
    remote_addr: std::net::SocketAddr,
    proc_map: ProcessMap,
    controller: CgroupController,
    container_memory_limit: Option<u64>,
    oom_polling_period: Duration,
    container_name: Arc<Mutex<Option<String>>>,
    shutdown_tx: broadcast::Sender<()>,
    oom_channels: OomChannelMap,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    log::debug!("[DEBUG] New WebSocket connection from {remote_addr}");

    let (ws_sink, mut ws_rx) = ws_stream.split();
    let ws_tx = WsStreamHandle {
        tx: Arc::new(TokioMutex::new(Box::new(ws_sink) as DynWsSink)),
        process_info: None,
        controller: None,
    };

    // Read first message — may be JSON (CreateProcess/ProcessConnection) or JWT token
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

    if first_msg.is_empty() {
        log::debug!("Empty first message");
        return;
    }

    // First-byte dispatch: '{' = JSON, 'e' = JWT token (eyJ...).
    // Binary: 0x13704f: cmp $0x65,%eax; je JWT_PATH; cmp $0x7b,%eax; je JSON_PATH
    let first_byte = first_msg.as_bytes()[0];
    let (first_json, _want_trace) = match first_byte {
        b'{' => {
            // JSON path — direct CreateProcess or ProcessConnection
            log::debug!("[DEBUG] Received ProcessConnection JSON (no JWT)");
            (first_msg, false)
        }
        b'e' => {
            // JWT path — token starts with "eyJ..."
            log::debug!("[DEBUG] Received JWT token, verifying...");
            // JWT verification would happen here (calls into jwt module).
            // If no auth_public_key is loaded, JWT is accepted without verification:
            //   "[DEBUG] No auth public key loaded, accepting JWT without verification"
            // On success: "[DEBUG] JWT verified successfully: sub='...'"
            // On failure: "[DEBUG] JWT verification failed: ..."
            //             "JWT authentication failed: ..."
            //
            // After JWT, read the SECOND message which must be CreateProcess JSON.
            let second_msg = match ws_rx.next().await {
                Some(Ok(msg)) if msg.is_text() => msg.into_text().unwrap_or_default(),
                Some(Ok(_)) => {
                    let _ = send_msg(
                        &ws_tx,
                        &ServerMessage::InfraError {
                            error: "Second message after JWT should be text json CreateProcess"
                                .to_string(),
                        },
                    )
                    .await;
                    return;
                }
                _ => {
                    log::debug!("Client closed connection after JWT");
                    return;
                }
            };
            (second_msg, true)
        }
        _ => {
            log::debug!(
                "[DEBUG] Unexpected first byte '{}' in first message",
                first_byte as char
            );
            let _ = send_msg(
                &ws_tx,
                &ServerMessage::InfraError {
                    error: format!(
                        "Unexpected first byte '{}': expected '{{' (JSON) or 'e' (JWT)",
                        first_byte as char
                    ),
                },
            )
            .await;
            return;
        }
    };

    // Parse the JSON message (either the first message or the second after JWT)
    let first: FirstMessage = match serde_json::from_str(&first_json) {
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

    let ws_rx: DynWsRx = Box::new(ws_rx);

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
/// Binary: inlined into handle_ws_connection (0x135c00..0x13c279), not a separate function.
///
/// String refs:
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
    ws_rx: DynWsRx,
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

    // Validate process_id
    // Binary: 810fd3a4 — validation strings: "process_id cannot be empty",
    //   "process_id too long (", "process_id cannot contain control characters",
    //   "process_id cannot contain the trace marker"
    if process_id.is_empty() {
        log::debug!("[DEBUG] Error starting process: process_id cannot be empty");
        let _ = send_msg(
            &ws_tx,
            &ServerMessage::FailedToStartProcess {
                error: "process_id cannot be empty".to_string(),
            },
        )
        .await;
        return;
    }
    if process_id.len() > 4096 {
        log::debug!(
            "[DEBUG] Error starting process: process_id too long ({})",
            process_id.len()
        );
        let _ = send_msg(
            &ws_tx,
            &ServerMessage::FailedToStartProcess {
                error: format!("process_id too long ({})", process_id.len()),
            },
        )
        .await;
        return;
    }
    if process_id.chars().any(|c| c.is_control()) {
        log::debug!("[DEBUG] Error starting process: process_id cannot contain control characters");
        let _ = send_msg(
            &ws_tx,
            &ServerMessage::FailedToStartProcess {
                error: "process_id cannot contain control characters".to_string(),
            },
        )
        .await;
        return;
    }
    if process_id.contains("##TRACE##") {
        log::debug!("[DEBUG] Error starting process: process_id cannot contain the trace marker");
        let _ = send_msg(
            &ws_tx,
            &ServerMessage::FailedToStartProcess {
                error: "process_id cannot contain the trace marker".to_string(),
            },
        )
        .await;
        return;
    }

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
    // Binary: edebff2c — CreateProcess.cpu_timeout, in seconds.
    let cpu_timeout = req.cpu_timeout.map(Duration::from_secs);
    let reattachable = req.reattachable.unwrap_or(false);
    let mut handle = ProcHandle::new(
        pid,
        reattachable,
        timeout,
        cpu_timeout,
        req.memory_limit_bytes,
    );
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
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let process_info = ProcessInfo {
        process_id: process_id.clone(),
        pid,
        reattachable,
        timeout: req.timeout,
        cpu_timeout: req.cpu_timeout,
        memory_limit_bytes: req.memory_limit_bytes,
        start_time: now.as_secs(),
        start_wallclock_micros: now.as_micros() as u64,
        cmd_summary: req.name.clone(),
        stdin_bytes: 0,
        stdout_bytes: 0,
        stderr_bytes: 0,
        trace_emitted: false,
        trace_outcome: None,
    };
    let cgroup_config = cgroup_path.as_ref().map(|cp| proc_handle::Cgroup {
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
        handle_cpu_timeout,
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
            entry.proc_handle.cpu_timeout,
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
        process_id.clone(),
        handle_timeout,
        handle_cpu_timeout,
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
    // Binary: 0x47db0..0x48fee  (4670 bytes)
    // Xrefs: "[DEBUG] started stdout pipe" (0x2a4512), "[DEBUG] stdout done" (0x2a452e)
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
    // Binary: 0x461d0..0x47404  (4660 bytes)
    // Xrefs: "[DEBUG] started stderr pipe" (0x2a44d1), "[DEBUG] stderr done" (0x2a44fe)
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
/// Binary: inlined into handle_ws_connection (0x135c00..0x13c279).
///
/// String refs at 0x2959dd, 0x295956, 0x2959a1, 0x29590d:
///   "[DEBUG] Processing reattach request for process_id:"
///   "[DEBUG] Process not found:"
///   "[DEBUG] Reattaching to detached process: with PID"
///   "[DEBUG] Process already attached:"
///   "[DEBUG] Received process connection request:"
async fn handle_process_connection(
    req: ProcessConnection,
    ws_tx: WsStreamHandle,
    _ws_rx: DynWsRx,
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
/// Binary: inlined into process_ws_message (0x13fb00..0x144889).
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
