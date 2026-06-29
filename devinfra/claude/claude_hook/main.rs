use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::io::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};
use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use hyper_util::rt::TokioIo;
use protocol::{
    AnyHookInput, AnyHookSpecificOutput, HookOutput, HookRequest, HookResponse,
    SessionStartSpecificOutput,
};
use serde::Deserialize;
use tokio::net::{UnixListener, UnixStream};

use claude_hook_config::ProfileConfig;
use claude_hook_env_file::write_env_file;
use claude_hook_env_script::{StartupResult, source_env_script};
use claude_hook_shim_install::install_all_shims;

mod bg_command;
mod daemon_lifecycle;
mod git_shim;
mod session;
mod shim_runtime;
mod test_util;

use session::{Session, format_system_message};

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

#[derive(Parser)]
#[command(name = "claude-hook", version)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// PATH shim runtime: apply local shim policy, resolve real binary, exec.
    Shim {
        name: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },

    /// Internal: run as daemon server (started by self-fork).
    #[command(hide = true)]
    Daemon {
        #[arg(long)]
        sock: PathBuf,
        #[arg(long)]
        daemon_dir: PathBuf,
        /// File descriptor inherited from the launcher. The daemon writes
        /// "READY\n" to it (and closes it) once the UDS listener is bound.
        /// If the daemon dies before signaling ready, the kernel closes
        /// the FD on process exit and the launcher reads EOF — race-free
        /// pre-bind crash detection, independent of zombie-reap timing.
        #[arg(long)]
        ready_fd: Option<i32>,
    },
}

// ---------------------------------------------------------------------------
// Session paths (mirrors devinfra/claude/session_paths.py)
// ---------------------------------------------------------------------------

fn short_session_dir(session_id: &str) -> PathBuf {
    PathBuf::from("/tmp/claude-hd").join(session_id)
}

pub(crate) fn daemon_sock_path(session_id: &str) -> PathBuf {
    short_session_dir(session_id).join("d.sock")
}

pub(crate) fn daemon_dir_path(session_id: &str) -> PathBuf {
    short_session_dir(session_id)
}

fn session_id_from_env() -> Option<String> {
    let env_file = std::env::var("CLAUDE_ENV_FILE").ok()?;
    let p = Path::new(&env_file);
    // CLAUDE_ENV_FILE = ~/.claude/session-env/<session_id>/sessionstart-hook-0.sh
    p.parent()?.file_name()?.to_str().map(String::from)
}

pub(crate) fn home_session_dir(session_id: &str) -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
    PathBuf::from(home)
        .join(".claude")
        .join("session-env")
        .join(session_id)
}

// ---------------------------------------------------------------------------
// Daemon state
// ---------------------------------------------------------------------------

struct AppState {
    last_request: AtomicU64,
    profile: ProfileConfig,
    startup: StartupResult,
    project_dir: PathBuf,
    sock_path: PathBuf,
    sessions: RwLock<HashMap<String, Arc<Session>>>,
}

impl AppState {
    fn touch(&self) {
        self.last_request.store(Self::now(), Ordering::Relaxed);
    }

    fn now() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
    }

    fn get_or_create_session(&self, session_id: &str) -> Arc<Session> {
        {
            let g = self.sessions.read().unwrap();
            if let Some(s) = g.get(session_id) {
                return s.clone();
            }
        }
        let mut g = self.sessions.write().unwrap();
        g.entry(session_id.to_string())
            .or_insert_with(|| Arc::new(Session::new(session_id.to_string())))
            .clone()
    }
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/// True when running inside a Firecracker microVM (checks PID 1 cmdline).
fn is_firecracker() -> bool {
    const NEEDLE: &[u8] = b"--firecracker-init";
    std::fs::read("/proc/1/cmdline")
        .map(|b| b.windows(NEEDLE.len()).any(|w| w == NEEDLE))
        .unwrap_or(false)
}

fn write_buildbuddy_bazelrc(session_dir: &Path, api_key: &str) -> Option<PathBuf> {
    let bb_bazelrc = session_dir.join("buildbuddy.bazelrc");
    let content = format!(
        "# BuildBuddy authentication (auto-generated per session)\n\
         # Static configuration is in .bazelrc under build:rbe\n\
         common --remote_header=x-buildbuddy-api-key={api_key}\n\
         build --shell_executable=/bin/bash\n\
         \n\
         # Enable RBE (platforms, exec properties in .bazelrc + BUILD.bazel platform)\n\
         build --config=rbe\n"
    );
    // Atomic write + 0600: file contains the API key (a secret).
    // Mirrors env_file.rs which uses the same pattern for the session env file.
    let write_result = (|| {
        let mut tmp = tempfile::NamedTempFile::new_in(session_dir).ok()?;
        tmp.write_all(content.as_bytes()).ok()?;
        tmp.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o600))
            .ok()?;
        tmp.persist(&bb_bazelrc).ok()?;
        Some(())
    })();
    if write_result.is_some() {
        Some(bb_bazelrc)
    } else {
        eprintln!("SessionStart: failed to write buildbuddy.bazelrc");
        None
    }
}

fn write_session_bazelrc(
    session_dir: &Path,
    bbr_bazelrc: &Path,
    env_overlay: &HashMap<String, String>,
) {
    let mut lines = vec![
        "# Per-session Bazel configuration (auto-generated by claude-hook daemon)".to_string(),
    ];

    // Per-session Bazel output_base so the agent's `bazelisk` invocations don't share
    // a server with the user's interactive bazel (or with another agent session). Each
    // bazel server serializes commands on a workspace-wide command lock; sharing a
    // server across an interactive shell + a sandboxed-shell agent has produced stuck
    // command queues that don't drain. Disk caching covers most of the cost: external
    // deps refetch on first build per session, but action results are reused.
    lines.push(format!(
        "startup --output_base={}",
        shlex::try_quote(&session_dir.join("bazel-output-base").display().to_string()).unwrap()
    ));

    // Fail fast when a second bazelisk hits a busy server instead of silently queueing
    // on the command lock. Agents often background long builds; the silent queue makes
    // it look like new commands are running when they're actually waiting on a stalled
    // predecessor. With this flag the second command exits with "Another command (X)
    // is running" so the agent sees a real error and can take action.
    // `--block_for_lock` is a boolean startup option (client-side, controls whether the
    // second bazelisk waits for the workspace lock to drain). It does not accept a value
    // -- the off form is `--noblock_for_lock`; `--block_for_lock=false` is rejected with
    // "option '--block_for_lock' does not take a value". Putting it in `common` instead
    // of `startup` would make Bazel try to apply it per-command and reject it as
    // unrecognized.
    lines.push("startup --noblock_for_lock".into());

    // JVM heap sizing: full-monorepo bazel query loads 6000+ packages into
    // Skyframe analysis cache. Firecracker sessions have 16Gi RAM; 8Gi heap
    // is needed to avoid OOM. Local CLI hosts keep Bazel's default heap.
    if is_firecracker() {
        lines.push("startup --host_jvm_args=-Xmx8g".into());
    }

    // JVM truststore: Debian's /etc/ssl/certs/java/cacerts contains the
    // system CAs (including Anthropic's TLS inspection CA on web containers).
    let system_cacerts = Path::new("/etc/ssl/certs/java/cacerts");
    if system_cacerts.exists() {
        lines.push(format!(
            "startup --host_jvm_args=-Djavax.net.ssl.trustStore={}",
            shlex::try_quote(&system_cacerts.display().to_string()).unwrap()
        ));
        lines.push("startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit".into());
    }

    lines.push("test --test_tag_filters=-live_openai_api".into());

    // BuildBuddy remote cache: write per-session buildbuddy.bazelrc with the
    // API key, RBE-safe shell path, and build --config=rbe, then try-import it.
    // Keep this private: it contains the BuildBuddy API key.
    if let Some(api_key) = env_overlay.get("BUILDBUDDY_API_KEY") {
        if let Some(bb_bazelrc) = write_buildbuddy_bazelrc(session_dir, api_key) {
            lines.push(format!("try-import {}", bb_bazelrc.display()));
        }
    }

    lines.push(format!("try-import {}", bbr_bazelrc.display()));
    lines.push("common --config=ai_agent".into());

    let bazelrc = session_dir.join("bazelrc");
    let content = format!("{}\n", lines.join("\n"));
    if let Err(e) = std::fs::write(&bazelrc, content) {
        eprintln!("SessionStart: failed to write bazelrc: {e}");
    }
}

fn render_context_banner(state: &AppState, session_id: &str, daemon_log: &Path) -> String {
    let startup = &state.startup;
    let profile = &state.profile;
    let mut lines: Vec<String> = Vec::new();

    lines.push("# Claude Code session start hook — OK".into());
    lines.push(String::new());
    lines.push("**Environment:** CLI (local)".into());

    if startup.exit_code.is_some() {
        let n = startup.env_overlay.len();
        let mut var_summary = format!("yielded {n} vars");
        if !startup.env_overlay.is_empty() {
            let mut keys: Vec<&str> = startup.env_overlay.keys().map(|s| s.as_str()).collect();
            keys.sort();
            var_summary += &format!(": {}", keys.join(", "));
        }
        let status = if startup.exit_code == Some(0) {
            "succeeded"
        } else {
            "FAILED"
        };
        lines.push(format!(
            "**startup_env_script** `{}` {status} — {var_summary}",
            profile.startup_env_script.as_deref().unwrap_or("(none)")
        ));
        let trimmed = startup.output.trim();
        if !trimmed.is_empty() {
            lines.push("```".into());
            lines.push(trimmed.into());
            lines.push("```".into());
        }
    }

    if !profile.background_commands.is_empty() {
        lines.push(String::new());
        lines.push("## Background tasks".into());
        for cmd in &profile.background_commands {
            let suffix = if cmd.after_env { " (after env)" } else { "" };
            lines.push(format!("- {}{suffix}", cmd.name));
        }
    }

    let bb_key = startup.env_overlay.contains_key("BUILDBUDDY_API_KEY");
    if bb_key {
        lines.push("## BuildBuddy".into());
        lines.push("Bazel builds and tests by default execute remotely via BuildBuddy.".into());
        lines.push(
            "Use BuildBuddy API (key in `~/.config/bazel/buildbuddy.bazelrc`) to download undeclared test outputs, profiles, search invocations.".into()
        );
        if !session_id.is_empty() && session_id != "unknown" {
            lines.push(format!(
                "Your `bbr` invocations are tagged `session:{session_id}`. To list your builds: `bbapi invocation list --tag session:{session_id}`"
            ));
        }
    }

    lines.push(format!("Session start log: `{}`", daemon_log.display()));

    lines.join("\n")
}

fn handle_session_start(
    state: &AppState,
    session: &Arc<Session>,
    session_env_file: &Path,
    caller_session_id: &str,
) -> HookOutput {
    let session_dir = home_session_dir(&session.session_id);
    let shims_dir = session_dir.join("bin");

    if let Err(e) = install_all_shims(&shims_dir, &session.session_id, &state.profile.git_shim) {
        eprintln!("SessionStart: install_all_shims failed: {e}");
    }

    write_env_file(
        session_env_file,
        &shims_dir,
        &state.startup.env_overlay,
        state.profile.env_exports.as_deref(),
    );

    // Write bbr bazelrc (metadata tags for BuildBuddy invocation filtering).
    let bbr_bazelrc = session_dir.join("bbr.bazelrc");
    let bbr_content = format!(
        "# Auto-generated by session start hook\n\
         build --build_metadata=ROLE=claude-code\n\
         build --build_metadata=TAGS=session:{caller_session_id}\n"
    );
    if let Err(e) = std::fs::write(&bbr_bazelrc, &bbr_content) {
        eprintln!("SessionStart: failed to write bbr bazelrc: {e}");
    }

    // Write session bazelrc (injected via --bazelrc by the bazel/bazelisk shim).
    write_session_bazelrc(&session_dir, &bbr_bazelrc, &state.startup.env_overlay);

    // Launch background commands. `after_env: false` runs immediately (no
    // env file sourced); `after_env: true` sources the session env file.
    for cmd in &state.profile.background_commands {
        let env_file_for_bg = cmd.after_env.then(|| session_env_file.to_path_buf());
        bg_command::launch(
            session.clone(),
            cmd.clone(),
            state.sock_path.clone(),
            env_file_for_bg,
            state.project_dir.clone(),
            state.startup.env_overlay.clone(),
        );
    }

    let daemon_log = state.sock_path.parent().unwrap().join("daemon.err.log");
    let banner = render_context_banner(state, caller_session_id, &daemon_log);

    HookOutput {
        hook_specific_output: Some(AnyHookSpecificOutput::SessionStart(
            SessionStartSpecificOutput {
                additional_context: Some(banner),
                ..Default::default()
            },
        )),
        ..Default::default()
    }
}

async fn handle_hook(
    State(state): State<Arc<AppState>>,
    Json(req): Json<HookRequest>,
) -> Json<HookResponse> {
    state.touch();

    let session_env_file = req.env.get("CLAUDE_ENV_FILE").map(PathBuf::from);
    let session_id = req.hook.session_id();

    let session = session_id
        .as_deref()
        .map(|id| state.get_or_create_session(id));

    let mut output = match (&req.hook, session.as_ref()) {
        (AnyHookInput::SessionStart(_), Some(s)) => {
            if let Some(env_file) = session_env_file {
                let caller_sid = req
                    .env
                    .get("CLAUDE_CODE_SESSION_ID")
                    .map(|s| s.as_str())
                    .unwrap_or("unknown");
                Some(handle_session_start(&state, s, &env_file, caller_sid))
            } else {
                eprintln!("SessionStart: CLAUDE_ENV_FILE not in request env");
                Some(HookOutput::default())
            }
        }
        // PreToolUse / PostToolUse: no per-hook logic; output is built entirely
        // from the mailbox drain below. Listed explicitly so the dispatch table
        // reads as "these are handled" rather than falling into the noop arm.
        (AnyHookInput::PreToolUse(_) | AnyHookInput::PostToolUse(_), _) => None,
        _ => None,
    };

    // Drain mailbox into output.system_message for REPL hooks.
    if req.hook.is_repl() {
        if let Some(s) = session {
            let mailbox = s.drain_messages();
            let bg = s.drain_bg_output();
            if let Some(msg) = format_system_message(mailbox, bg) {
                let out = output.get_or_insert_with(HookOutput::default);
                out.system_message = Some(match out.system_message.take() {
                    Some(existing) if !existing.is_empty() => format!("{existing}\n\n{msg}"),
                    _ => msg,
                });
            }
        }
    }

    Json(HookResponse { output })
}

async fn handle_health() -> &'static str {
    r#"{"status":"ok"}"#
}

#[derive(Deserialize)]
struct MailboxRequest {
    message: String,
}

async fn handle_mailbox(
    State(state): State<Arc<AppState>>,
    Json(req): Json<MailboxRequest>,
) -> &'static str {
    state.touch();
    // Post to every registered session's mailbox (matches server.py).
    let g = state.sessions.read().unwrap();
    for s in g.values() {
        s.post_message(req.message.clone());
    }
    r#"{"status":"ok"}"#
}

// ---------------------------------------------------------------------------
// Daemon entry point
// ---------------------------------------------------------------------------

async fn run_daemon(sock: PathBuf, daemon_dir: PathBuf, ready_fd: Option<i32>) {
    let project_dir = std::env::var("CLAUDE_PROJECT_DIR")
        .map(PathBuf::from)
        .expect("CLAUDE_PROJECT_DIR must be set");

    let profile_rel = std::env::var("DUCKTAPE_CLAUDE_HOOKS_PROFILE")
        .expect("DUCKTAPE_CLAUDE_HOOKS_PROFILE must be set");
    let profile_path = project_dir.join(&profile_rel);
    let profile = ProfileConfig::load(&profile_path).unwrap_or_else(|e| {
        eprintln!("failed to load profile {}: {e}", profile_path.display());
        std::process::exit(1);
    });

    let startup = if let Some(script_rel) = &profile.startup_env_script {
        let script_path = project_dir.join(script_rel);
        eprintln!(
            "daemon: sourcing startup_env_script {}",
            script_path.display()
        );
        let r = source_env_script(&script_path, &HashMap::new());
        if let Some(code) = r.exit_code {
            if code != 0 {
                eprintln!("startup_env_script exited {code}: {}", r.output);
            } else if !r.output.is_empty() {
                eprintln!("startup_env_script output:\n{}", r.output);
            }
        }
        eprintln!(
            "daemon: env overlay captured ({} vars: {:?})",
            r.env_overlay.len(),
            r.env_overlay.keys().collect::<Vec<_>>()
        );
        r
    } else {
        StartupResult::default()
    };

    let state = Arc::new(AppState {
        last_request: AtomicU64::new(AppState::now()),
        profile,
        startup,
        project_dir,
        sock_path: sock.clone(),
        sessions: RwLock::new(HashMap::new()),
    });

    let app = Router::new()
        .route("/hook", post(handle_hook))
        .route("/health", get(handle_health))
        .route("/mailbox", post(handle_mailbox))
        .with_state(state.clone());

    // Publish the pidfile only after its flock is already held. The launcher
    // polls `pidfile exists && unlocked` as a late-crash signal while waiting
    // for READY; writing the pidfile before locking it creates a false death
    // window and can make the launcher close the readiness pipe too early.
    let pidfile = daemon_dir.join("daemon.pid");
    let pidfile_fd = write_locked_pidfile(&pidfile);
    // pidfile_fd must outlive the server — keep it alive here.

    eprintln!(
        "claude-hook daemon pid={} sock={}",
        std::process::id(),
        sock.display()
    );
    let listener = UnixListener::bind(&sock).expect("failed to bind UDS");

    // Signal readiness to the launcher (see `Command::Daemon::ready_fd`).
    // Order matters: bind must precede this so any client that connects
    // immediately after the launcher returns sees a kernel-queued socket.
    if let Some(fd) = ready_fd {
        // SAFETY: `fork_daemon` passes this FD over exec via `--ready-fd`;
        // it is valid and uniquely owned by us at this point. Wrapping
        // in `OwnedFd` claims ownership so `File`'s drop will close it.
        let owned = unsafe { OwnedFd::from_raw_fd(fd) };
        if let Err(e) = std::fs::File::from(owned).write_all(b"READY\n") {
            eprintln!("daemon: ready signal write failed: {e}");
        }
    }

    // Idle watchdog: SIGTERM after 30min of no requests.
    if state.profile.idle_watchdog {
        let wstate = state.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(30));
            loop {
                interval.tick().await;
                let idle = AppState::now() - wstate.last_request.load(Ordering::Relaxed);
                if idle >= 1800 {
                    eprintln!("Idle timeout reached ({idle}s), shutting down");
                    unsafe { libc::raise(libc::SIGTERM) };
                    return;
                }
            }
        });
    }

    axum::serve(listener, app).await.unwrap();
    drop(pidfile_fd); // explicit: flock released here (or on process death)
}

fn write_locked_pidfile(pidfile: &Path) -> std::fs::File {
    let parent = pidfile.parent().expect("pidfile must have parent");
    let mut tmp = tempfile::NamedTempFile::new_in(parent).expect("create pidfile tmp");
    write!(tmp, "{}", std::process::id()).expect("write pidfile tmp");
    let rc = unsafe { libc::flock(tmp.as_file().as_raw_fd(), libc::LOCK_EX) };
    assert!(
        rc == 0,
        "flock pidfile failed: {}",
        std::io::Error::last_os_error()
    );
    tmp.persist(pidfile).expect("persist pidfile")
}

// ---------------------------------------------------------------------------
// Client: double-fork + UDS request
// ---------------------------------------------------------------------------

/// Returned by `fork_daemon`: the daemon's pid plus the read end of the
/// readiness pipe. The caller awaits a `"READY"` write or EOF on the read
/// end via `wait_for_sock`.
pub(crate) struct DaemonFork {
    /// PID of the double-forked daemon process. Retained for diagnostics
    /// (e.g. surfacing in error messages) even though the readiness pipe
    /// supersedes `kill(pid, 0)` polling for liveness checks.
    #[allow(dead_code)]
    pub pid: i32,
    pub ready_read: OwnedFd,
}

pub(crate) fn fork_daemon(daemon_dir: &Path, sock_path: &Path) -> DaemonFork {
    use nix::sys::wait::waitpid;
    use nix::unistd::{ForkResult, fork, pipe, setsid};
    use std::os::fd::IntoRawFd;

    let log_out = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(daemon_dir.join("daemon.log"))
        .expect("open daemon.log");
    let log_err = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(daemon_dir.join("daemon.err.log"))
        .expect("open daemon.err.log");

    let self_exe = std::env::current_exe().expect("cannot determine own exe path");

    // Pipe 1: first child reports the double-forked grandchild's pid back.
    let (pid_read, pid_write) = pipe().expect("pid pipe");
    // Pipe 2: daemon (post-exec) signals readiness. Write end is inherited
    // across exec (no CLOEXEC); read end stays here. If the daemon dies
    // before writing READY, the kernel closes its FD on process exit and the
    // launcher reads EOF — race-free pre-bind crash detection.
    let (ready_read, ready_write) = pipe().expect("ready pipe");

    // SAFETY: After fork in the child, only async-signal-safe libc calls are
    // valid until exec. We do setsid + fork + a tiny pipe write + exit (in
    // the first child) or exec (in the grandchild) — all async-signal-safe.
    match unsafe { fork() }.expect("fork failed") {
        ForkResult::Parent { child } => {
            drop(pid_write);
            drop(ready_write);
            waitpid(child, None).ok();

            let mut buf = String::new();
            let daemon_pid = match std::fs::File::from(pid_read).read_to_string(&mut buf) {
                Ok(n) if n > 0 => buf.trim().parse().unwrap_or(-1),
                _ => {
                    eprintln!("claude-hook: daemon startup pipe returned no data");
                    -1
                }
            };
            DaemonFork {
                pid: daemon_pid,
                ready_read,
            }
        }
        ForkResult::Child => {
            drop(pid_read);
            drop(ready_read);
            setsid().expect("setsid");

            // SAFETY: same constraint as the outer fork.
            match unsafe { fork() }.expect("second fork failed") {
                ForkResult::Parent { child: grandchild } => {
                    let msg = grandchild.as_raw().to_string();
                    let _ = std::fs::File::from(pid_write).write_all(msg.as_bytes());
                    drop(ready_write);
                    std::process::exit(0);
                }
                ForkResult::Child => {
                    drop(pid_write);
                    // Release ownership so Drop doesn't close it — the FD must
                    // survive `exec` for the post-exec daemon to read its
                    // `--ready-fd` arg and signal readiness.
                    let ready_fd = ready_write.into_raw_fd();

                    let err = std::process::Command::new(&self_exe)
                        .args([
                            "daemon",
                            "--sock",
                            sock_path.to_str().unwrap(),
                            "--daemon-dir",
                            daemon_dir.to_str().unwrap(),
                            "--ready-fd",
                            &ready_fd.to_string(),
                        ])
                        .stdout(std::process::Stdio::from(log_out))
                        .stderr(std::process::Stdio::from(log_err))
                        .exec();
                    panic!("exec failed: {err}");
                }
            }
        }
    }
}

pub(crate) async fn wait_for_sock(
    sock_path: &Path,
    pidfile: &Path,
    ready_read: OwnedFd,
    timeout: Duration,
) -> Result<(), String> {
    use nix::fcntl::{FcntlArg, OFlag, fcntl};

    // Poll the readiness pipe non-blocking alongside the 100ms tick loop.
    let current = fcntl(&ready_read, FcntlArg::F_GETFL).map_err(|e| format!("F_GETFL: {e}"))?;
    let flags = OFlag::from_bits_retain(current) | OFlag::O_NONBLOCK;
    fcntl(&ready_read, FcntlArg::F_SETFL(flags)).map_err(|e| format!("F_SETFL: {e}"))?;
    let mut ready_file = std::fs::File::from(ready_read);

    let deadline = std::time::Instant::now() + timeout;
    let mut buf = [0u8; 16];
    while std::time::Instant::now() < deadline {
        match ready_file.read(&mut buf) {
            Ok(0) => {
                return Err(
                    "Daemon process died during startup (readiness pipe closed without signal)"
                        .into(),
                );
            }
            Ok(_) => {
                // Daemon signaled ready. Verify the socket also accepts a
                // connection — cheap belt-and-suspenders against a daemon
                // that wrote READY but somehow lost the listener.
                if sock_path.exists() && UnixStream::connect(sock_path).await.is_ok() {
                    return Ok(());
                }
                return Err("Daemon signaled ready but socket connect failed".into());
            }
            Err(e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(format!("readiness pipe read error: {e}")),
        }
        // Late-crash detection: daemon wrote pidfile (which happens before
        // the ready signal in `run_daemon`) but died before binding. The
        // readiness EOF normally covers this, but the pidfile flock check
        // catches the daemon dying after it sent READY too.
        if pidfile.exists() && !daemon_lifecycle::is_pidfile_locked(pidfile) {
            return Err("Daemon died during startup (pidfile unlocked)".into());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err(format!(
        "Daemon did not become reachable within {}s (no READY signal)",
        timeout.as_secs()
    ))
}

pub(crate) async fn post_json_over_uds(
    sock_path: &Path,
    path: &str,
    body: Vec<u8>,
) -> Result<Bytes, String> {
    tokio::time::timeout(
        Duration::from_secs(300),
        post_json_over_uds_inner(sock_path, path, body),
    )
    .await
    .map_err(|_| "daemon request timed out (300s)".to_string())?
}

async fn post_json_over_uds_inner(
    sock_path: &Path,
    path: &str,
    body: Vec<u8>,
) -> Result<Bytes, String> {
    // Connect deadline must stay tight: a hung daemon (or a half-open socket on
    // an exotic FS) used to spin the shim until the outer 300s ceiling, blocking
    // the parent shell. The 2s value mirrors `daemon_lifecycle::health_check`.
    // The "connect:" prefix on both branches lets `shim_runtime` classify this
    // as "daemon unreachable" and trigger respawn.
    let stream =
        match tokio::time::timeout(Duration::from_secs(2), UnixStream::connect(sock_path)).await {
            Ok(Ok(s)) => s,
            Ok(Err(e)) => return Err(format!("connect: {e}")),
            Err(_) => return Err("connect: timed out after 2s".to_string()),
        };
    let io = TokioIo::new(stream);
    let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(|e| format!("http1 handshake: {e}"))?;
    tokio::spawn(async move {
        if let Err(e) = conn.await {
            eprintln!("uds connection driver: {e}");
        }
    });

    let req = hyper::Request::builder()
        .method("POST")
        .uri(path)
        .header("host", "localhost")
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)))
        .map_err(|e| format!("build request: {e}"))?;

    let resp = sender
        .send_request(req)
        .await
        .map_err(|e| format!("send request: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("daemon returned HTTP {}", resp.status()));
    }

    resp.collect()
        .await
        .map_err(|e| format!("read body: {e}"))
        .map(|c| c.to_bytes())
}

async fn dispatch_hook() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();

    let hook: AnyHookInput = match serde_json::from_str(&input) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("claude-hook: failed to parse hook input: {e}");
            std::process::exit(1);
        }
    };
    let env: HashMap<String, String> = std::env::vars().collect();
    let req = HookRequest { hook, env };

    let session_id = match session_id_from_env() {
        Some(id) => id,
        None => {
            eprintln!("claude-hook: cannot determine session_id (CLAUDE_ENV_FILE not set)");
            print!("{{}}");
            return;
        }
    };

    let sock_path = daemon_sock_path(&session_id);
    let daemon_dir = daemon_dir_path(&session_id);

    if let Err(e) = daemon_lifecycle::ensure_daemon(&sock_path, &daemon_dir).await {
        eprintln!("claude-hook: {e}");
        print!("{{}}");
        return;
    }

    let body = serde_json::to_vec(&req).unwrap();
    match post_json_over_uds(&sock_path, "/hook", body).await {
        Ok(resp_body) => {
            // The daemon returns a HookResponse envelope; Claude Code expects
            // just the HookOutput on stdout (no wrapper).
            match serde_json::from_slice::<HookResponse>(&resp_body) {
                Ok(resp) => {
                    if let Some(output) = resp.output {
                        let out = serde_json::to_vec(&output).unwrap();
                        io::stdout().write_all(&out).unwrap();
                    }
                }
                Err(e) => {
                    eprintln!("claude-hook: failed to parse daemon response: {e}");
                    io::stdout().write_all(&resp_body).unwrap();
                }
            }
        }
        Err(e) => {
            eprintln!("claude-hook: daemon request failed: {e}");
            print!("{{}}");
        }
    }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    match cli.command {
        Some(Command::Shim { name, args }) => shim_runtime::run_shim(name, args).await,
        Some(Command::Daemon {
            sock,
            daemon_dir,
            ready_fd,
        }) => {
            run_daemon(sock, daemon_dir, ready_fd).await;
        }
        None => dispatch_hook().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::PathFixture;

    // --------- C.3: write_session_bazelrc / write_buildbuddy_bazelrc ---------

    struct SessionFixture {
        _f: PathFixture,
        session_dir: PathBuf,
        bbr_bazelrc: PathBuf,
    }

    impl SessionFixture {
        fn new() -> Self {
            let f = PathFixture::new();
            let session_dir = f.mkdir("session");
            let bbr_bazelrc = session_dir.join("bbr.bazelrc");
            std::fs::write(&bbr_bazelrc, "# bbr\n").unwrap();
            Self {
                _f: f,
                session_dir,
                bbr_bazelrc,
            }
        }

        fn write(&self, env: &HashMap<String, String>) -> String {
            write_session_bazelrc(&self.session_dir, &self.bbr_bazelrc, env);
            std::fs::read_to_string(self.session_dir.join("bazelrc")).unwrap()
        }
    }

    #[test]
    fn write_locked_pidfile_is_locked_when_visible() {
        let f = PathFixture::new();
        let dir = f.mkdir("daemon");
        let pidfile = dir.join("daemon.pid");

        let pidfile_fd = write_locked_pidfile(&pidfile);

        assert_eq!(
            std::fs::read_to_string(&pidfile).unwrap(),
            std::process::id().to_string()
        );
        assert!(
            daemon_lifecycle::is_pidfile_locked(&pidfile),
            "published pidfile must already be locked"
        );

        drop(pidfile_fd);
        assert!(
            !daemon_lifecycle::is_pidfile_locked(&pidfile),
            "dropping the pidfile fd must release the lock"
        );
    }

    #[test]
    fn session_bazelrc_with_api_key_writes_buildbuddy_bazelrc() {
        let fx = SessionFixture::new();
        let mut env = HashMap::new();
        env.insert(
            "BUILDBUDDY_API_KEY".to_string(),
            "test-api-key-123".to_string(),
        );

        let session_rc = fx.write(&env);

        let bb_bazelrc = fx.session_dir.join("buildbuddy.bazelrc");
        assert!(bb_bazelrc.exists(), "buildbuddy.bazelrc must be created");
        let bb_content = std::fs::read_to_string(&bb_bazelrc).unwrap();
        assert!(
            bb_content.contains("x-buildbuddy-api-key=test-api-key-123"),
            "API key must be in buildbuddy.bazelrc: {bb_content}"
        );
        assert!(
            bb_content.contains("build --config=rbe"),
            "RBE config must be enabled: {bb_content}"
        );
        assert!(
            bb_content.contains("build --shell_executable=/bin/bash"),
            "RBE shell path must be normalized: {bb_content}"
        );
        assert!(
            session_rc.contains(&format!("try-import {}", bb_bazelrc.display())),
            "session bazelrc must try-import buildbuddy.bazelrc: {session_rc}"
        );
    }

    #[test]
    fn session_bazelrc_without_api_key_skips_buildbuddy_bazelrc() {
        let fx = SessionFixture::new();
        let content = fx.write(&HashMap::new());

        assert!(
            !fx.session_dir.join("buildbuddy.bazelrc").exists(),
            "buildbuddy.bazelrc must not be created without API key"
        );
        assert!(
            !content.contains("buildbuddy.bazelrc"),
            "session bazelrc must not reference buildbuddy.bazelrc: {content}"
        );
    }

    #[test]
    fn session_bazelrc_always_includes_test_tag_filter_and_ai_agent() {
        let fx = SessionFixture::new();
        let content = fx.write(&HashMap::new());

        assert!(
            content.contains("test --test_tag_filters=-live_openai_api"),
            "must filter live_openai_api tests: {content}"
        );
        assert!(
            content.contains("common --config=ai_agent"),
            "must set ai_agent config: {content}"
        );
    }

    #[test]
    fn session_bazelrc_isolates_output_base_and_fails_fast_on_lock() {
        let fx = SessionFixture::new();
        let content = fx.write(&HashMap::new());

        let expected_output_base = fx.session_dir.join("bazel-output-base");
        assert!(
            content.contains(&format!(
                "startup --output_base={}",
                expected_output_base.display()
            )),
            "must pin output_base to a session-local path: {content}"
        );
        assert!(
            content.contains("startup --noblock_for_lock"),
            "must fail-fast on lock contention instead of queueing: {content}"
        );
    }

    #[test]
    fn session_bazelrc_always_includes_bbr_bazelrc_import() {
        let fx = SessionFixture::new();
        let content = fx.write(&HashMap::new());

        assert!(
            content.contains(&format!("try-import {}", fx.bbr_bazelrc.display())),
            "must try-import bbr bazelrc: {content}"
        );
    }
}
