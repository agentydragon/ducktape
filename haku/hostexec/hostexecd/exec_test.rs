//! Exec module tests: capture, exit codes, signal death, timeout+kill, and output-cap truncation,
//! plus the JSON shape (must match `mcp_infra.exec.models.BaseExecResult`). Uses real subprocesses
//! (`/bin/sh`, `/bin/sleep`) — Linux only, like `hostexecd` itself.

use std::time::Duration;

use exec::{ExecRequest, ExitStatus, OutputStream, run_command};

fn request(argv: &[&str], timeout_ms: u64, max_bytes: usize) -> ExecRequest {
    ExecRequest {
        argv: argv.iter().map(|s| s.to_string()).collect(),
        cwd: None,
        timeout: Duration::from_millis(timeout_ms),
        max_bytes,
        credentials: None,
    }
}

#[tokio::test]
async fn captures_stdout_and_zero_exit() {
    let r = run_command(&request(&["/bin/sh", "-c", "printf hello"], 5000, 1000))
        .await
        .unwrap();
    assert_eq!(r.exit, ExitStatus::Exited { exit_code: 0 });
    assert_eq!(r.stdout, OutputStream::Full("hello".to_string()));
    assert_eq!(r.stderr, OutputStream::Full(String::new()));
}

#[tokio::test]
async fn propagates_nonzero_exit_code() {
    let r = run_command(&request(&["/bin/sh", "-c", "exit 3"], 5000, 1000))
        .await
        .unwrap();
    assert_eq!(r.exit, ExitStatus::Exited { exit_code: 3 });
}

#[tokio::test]
async fn captures_stderr() {
    let r = run_command(&request(&["/bin/sh", "-c", "printf oops 1>&2"], 5000, 1000))
        .await
        .unwrap();
    assert_eq!(r.stderr, OutputStream::Full("oops".to_string()));
}

#[tokio::test]
async fn truncates_output_over_cap() {
    // 10 bytes of output, cap 4 → first 4 stored, total 10 reported.
    let r = run_command(&request(&["/bin/sh", "-c", "printf aaaaaaaaaa"], 5000, 4))
        .await
        .unwrap();
    assert_eq!(
        r.stdout,
        OutputStream::Truncated {
            truncated_text: "aaaa".to_string(),
            total_bytes: 10
        }
    );
}

#[tokio::test]
async fn output_exactly_at_cap_is_not_truncated() {
    let r = run_command(&request(&["/bin/sh", "-c", "printf abcd"], 5000, 4))
        .await
        .unwrap();
    assert_eq!(r.stdout, OutputStream::Full("abcd".to_string()));
}

#[tokio::test]
async fn times_out_and_kills() {
    let r = run_command(&request(&["/bin/sleep", "30"], 300, 1000))
        .await
        .unwrap();
    assert_eq!(r.exit, ExitStatus::TimedOut);
}

#[tokio::test]
async fn reports_death_by_signal() {
    let r = run_command(&request(&["/bin/sh", "-c", "kill -TERM $$"], 5000, 1000))
        .await
        .unwrap();
    assert_eq!(r.exit, ExitStatus::Killed { signal: 15 });
}

// The credentials drop (setgroups/setgid/setuid) requires root — even a self-drop, since setgroups
// is privileged — so it cannot be exercised from the non-root unit-test worker; it is validated on
// a host. `users_test` covers the group *resolution* that feeds it. The `credentials: None` path
// (no drop) is covered by every other test here.

#[tokio::test]
async fn serializes_like_base_exec_result() {
    // exit is `kind`-tagged; a full stream is a bare string — matches Python BaseExecResult JSON.
    let r = run_command(&request(&["/bin/sh", "-c", "printf hi"], 5000, 1000))
        .await
        .unwrap();
    let v = serde_json::to_value(&r).unwrap();
    assert_eq!(v["exit"]["kind"], "exited");
    assert_eq!(v["exit"]["exit_code"], 0);
    assert_eq!(v["stdout"], "hi");
    assert!(v["duration_ms"].is_number());
}
