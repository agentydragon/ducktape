//! Unix platform implementations for vsock and UDS operations.
//! Reverse-engineered from process_api BuildID edebff2c28de76238c95c299ba3401a9098c9e17
//! release process_api_2026-05-11-18-55
//! edebff2c: no application-string delta in this module; logic carried forward.
//!
//! Source path (edebff2c panic-location table): src/platform/unix/mod.rs
//!   (810fd3a4 embedded the unremapped
//!   /root/src/tree/marcus-process-api/sandboxing/sandboxing/server/process_api/ prefix)
//!
//! This module provides the platform-specific vsock and dial-UDS abstractions
//! used by main.rs and control_server.rs. The binary links tokio-vsock 0.7.2
//! for AF_VSOCK socket support (Firecracker guest-to-host communication).
//!
//! Vsock string refs from binary:
//!   "cid: ", "vsock:",
//!   "/root/.cargo/registry/src/artifactory.infra.ant.dev-7db23613d841872b/
//!    tokio-vsock-0.7.2/src/listener.rs",
//!   "/root/.cargo/registry/src/artifactory.infra.ant.dev-7db23613d841872b/
//!    tokio-vsock-0.7.2/src/stream.rs"

use tokio_vsock::{VMADDR_CID_ANY, VsockAddr, VsockListener};

/// Host CID for vsock connections. Only CID 2 (the hypervisor host) is
/// accepted; all other CIDs are rejected as a security measure.
pub const VSOCK_HOST_CID: u32 = 2;

/// Bind a vsock listener on VMADDR_CID_ANY for the given port.
/// Returns the listener or an error string for logging.
pub fn bind_vsock_listener(port: u32) -> Result<VsockListener, std::io::Error> {
    VsockListener::bind(VsockAddr::new(VMADDR_CID_ANY, port))
}

/// Check if a vsock peer CID is the host (CID 2).
pub fn is_host_cid(cid: u32) -> bool {
    cid == VSOCK_HOST_CID
}
