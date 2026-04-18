//! Client-side daemon lifecycle management.
//!
//! Ports `devinfra/claude/hook_daemon/client.py::_ensure_daemon`. Provides:
//!
//!   - **Pidfile flock probe** — non-blocking `flock(LOCK_EX|LOCK_NB)` to
//!     detect whether a daemon process holds the pidfile lock (PID-reuse safe).
//!   - **Cross-process startup lock** (`daemon.lock`) — prevents concurrent
//!     clients from racing to start multiple daemons.
//!   - **Kill stale daemon** — SIGTERM → 500ms grace → SIGKILL → 10s reap.
//!   - **Circuit breaker** — file-based exponential backoff on repeated startup
//!     failures, cleared on success.
//!   - **`ensure_daemon`** — the full lifecycle orchestration.

use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use http_body_util::Empty;
use hyper::body::Bytes;
use hyper_util::rt::TokioIo;
use serde::{Deserialize, Serialize};

const STARTUP_FAILURE_FILE: &str = "startup_failure.json";
const CIRCUIT_BREAKER_BASE_SECS: u64 = 2;
const CIRCUIT_BREAKER_MAX_SECS: u64 = 120;

// ---------------------------------------------------------------------------
// Pidfile flock probe
// ---------------------------------------------------------------------------

/// Non-blocking flock probe on the pidfile. Returns `true` if a daemon holds
/// the lock (i.e., a live daemon is running).
pub fn is_pidfile_locked(pidfile: &Path) -> bool {
    let fd = match std::fs::File::open(pidfile) {
        Ok(f) => f,
        Err(_) => return false,
    };
    let raw = fd.as_raw_fd();
    let rc = unsafe { libc::flock(raw, libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        unsafe { libc::flock(raw, libc::LOCK_UN) };
        false
    } else {
        true
    }
}

// ---------------------------------------------------------------------------
// Cross-process startup lock (daemon.lock)
// ---------------------------------------------------------------------------

/// RAII guard that holds an exclusive flock on `daemon.lock`. Released on drop.
pub struct DaemonLockGuard {
    _fd: std::fs::File,
}

impl Drop for DaemonLockGuard {
    fn drop(&mut self) {
        unsafe { libc::flock(self._fd.as_raw_fd(), libc::LOCK_UN) };
    }
}

/// Acquire an exclusive blocking flock on `<daemon_dir>/daemon.lock`.
pub fn acquire_daemon_lock(daemon_dir: &Path) -> DaemonLockGuard {
    let fd = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(daemon_dir.join("daemon.lock"))
        .expect("open daemon.lock");
    let rc = unsafe { libc::flock(fd.as_raw_fd(), libc::LOCK_EX) };
    assert!(
        rc == 0,
        "flock daemon.lock failed: {}",
        std::io::Error::last_os_error()
    );
    DaemonLockGuard { _fd: fd }
}

// ---------------------------------------------------------------------------
// Kill stale daemon
// ---------------------------------------------------------------------------

fn read_pidfile(pidfile: &Path) -> Option<i32> {
    std::fs::read_to_string(pidfile).ok()?.trim().parse().ok()
}

/// Kill the daemon identified by pidfile: SIGTERM, 500ms grace, then SIGKILL.
pub fn kill_daemon_by_pidfile(pidfile: &Path) {
    let Some(pid) = read_pidfile(pidfile) else {
        return;
    };
    let nix_pid = nix::unistd::Pid::from_raw(pid);

    if nix::sys::signal::kill(nix_pid, nix::sys::signal::Signal::SIGTERM).is_err() {
        return;
    }

    std::thread::sleep(Duration::from_millis(500));

    if let Err(e) = nix::sys::signal::kill(nix_pid, nix::sys::signal::Signal::SIGKILL) {
        eprintln!("lifecycle: SIGKILL pid={pid} failed: {e}");
    }

    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if nix::sys::signal::kill(nix_pid, None).is_err() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    eprintln!("lifecycle: pid={pid} still alive after SIGTERM+SIGKILL");
}

// ---------------------------------------------------------------------------
// Circuit breaker
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize)]
struct StartupFailure {
    consecutive_failures: u32,
    last_failure_epoch: u64,
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

fn read_startup_failure(daemon_dir: &Path) -> Option<StartupFailure> {
    let path = daemon_dir.join(STARTUP_FAILURE_FILE);
    let data = match std::fs::read(&path) {
        Ok(d) => d,
        Err(_) => return None,
    };
    match serde_json::from_slice(&data) {
        Ok(f) => Some(f),
        Err(_) => {
            if let Err(e) = std::fs::remove_file(&path) {
                eprintln!(
                    "lifecycle: failed to remove corrupt {}: {e}",
                    path.display()
                );
            }
            None
        }
    }
}

/// Returns `Err` with a message if the circuit breaker is open (cooldown not elapsed).
pub fn check_circuit_breaker(daemon_dir: &Path) -> Result<(), String> {
    let Some(failure) = read_startup_failure(daemon_dir) else {
        return Ok(());
    };
    let backoff_factor = 1u64
        .checked_shl(failure.consecutive_failures)
        .unwrap_or(u64::MAX);
    let cooldown = CIRCUIT_BREAKER_BASE_SECS
        .saturating_mul(backoff_factor)
        .min(CIRCUIT_BREAKER_MAX_SECS);
    let elapsed = now_epoch().saturating_sub(failure.last_failure_epoch);
    if elapsed < cooldown {
        let remaining = cooldown - elapsed;
        return Err(format!(
            "Circuit breaker open: {} consecutive startup failure(s), next attempt in {remaining}s",
            failure.consecutive_failures
        ));
    }
    Ok(())
}

pub fn record_startup_failure(daemon_dir: &Path) {
    let prev = read_startup_failure(daemon_dir);
    let count = prev.map_or(1, |p| p.consecutive_failures + 1);
    let data = StartupFailure {
        consecutive_failures: count,
        last_failure_epoch: now_epoch(),
    };
    let path = daemon_dir.join(STARTUP_FAILURE_FILE);
    if let Ok(json) = serde_json::to_vec(&data) {
        if let Ok(mut tmp) = tempfile::NamedTempFile::new_in(daemon_dir) {
            use std::io::Write;
            tmp.write_all(&json).ok();
            if let Err(e) = tmp.persist(&path) {
                eprintln!("lifecycle: failed to persist {}: {e}", path.display());
            }
        }
    }
}

pub fn clear_startup_failure(daemon_dir: &Path) {
    if let Err(e) = std::fs::remove_file(daemon_dir.join(STARTUP_FAILURE_FILE)) {
        if e.kind() != std::io::ErrorKind::NotFound {
            eprintln!("lifecycle: failed to clear startup_failure.json: {e}");
        }
    }
}

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------

async fn health_check(sock_path: &Path) -> bool {
    let Ok(stream) = tokio::net::UnixStream::connect(sock_path).await else {
        return false;
    };
    let io = TokioIo::new(stream);
    let Ok((mut sender, conn)) = hyper::client::conn::http1::handshake(io).await else {
        return false;
    };
    tokio::spawn(async move {
        let _ = conn.await;
    });
    let req = hyper::Request::builder()
        .method("GET")
        .uri("/health")
        .header("host", "localhost")
        .body(Empty::<Bytes>::new());
    let Ok(req) = req else { return false };
    let Ok(resp) = tokio::time::timeout(Duration::from_secs(2), sender.send_request(req)).await
    else {
        return false;
    };
    resp.is_ok()
}

// ---------------------------------------------------------------------------
// ensure_daemon orchestration
// ---------------------------------------------------------------------------

/// Full lifecycle: lock → health → circuit breaker → flock probe → kill
/// stale → clean state → fork → wait (with crash detection) → circuit
/// breaker update → unlock.
///
/// Calls `crate::fork_daemon` and `crate::wait_for_sock` directly.
pub async fn ensure_daemon(sock_path: &Path, daemon_dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(daemon_dir).ok();
    let pidfile = daemon_dir.join("daemon.pid");

    // Fast path: healthy daemon already running (no lock needed).
    if sock_path.exists() && health_check(sock_path).await {
        return Ok(());
    }

    // Slow path: acquire daemon.lock to prevent concurrent races.
    let _lock = acquire_daemon_lock(daemon_dir);

    // Re-check after lock — another client may have won the race.
    if sock_path.exists() && health_check(sock_path).await {
        return Ok(());
    }

    check_circuit_breaker(daemon_dir)?;

    if is_pidfile_locked(&pidfile) {
        eprintln!("lifecycle: pidfile locked but daemon unhealthy — killing stale daemon");
        kill_daemon_by_pidfile(&pidfile);
    }

    // Clean stale state.
    if sock_path.exists() {
        if let Err(e) = std::fs::remove_file(sock_path) {
            eprintln!("lifecycle: failed to remove stale socket: {e}");
        }
    }
    if pidfile.exists() {
        if let Err(e) = std::fs::remove_file(&pidfile) {
            eprintln!("lifecycle: failed to remove stale pidfile: {e}");
        }
    }

    let daemon_pid = crate::fork_daemon(daemon_dir, sock_path);

    match crate::wait_for_sock(sock_path, &pidfile, daemon_pid, Duration::from_secs(10)).await {
        Ok(()) => {
            clear_startup_failure(daemon_dir);
            Ok(())
        }
        Err(e) => {
            record_startup_failure(daemon_dir);
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn circuit_breaker_noop_when_no_failures() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(check_circuit_breaker(tmp.path()).is_ok());
    }

    #[test]
    fn circuit_breaker_blocks_after_failure() {
        let tmp = tempfile::tempdir().unwrap();
        record_startup_failure(tmp.path());
        assert!(check_circuit_breaker(tmp.path()).is_err());
    }

    #[test]
    fn circuit_breaker_clears_on_success() {
        let tmp = tempfile::tempdir().unwrap();
        record_startup_failure(tmp.path());
        assert!(check_circuit_breaker(tmp.path()).is_err());
        clear_startup_failure(tmp.path());
        assert!(check_circuit_breaker(tmp.path()).is_ok());
    }

    #[test]
    fn circuit_breaker_increments_count() {
        let tmp = tempfile::tempdir().unwrap();
        record_startup_failure(tmp.path());
        record_startup_failure(tmp.path());
        let f = read_startup_failure(tmp.path()).unwrap();
        assert_eq!(f.consecutive_failures, 2);
    }

    #[test]
    fn circuit_breaker_handles_corrupt_file() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join(STARTUP_FAILURE_FILE), b"not json").unwrap();
        assert!(check_circuit_breaker(tmp.path()).is_ok());
        assert!(!tmp.path().join(STARTUP_FAILURE_FILE).exists());
    }

    #[test]
    fn circuit_breaker_allows_after_cooldown() {
        let tmp = tempfile::tempdir().unwrap();
        let data = StartupFailure {
            consecutive_failures: 1,
            last_failure_epoch: now_epoch() - 300,
        };
        let json = serde_json::to_vec(&data).unwrap();
        std::fs::write(tmp.path().join(STARTUP_FAILURE_FILE), json).unwrap();
        assert!(check_circuit_breaker(tmp.path()).is_ok());
    }

    #[test]
    fn is_pidfile_locked_returns_false_for_missing_file() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(!is_pidfile_locked(&tmp.path().join("nonexistent.pid")));
    }

    #[test]
    fn is_pidfile_locked_returns_false_for_unlocked_file() {
        let tmp = tempfile::tempdir().unwrap();
        let pidfile = tmp.path().join("daemon.pid");
        std::fs::write(&pidfile, "12345").unwrap();
        assert!(!is_pidfile_locked(&pidfile));
    }

    #[test]
    fn daemon_lock_guard_releases_on_drop() {
        let tmp = tempfile::tempdir().unwrap();
        {
            let _g = acquire_daemon_lock(tmp.path());
            // Lock held — a second non-blocking attempt should fail.
            let fd2 = std::fs::OpenOptions::new()
                .create(true)
                .truncate(false)
                .read(true)
                .write(true)
                .open(tmp.path().join("daemon.lock"))
                .unwrap();
            let rc = unsafe { libc::flock(fd2.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            assert_ne!(rc, 0, "should not acquire while guard held");
        }
        // Guard dropped — lock should be available.
        let fd3 = std::fs::OpenOptions::new()
            .read(true)
            .open(tmp.path().join("daemon.lock"))
            .unwrap();
        let rc = unsafe { libc::flock(fd3.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        assert_eq!(rc, 0, "should acquire after guard dropped");
        unsafe { libc::flock(fd3.as_raw_fd(), libc::LOCK_UN) };
    }
}
