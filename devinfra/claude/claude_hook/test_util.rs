//! Shared test helpers for the claude_hook binary crate.
//!
//! Both `main.rs::tests` and `shim_runtime.rs::tests` exercise the same
//! shim RPC types and need similar tempdir/PATH scaffolding. The helpers
//! live here so each test module imports them instead of rolling its own.

#![cfg(test)]

use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use protocol::ShimExecRequest;

/// Tempdir with convenience methods for creating PATH-like layouts.
///
/// The owned `TempDir` is held until this fixture drops, so tests can
/// freely pass paths around without worrying about cleanup.
pub(crate) struct PathFixture {
    _tmp: tempfile::TempDir,
    pub root: PathBuf,
}

impl PathFixture {
    pub fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().to_path_buf();
        Self { _tmp: tmp, root }
    }

    /// Create `<root>/<name>` (recursively) and return its absolute path.
    pub fn mkdir(&self, name: &str) -> PathBuf {
        let p = self.root.join(name);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    /// Place an executable stub at `<dir>/<name>` (`0755`). Returns the path.
    pub fn with_exec(&self, dir: &Path, name: &str) -> PathBuf {
        let p = dir.join(name);
        std::fs::write(&p, "#!/bin/sh\nexit 0\n").unwrap();
        let mut perms = std::fs::metadata(&p).unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&p, perms).unwrap();
        p
    }

    /// Write a non-executable file at `<dir>/<name>`. Returns the path.
    pub fn with_nonexec(&self, dir: &Path, name: &str) -> PathBuf {
        let p = dir.join(name);
        std::fs::write(&p, "not a real binary").unwrap();
        p
    }

    /// Build an env map containing only `PATH=<dirs joined by ':'>`.
    pub fn env_with_path(&self, dirs: &[&Path]) -> HashMap<String, String> {
        HashMap::from([("PATH".into(), Self::join_path(dirs))])
    }

    /// Join directories with `:` to form a `PATH` string.
    pub fn join_path(dirs: &[&Path]) -> String {
        dirs.iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(":")
    }
}

/// Build a `ShimExecRequest` with test defaults, overriding shim/argv/PATH.
pub(crate) fn make_request(shim: &str, argv: &[&str], path_env: &str) -> ShimExecRequest {
    let mut env = HashMap::new();
    env.insert("PATH".into(), path_env.into());
    ShimExecRequest {
        shim: shim.into(),
        session_id: "test-session".into(),
        cwd: PathBuf::from("/tmp"),
        argv: argv.iter().map(|s| (*s).to_string()).collect(),
        pid: 1234,
        env,
    }
}
