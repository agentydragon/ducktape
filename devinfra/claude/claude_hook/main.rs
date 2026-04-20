use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::os::unix::io::AsRawFd;
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
    SessionStartSpecificOutput, ShimExecRequest, ShimResponse,
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
    /// PATH shim runtime: report to daemon, resolve real binary, exec.
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

fn daemon_dir_path(session_id: &str) -> PathBuf {
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

/// Detect root filesystem type from /proc/mounts.
fn detect_root_fstype() -> String {
    std::fs::read_to_string("/proc/mounts")
        .ok()
        .and_then(|contents| {
            contents.lines().find_map(|line| {
                let mut parts = line.split_whitespace();
                let _dev = parts.next()?;
                let mountpoint = parts.next()?;
                let fstype = parts.next()?;
                (mountpoint == "/").then(|| fstype.to_string())
            })
        })
        .unwrap_or_else(|| "unknown".to_string())
}

/// True when running inside a Firecracker microVM (checks PID 1 cmdline).
fn is_firecracker() -> bool {
    const NEEDLE: &[u8] = b"--firecracker-init";
    std::fs::read("/proc/1/cmdline")
        .map(|b| b.windows(NEEDLE.len()).any(|w| w == NEEDLE))
        .unwrap_or(false)
}

/// True when running inside gVisor (9p root or "runsc" hostname).
fn is_gvisor() -> bool {
    if detect_root_fstype() == "9p" {
        return true;
    }
    std::fs::read_to_string("/etc/hostname")
        .map(|h| h.trim() == "runsc")
        .unwrap_or(false)
}

fn write_buildbuddy_bazelrc(session_dir: &Path, api_key: &str) -> Option<PathBuf> {
    let bb_bazelrc = session_dir.join("buildbuddy.bazelrc");
    let content = format!(
        "# BuildBuddy authentication (auto-generated per session)\n\
         # Static configuration is in .bazelrc under build:rbe\n\
         common --remote_header=x-buildbuddy-api-key={api_key}\n\
         \n\
         # Enable RBE (platforms, exec properties in .bazelrc + BUILD.bazel platform)\n\
         build --config=rbe\n"
    );
    // Atomic write + 0600: file contains the API key (a secret).
    // Mirrors env_file.rs which uses the same pattern for the session env file.
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;
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

    // JVM heap sizing: full-monorepo bazel query loads 6000+ packages into
    // Skyframe analysis cache. Firecracker containers have 16Gi RAM; 8Gi heap
    // is needed to avoid OOM. gVisor containers use tmpfs (eats RAM), so use
    // a smaller heap. Mirrors bazelrc.mako in the Python implementation.
    let xmx = if is_firecracker() {
        Some("8g")
    } else if is_gvisor() {
        Some("4g")
    } else {
        None
    };
    if let Some(size) = xmx {
        lines.push(format!("startup --host_jvm_args=-Xmx{size}"));
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

    lines.push(String::new());
    lines.push("test --test_tag_filters=-live_openai_api".into());

    // BuildBuddy remote cache: write per-session buildbuddy.bazelrc with the
    // API key and build --config=rbe, then try-import it. The Python
    // implementation (buildbuddy.py) writes this file; we mirror that here.
    if let Some(api_key) = env_overlay.get("BUILDBUDDY_API_KEY") {
        if let Some(bb_bazelrc) = write_buildbuddy_bazelrc(session_dir, api_key) {
            lines.push(String::new());
            lines.push(format!("try-import {}", bb_bazelrc.display()));
        }
    }

    lines.push(format!("try-import {}", bbr_bazelrc.display()));
    lines.push(String::new());
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
        lines.push(String::new());
        lines.push("## BuildBuddy".into());
        lines.push("Bazel builds and tests by default execute remotely via BuildBuddy.".into());
        lines.push(
            "Use BuildBuddy API (key in `~/.config/bazel/buildbuddy.bazelrc`) to download undeclared test outputs, profiles, search invocations.".into()
        );
        if !session_id.is_empty() && session_id != "unknown" {
            lines.push(format!(
                "Your `bbr` invocations are tagged `session:{session_id}`. To list your builds:"
            ));
            lines.push(format!(
                "`bbapi invocation list --tag session:{session_id}`"
            ));
        }
    }

    lines.push(String::new());
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

    if let Err(e) = install_all_shims(&shims_dir, &session.session_id) {
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

fn resolve_binary_from_env(
    binary: &str,
    shim_dir: &Path,
    env: &HashMap<String, String>,
) -> Option<PathBuf> {
    let path = env.get("PATH")?;
    let shim_canon = shim_dir.canonicalize().ok();
    for dir in path.split(':') {
        if dir.is_empty() {
            continue;
        }
        let dir_path = Path::new(dir);
        if let (Some(a), Ok(b)) = (shim_canon.as_ref(), dir_path.canonicalize()) {
            if a == &b {
                continue;
            }
        }
        let candidate = dir_path.join(binary);
        if candidate.is_file() {
            use std::os::unix::fs::PermissionsExt;
            if let Ok(m) = std::fs::metadata(&candidate) {
                if m.permissions().mode() & 0o111 != 0 {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

async fn handle_shim_exec(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ShimExecRequest>,
) -> Json<ShimResponse> {
    state.touch();
    let _session = state.get_or_create_session(&req.session_id);
    let session_dir = home_session_dir(&req.session_id);
    let shim_dir = session_dir.join("bin");
    let bazelrc_path = session_dir.join("bazelrc");
    Json(shim_exec_decision(
        req,
        &state.profile,
        &shim_dir,
        &bazelrc_path,
    ))
}

/// Pure decision logic for `/shim-exec`: given a request, profile, and session
/// paths, return the `ShimResponse` the daemon should send back. No axum/hyper
/// types, no state mutation, no env lookups — testable in isolation.
///
/// Takes `req` by value so `argv` can be moved into `resolve_execve` without
/// allocation. Only the `bazelisk`/`bazel` branch needs to clone, and only to
/// inject `--bazelrc`.
fn shim_exec_decision(
    req: ShimExecRequest,
    profile: &ProfileConfig,
    shim_dir: &Path,
    bazelrc_path: &Path,
) -> ShimResponse {
    match req.shim.as_str() {
        "git" => match git_shim::evaluate(&req.argv, &profile.git_shim) {
            Ok(()) => resolve_execve(&req.shim, req.argv, shim_dir, &req.env),
            Err(message) => ShimResponse::Blocked { message },
        },
        "bazelisk" | "bazel" => {
            let mut argv = req.argv;
            if bazelrc_path.exists() {
                argv.insert(1, format!("--bazelrc={}", bazelrc_path.display()));
            }
            resolve_execve(&req.shim, argv, shim_dir, &req.env)
        }
        _ => resolve_execve(&req.shim, req.argv, shim_dir, &req.env),
    }
}

fn resolve_execve(
    shim: &str,
    mut argv: Vec<String>,
    shim_dir: &Path,
    env: &HashMap<String, String>,
) -> ShimResponse {
    match resolve_binary_from_env(shim, shim_dir, env) {
        Some(real) => {
            argv[0] = real.to_string_lossy().into_owned();
            ShimResponse::Execve { argv }
        }
        None => ShimResponse::Blocked {
            message: format!("{shim}: command not found (not on PATH outside shim directory)"),
        },
    }
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

async fn run_daemon(sock: PathBuf, daemon_dir: PathBuf) {
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
        .route("/shim-exec", post(handle_shim_exec))
        .route("/health", get(handle_health))
        .route("/mailbox", post(handle_mailbox))
        .with_state(state.clone());

    // Write pidfile and acquire exclusive flock (held for daemon lifetime).
    // Kernel releases the flock on process death, making client-side
    // is_pidfile_locked() probes authoritative for liveness detection.
    let pidfile = daemon_dir.join("daemon.pid");
    std::fs::write(&pidfile, std::process::id().to_string()).unwrap();
    let pidfile_fd = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&pidfile)
        .expect("reopen pidfile for flock");
    unsafe { libc::flock(pidfile_fd.as_raw_fd(), libc::LOCK_EX) };
    // pidfile_fd must outlive the server — keep it alive here.

    eprintln!(
        "claude-hook daemon pid={} sock={}",
        std::process::id(),
        sock.display()
    );
    let listener = UnixListener::bind(&sock).expect("failed to bind UDS");

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

// ---------------------------------------------------------------------------
// Client: double-fork + UDS request
// ---------------------------------------------------------------------------

pub(crate) fn fork_daemon(daemon_dir: &Path, sock_path: &Path) -> i32 {
    let log_out = daemon_dir.join("daemon.log");
    let log_err = daemon_dir.join("daemon.err.log");

    let self_exe = std::env::current_exe().expect("cannot determine own exe path");

    let mut pipe_fds = [0i32; 2];
    unsafe { libc::pipe(pipe_fds.as_mut_ptr()) };
    let (read_fd, write_fd) = (pipe_fds[0], pipe_fds[1]);

    let pid = unsafe { libc::fork() };
    if pid > 0 {
        unsafe { libc::close(write_fd) };
        unsafe { libc::waitpid(pid, std::ptr::null_mut(), 0) };
        let mut buf = [0u8; 32];
        let n = unsafe { libc::read(read_fd, buf.as_mut_ptr() as *mut _, buf.len()) };
        unsafe { libc::close(read_fd) };
        if n <= 0 {
            eprintln!("claude-hook: daemon startup pipe returned no data");
            return -1;
        }
        let s = std::str::from_utf8(&buf[..n as usize]).unwrap_or("").trim();
        return s.parse().unwrap_or(-1);
    }

    unsafe { libc::close(read_fd) };
    unsafe { libc::setsid() };
    let pid2 = unsafe { libc::fork() };
    if pid2 > 0 {
        let msg = pid2.to_string();
        unsafe { libc::write(write_fd, msg.as_ptr() as *const _, msg.len()) };
        unsafe { libc::close(write_fd) };
        std::process::exit(0);
    }

    unsafe { libc::close(write_fd) };

    let fd_out = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_out)
        .expect("open daemon.log");
    let fd_err = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_err)
        .expect("open daemon.err.log");
    unsafe {
        libc::dup2(std::os::unix::io::AsRawFd::as_raw_fd(&fd_out), 1);
        libc::dup2(std::os::unix::io::AsRawFd::as_raw_fd(&fd_err), 2);
    }
    drop(fd_out);
    drop(fd_err);

    let err = std::process::Command::new(&self_exe)
        .args([
            "daemon",
            "--sock",
            sock_path.to_str().unwrap(),
            "--daemon-dir",
            daemon_dir.to_str().unwrap(),
        ])
        .exec();
    panic!("exec failed: {err}");
}

pub(crate) async fn wait_for_sock(
    sock_path: &Path,
    pidfile: &Path,
    daemon_pid: i32,
    timeout: Duration,
) -> Result<(), String> {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if sock_path.exists() && UnixStream::connect(sock_path).await.is_ok() {
            return Ok(());
        }
        // Post-pidfile crash detection: daemon wrote pidfile then died.
        if pidfile.exists() && !daemon_lifecycle::is_pidfile_locked(pidfile) {
            return Err("Daemon died during startup (pidfile unlocked)".into());
        }
        // Pre-pidfile crash detection: daemon died before writing pidfile.
        let rc = unsafe { libc::kill(daemon_pid, 0) };
        if rc != 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
            return Err("Daemon process died during startup (pre-pidfile)".into());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let note = if sock_path.exists() {
        "socket appeared but connect never succeeded"
    } else {
        "socket never appeared"
    };
    Err(format!(
        "Daemon did not become reachable within {}s ({note})",
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
    let stream = UnixStream::connect(sock_path)
        .await
        .map_err(|e| format!("connect: {e}"))?;
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
        Some(Command::Daemon { sock, daemon_dir }) => {
            run_daemon(sock, daemon_dir).await;
        }
        None => dispatch_hook().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::{PathFixture, make_request};
    use claude_hook_config::GitShimConfig;

    // --------- C.1: resolve_binary_from_env ---------

    /// Harness for "PATH = shim:real, some dirs may contain a `git` stub" —
    /// assert whether the resolver returns `real/git` or `None`.
    fn assert_resolve_on_shim_real(shim_has: bool, real_has: bool, expect_real: bool) {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let real = f.mkdir("real");
        if shim_has {
            f.with_exec(&shim, "git");
        }
        if real_has {
            f.with_exec(&real, "git");
        }
        let env = f.env_with_path(&[&shim, &real]);
        let got = resolve_binary_from_env("git", &shim, &env);
        assert_eq!(got, expect_real.then(|| real.join("git")));
    }

    #[test]
    fn resolve_only_in_real() {
        assert_resolve_on_shim_real(false, true, true);
    }

    #[test]
    fn resolve_in_both_prefers_real() {
        // Shim-dir hits must always be skipped even when first on PATH.
        assert_resolve_on_shim_real(true, true, true);
    }

    #[test]
    fn resolve_only_in_shim() {
        assert_resolve_on_shim_real(true, false, false);
    }

    #[test]
    fn resolve_in_neither() {
        assert_resolve_on_shim_real(false, false, false);
    }

    #[test]
    fn resolve_handles_canonicalized_paths() {
        // PATH references shim via a symlink; resolver gets the real shim dir.
        // Both canonicalize to the same inode, so the symlinked entry is skipped.
        let f = PathFixture::new();
        let shim_real = f.mkdir("shim_real");
        let shim_link = f.root.join("shim_link");
        std::os::unix::fs::symlink(&shim_real, &shim_link).unwrap();
        let real = f.mkdir("real");
        f.with_exec(&shim_real, "git");
        f.with_exec(&real, "git");
        let env = f.env_with_path(&[&shim_link, &real]);
        assert_eq!(
            resolve_binary_from_env("git", &shim_real, &env),
            Some(real.join("git")),
        );
    }

    #[test]
    fn resolve_skips_non_executable_file() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let stub = f.mkdir("stub");
        let real = f.mkdir("real");
        f.with_nonexec(&stub, "git");
        f.with_exec(&real, "git");
        let env = f.env_with_path(&[&shim, &stub, &real]);
        assert_eq!(
            resolve_binary_from_env("git", &shim, &env),
            Some(real.join("git")),
        );
    }

    #[test]
    fn resolve_ignores_empty_path_segment() {
        // PATH with empty segment between colons: shim::real.
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let real = f.mkdir("real");
        f.with_exec(&real, "git");
        let path_env = format!("{}::{}", shim.display(), real.display());
        let env = HashMap::from([("PATH".to_string(), path_env)]);
        assert_eq!(
            resolve_binary_from_env("git", &shim, &env),
            Some(real.join("git")),
        );
    }

    #[test]
    fn resolve_returns_none_without_path_env() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        assert_eq!(resolve_binary_from_env("git", &shim, &HashMap::new()), None);
    }

    // --------- C.2: shim_exec_decision ---------

    fn all_blocks_profile() -> ProfileConfig {
        ProfileConfig {
            git_shim: GitShimConfig {
                block_add_all: true,
                block_stash: true,
                block_amend: true,
            },
            ..Default::default()
        }
    }

    /// Harness: run `shim_exec_decision` with a `real/` dir containing `shim`
    /// and return the response so the test can match on it.
    fn decide_with_real(
        shim: &str,
        argv: &[&str],
        profile: &ProfileConfig,
        bazelrc_exists: bool,
    ) -> (ShimResponse, PathBuf) {
        let f = PathFixture::new();
        let shim_dir = f.mkdir("bin");
        let real = f.mkdir("real");
        f.with_exec(&real, shim);
        let bazelrc = f.root.join("bazelrc");
        if bazelrc_exists {
            std::fs::write(&bazelrc, "# test\n").unwrap();
        }
        let req = make_request(shim, argv, &PathFixture::join_path(&[&real]));
        let resp = shim_exec_decision(req, profile, &shim_dir, &bazelrc);
        (resp, real)
    }

    fn assert_git_block(argv: &[&str], msg_substring: &str) {
        // Git blocks short-circuit before PATH resolution; the real/ dir is
        // there to prove the shim_exec_decision path doesn't accidentally
        // hit it.
        let (resp, _real) = decide_with_real("git", argv, &all_blocks_profile(), false);
        match resp {
            ShimResponse::Blocked { message } => assert!(
                message.contains(msg_substring),
                "missing {msg_substring:?} in: {message}"
            ),
            other => panic!("expected Blocked, got {other:?}"),
        }
    }

    #[test]
    fn git_block_add_dash_a() {
        assert_git_block(&["git", "add", "-A"], "git add -A");
    }
    #[test]
    fn git_block_add_dot() {
        assert_git_block(&["git", "add", "."], "git add .");
    }
    #[test]
    fn git_block_add_all() {
        assert_git_block(&["git", "add", "--all"], "git add --all");
    }
    #[test]
    fn git_block_stash() {
        assert_git_block(&["git", "stash"], "git stash");
    }
    #[test]
    fn git_block_commit_amend() {
        assert_git_block(&["git", "commit", "--amend"], "git commit --amend");
    }

    fn assert_passthrough(shim: &str, argv: &[&str], expected_suffix: &[&str]) {
        let (resp, real) = decide_with_real(shim, argv, &all_blocks_profile(), false);
        match resp {
            ShimResponse::Execve { argv } => {
                assert_eq!(argv[0], real.join(shim).display().to_string());
                let suffix: Vec<String> = argv.into_iter().skip(1).collect();
                assert_eq!(suffix, expected_suffix);
            }
            other => panic!("expected Execve, got {other:?}"),
        }
    }

    #[test]
    fn passthrough_git_status() {
        assert_passthrough("git", &["git", "status"], &["status"]);
    }
    #[test]
    fn passthrough_bb_info() {
        assert_passthrough("bb", &["bb", "info"], &["info"]);
    }

    #[test]
    fn shim_exec_blocks_when_binary_only_in_shim_dir() {
        let f = PathFixture::new();
        let shim_dir = f.mkdir("bin");
        // PATH has only shim_dir → resolver excludes it → None → Blocked.
        let req = make_request("git", &["git", "status"], &shim_dir.display().to_string());
        match shim_exec_decision(
            req,
            &ProfileConfig::default(),
            &shim_dir,
            &f.root.join("bazelrc"),
        ) {
            ShimResponse::Blocked { message } => assert!(
                message.contains("command not found"),
                "got message: {message}"
            ),
            other => panic!("expected Blocked, got {other:?}"),
        }
    }

    fn assert_bazelisk_injection(bazelrc_exists: bool) {
        let (resp, real) = decide_with_real(
            "bazelisk",
            &["bazelisk", "build", "//..."],
            &ProfileConfig::default(),
            bazelrc_exists,
        );
        match resp {
            ShimResponse::Execve { argv } => {
                assert_eq!(argv[0], real.join("bazelisk").display().to_string());
                // bazelrc path is derived inside the harness; scan argv[1] for it.
                if bazelrc_exists {
                    assert!(
                        argv[1].starts_with("--bazelrc="),
                        "expected --bazelrc= injection, got argv: {argv:?}"
                    );
                    assert_eq!(&argv[2..], &["build".to_string(), "//...".into()]);
                } else {
                    assert_eq!(&argv[1..], &["build".to_string(), "//...".into()]);
                }
            }
            other => panic!("expected Execve, got {other:?}"),
        }
    }

    #[test]
    fn bazelisk_injects_bazelrc_when_present() {
        assert_bazelisk_injection(true);
    }
    #[test]
    fn bazelisk_skips_bazelrc_when_missing() {
        assert_bazelisk_injection(false);
    }

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
    fn session_bazelrc_always_includes_bbr_bazelrc_import() {
        let fx = SessionFixture::new();
        let content = fx.write(&HashMap::new());

        assert!(
            content.contains(&format!("try-import {}", fx.bbr_bazelrc.display())),
            "must try-import bbr bazelrc: {content}"
        );
    }
}
