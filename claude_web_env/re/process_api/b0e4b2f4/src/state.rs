//! Reverse-engineered from process_api release process_api_2026-02-02-04-57
//! ELF Build ID: b0e4b2f428d0472787f5b2a22fea44a58bc8fdd0
//!
//! Process map state management: attach/detach/reattach.
//!
//! Functions decompiled from:
//!   state_map_lookup:            0x1ba610..0x1bacf7  (1767 bytes)
//!   state_transition_validate:   0x1b9f30..0x1ba60d  (1757 bytes)

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::Mutex;

use crate::proc_handle::ProcHandle;

/// The attach/detach state of a managed process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcessState {
    /// Process is attached to a WebSocket connection (actively streaming I/O).
    Attached,
    /// Process is detached (reattachable, waiting for reconnect).
    Detached,
    /// Process has exited and its handle is being cleaned up.
    Done,
}

/// Entry in the process map, keyed by process_id (user-provided string).
#[derive(Debug)]
pub struct ProcessEntry {
    pub process_id: String,
    pub pid: u32,
    pub state: ProcessState,
    pub reattachable: bool,
    pub handle: ProcHandle,
}

/// Shared process map: maps process_id → ProcessEntry.
pub type ProcessMap = Arc<Mutex<HashMap<String, ProcessEntry>>>;

/// Create a new empty process map.
pub fn new_process_map() -> ProcessMap {
    Arc::new(Mutex::new(HashMap::new()))
}

/// Decompiled from 0x1ba610..0x1bacf7  (1767 bytes)
/// Xrefs: "src/state.rs is in an inconsiste...", "Process not found"
///
/// Look up a process by ID. Returns an error if the process is not found
/// or is in an inconsistent state.
pub fn lookup_process(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: &str,
) -> Result<ProcessState, String> {
    let map = proc_map.lock();
    match map.get(process_id) {
        Some(entry) => Ok(entry.state.clone()),
        None => Err(format!("Process not found: {process_id}")),
    }
}

/// Decompiled from 0x1b9f30..0x1ba60d  (1757 bytes)
/// Xrefs: "src/state.rs is in an inconsiste..."
///
/// Validate and perform a state transition. The process must be in
/// `expected_state` to transition to `new_state`.
pub fn transition_state(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: &str,
    expected_state: ProcessState,
    new_state: ProcessState,
) -> Result<(), String> {
    let mut map = proc_map.lock();
    let entry = map.get_mut(process_id).ok_or_else(|| {
        format!("Process not found: {process_id}")
    })?;

    if entry.state != expected_state {
        return Err(format!(
            "src/state.rs: {process_id} is in an inconsistent state: expected {expected_state:?}, got {:?}",
            entry.state
        ));
    }

    entry.state = new_state;
    Ok(())
}

/// Attach a process (transition from Detached → Attached).
/// Xrefs: "[DEBUG] Reattaching to detached process"
pub fn attach_process(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: &str,
) -> Result<(), String> {
    transition_state(proc_map, process_id, ProcessState::Detached, ProcessState::Attached)
}

/// Detach a process (transition from Attached → Detached).
/// Xrefs: "[DEBUG] Detaching process:", "[DEBUG] Successfully detached process"
pub fn detach_process(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: &str,
) -> Result<(), String> {
    transition_state(proc_map, process_id, ProcessState::Attached, ProcessState::Detached)
}

/// Insert a new process into the map in Attached state.
pub fn insert_process(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: String,
    pid: u32,
    reattachable: bool,
    handle: ProcHandle,
) {
    let mut map = proc_map.lock();
    map.insert(
        process_id.clone(),
        ProcessEntry {
            process_id,
            pid,
            state: ProcessState::Attached,
            reattachable,
            handle,
        },
    );
}

/// Remove a process from the map entirely.
pub fn remove_process(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: &str,
) -> Option<ProcessEntry> {
    let mut map = proc_map.lock();
    map.remove(process_id)
}

/// Check if a process with the given ID already exists and is running.
pub fn process_exists(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
    process_id: &str,
) -> bool {
    let map = proc_map.lock();
    map.contains_key(process_id)
}

/// Get the current process map state as a debug string.
/// Xrefs: "[DEBUG] Current process map:", "Currently tracked processes:"
pub fn debug_process_map(
    proc_map: &Mutex<HashMap<String, ProcessEntry>>,
) -> String {
    let map = proc_map.lock();
    let entries: Vec<String> = map
        .iter()
        .map(|(id, entry)| {
            format!("  {id}: pid={}, state={:?}, reattachable={}", entry.pid, entry.state, entry.reattachable)
        })
        .collect();
    format!("Currently tracked processes: {}\n{}", map.len(), entries.join("\n"))
}
