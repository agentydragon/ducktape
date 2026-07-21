//! Claimed-work wire envelope and backend payload decoding.
//!
//! The envelope must deserialize independently of the backend payload. Once the console has
//! committed a claim, `execution_id` and `lease_token` are the daemon's only way to report a
//! malformed or incompatible payload as a definitive failed result instead of abandoning its
//! lease.

use serde::Deserialize;
use serde_json::Value;

#[derive(Deserialize)]
pub struct ClaimedExecution {
    pub execution_id: String,
    pub backend: String,
    pub payload: Value,
    pub lease_token: String,
    pub lease_expires_at: String,
}

#[derive(Debug, Deserialize)]
pub struct RunRequest {
    pub token: String,
    pub run_as: String,
    pub cmd: String,
    pub cwd: Option<String>,
    pub max_bytes: usize,
    pub timeout_ms: u64,
}

pub fn decode_run_request(payload: Value) -> Result<RunRequest, serde_json::Error> {
    serde_json::from_value(payload)
}
