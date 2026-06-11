//! `claude-hook shim <name> [args...]` runtime.
//!
//! Shims are deliberately self-contained. They do not contact the hook daemon:
//! sandboxed Claude Code tool calls can block or deny AF_UNIX sockets, and the
//! old daemon-backed `/shim-exec` path made simple tool execution depend on a
//! control channel the sandbox might not expose.

use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};

use claude_hook_config::GitShimConfig;
use claude_hook_shim_install::{
    GIT_BLOCK_ADD_ALL_ENV, GIT_BLOCK_AMEND_ENV, GIT_BLOCK_STASH_ENV, SHIM_DIR_ENV,
    SHIM_SESSION_ID_ENV,
};
use url::Url;

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ShimDecision {
    Block { message: String, exit_code: i32 },
    Exec(Vec<String>),
}

pub async fn run_shim(name: String, forwarded: Vec<String>) -> ! {
    let env: HashMap<String, String> = std::env::vars().collect();
    let session_id = env.get(SHIM_SESSION_ID_ENV).unwrap_or_else(|| {
        eprintln!("claude-hook shim: {SHIM_SESSION_ID_ENV} not set in env; shim wrapper broken?");
        std::process::exit(2);
    });
    let shim_dir = shim_dir_from_env(session_id, &env);

    let approved_argv = match decide(&name, forwarded, session_id, &shim_dir, &env) {
        ShimDecision::Block { message, exit_code } => {
            eprintln!("[{name}-shim] BLOCKED: {message}");
            std::process::exit(exit_code);
        }
        ShimDecision::Exec(argv) => argv,
    };

    let err = std::process::Command::new(&approved_argv[0])
        .args(&approved_argv[1..])
        .exec();
    eprintln!("{name}: exec failed: {err}");
    std::process::exit(126);
}

fn decide(
    name: &str,
    forwarded: Vec<String>,
    session_id: &str,
    shim_dir: &Path,
    env: &HashMap<String, String>,
) -> ShimDecision {
    let mut argv = vec![name.to_string()];
    argv.extend(forwarded);

    match name {
        "git" => {
            let git = git_config_from_env(env);
            if let Err(message) = crate::git_shim::evaluate(&argv, &git) {
                return ShimDecision::Block {
                    message,
                    exit_code: 1,
                };
            }
            resolve_execve(name, argv, shim_dir, env)
        }
        "bazelisk" | "bazel" => {
            inject_bazel_startup_args(&mut argv, session_id, env);
            resolve_execve(name, argv, shim_dir, env)
        }
        // TODO(devinfra/claude/TODO.md): decide whether direct local `bb`
        // invocations should also inject the session bazelrc. It is trickier
        // than Bazel/Bazelisk because `bb` also fronts remote execution modes.
        _ => resolve_execve(name, argv, shim_dir, env),
    }
}

/// Inject session Bazel startup flags.
///
/// Claude's Linux sandbox exposes its filtered network path through HTTP(S)
/// proxy env vars. Bazel's RBE/BES clients use grpc-java, which ignores those
/// env vars but does honor Java proxyHost/proxyPort system properties, so the
/// shim translates the proxy env into `--host_jvm_args=-D...` startup args.
fn inject_bazel_startup_args(
    argv: &mut Vec<String>,
    session_id: &str,
    env: &HashMap<String, String>,
) {
    let bazelrc = home_session_dir_from_env(session_id, env).join("bazelrc");
    let mut startup_insert = 1;
    if bazelrc.exists() {
        argv.insert(1, format!("--bazelrc={}", bazelrc.display()));
        startup_insert = 2;
    }
    argv.splice(
        startup_insert..startup_insert,
        bazel_proxy_args_from_env(env),
    );
}

fn shim_dir_from_env(session_id: &str, env: &HashMap<String, String>) -> PathBuf {
    env.get(SHIM_DIR_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|| home_session_dir_from_env(session_id, env).join("bin"))
}

fn home_session_dir_from_env(session_id: &str, env: &HashMap<String, String>) -> PathBuf {
    let home = env
        .get("HOME")
        .cloned()
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_else(|| "/root".to_string());
    PathBuf::from(home)
        .join(".claude")
        .join("session-env")
        .join(session_id)
}

fn env_flag(env: &HashMap<String, String>, key: &str) -> bool {
    matches!(
        env.get(key).map(|s| s.as_str()),
        Some("1" | "true" | "TRUE" | "yes" | "YES" | "on" | "ON")
    )
}

fn git_config_from_env(env: &HashMap<String, String>) -> GitShimConfig {
    GitShimConfig {
        block_add_all: env_flag(env, GIT_BLOCK_ADD_ALL_ENV),
        block_stash: env_flag(env, GIT_BLOCK_STASH_ENV),
        block_amend: env_flag(env, GIT_BLOCK_AMEND_ENV),
    }
}

fn resolve_execve(
    shim: &str,
    mut argv: Vec<String>,
    shim_dir: &Path,
    env: &HashMap<String, String>,
) -> ShimDecision {
    match resolve_binary_from_env(shim, shim_dir, env) {
        Some(real) => {
            argv[0] = real.to_string_lossy().into_owned();
            ShimDecision::Exec(argv)
        }
        None => ShimDecision::Block {
            message: format!("{shim}: command not found (not on PATH outside shim directory)"),
            exit_code: 127,
        },
    }
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
            if let Ok(m) = std::fs::metadata(&candidate) {
                if m.permissions().mode() & 0o111 != 0 {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

fn bazel_proxy_args_from_env(env: &HashMap<String, String>) -> Vec<String> {
    let Some(proxy_url) = ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"]
        .into_iter()
        .find_map(|k| env.get(k))
    else {
        return vec![];
    };
    let Ok(parsed) = Url::parse(proxy_url) else {
        return vec![];
    };
    if !matches!(parsed.scheme(), "http" | "https") {
        return vec![];
    }
    let Some(host) = parsed.host_str() else {
        return vec![];
    };
    let Some(port) = parsed.port_or_known_default() else {
        return vec![];
    };

    let username = (!parsed.username().is_empty()).then(|| percent_decode(parsed.username()));
    let password = parsed.password().map(percent_decode);

    let mut values = vec![
        ("proxyHost".to_string(), host.to_string()),
        ("proxyPort".to_string(), port.to_string()),
    ];
    if let Some(username) = username {
        values.push(("proxyUser".to_string(), username));
    }
    if let Some(password) = password {
        values.push(("proxyPassword".to_string(), password));
    }

    let mut args = vec![];
    for scheme in ["https", "http"] {
        for (key, value) in &values {
            args.push(format!("--host_jvm_args=-D{scheme}.{key}={value}"));
        }
    }
    args
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(hi), Some(lo)) = (from_hex(bytes[i + 1]), from_hex(bytes[i + 2])) {
                out.push((hi << 4) | lo);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn from_hex(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::PathFixture;

    fn env_for(f: &PathFixture, shim_dir: &Path, real_dir: &Path) -> HashMap<String, String> {
        let mut env = f.env_with_path(&[shim_dir, real_dir]);
        env.insert(SHIM_DIR_ENV.to_string(), shim_dir.display().to_string());
        env
    }

    fn decide_with_real(
        name: &str,
        args: &[&str],
        env: HashMap<String, String>,
        shim_dir: &Path,
        session_id: &str,
    ) -> ShimDecision {
        decide(
            name,
            args.iter().map(|s| s.to_string()).collect(),
            session_id,
            shim_dir,
            &env,
        )
    }

    #[test]
    fn resolves_real_binary_outside_shim_dir() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let real = f.mkdir("real");
        f.with_exec(&shim, "bb");
        let real_bb = f.with_exec(&real, "bb");
        let env = env_for(&f, &shim, &real);

        match decide_with_real("bb", &["info"], env, &shim, "s1") {
            ShimDecision::Exec(argv) => {
                assert_eq!(
                    argv,
                    vec![real_bb.display().to_string(), "info".to_string()]
                );
            }
            other => panic!("expected Exec, got {other:?}"),
        }
    }

    #[test]
    fn missing_real_binary_blocks_with_127() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        f.with_exec(&shim, "bb");
        let env = HashMap::from([
            ("PATH".to_string(), shim.display().to_string()),
            (SHIM_DIR_ENV.to_string(), shim.display().to_string()),
        ]);

        assert_eq!(
            decide_with_real("bb", &["info"], env, &shim, "s1"),
            ShimDecision::Block {
                message: "bb: command not found (not on PATH outside shim directory)".to_string(),
                exit_code: 127,
            }
        );
    }

    #[test]
    fn resolver_skips_non_executable_files() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let stub = f.mkdir("stub");
        let real = f.mkdir("real");
        f.with_nonexec(&stub, "bb");
        let real_bb = f.with_exec(&real, "bb");
        let env = f.env_with_path(&[&shim, &stub, &real]);

        assert_eq!(resolve_binary_from_env("bb", &shim, &env), Some(real_bb));
    }

    #[test]
    fn git_blocks_only_enabled_policies() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let real = f.mkdir("real");
        f.with_exec(&real, "git");
        let mut env = env_for(&f, &shim, &real);
        env.insert(GIT_BLOCK_ADD_ALL_ENV.to_string(), "1".to_string());
        env.insert(GIT_BLOCK_AMEND_ENV.to_string(), "0".to_string());
        env.insert(GIT_BLOCK_STASH_ENV.to_string(), "0".to_string());

        match decide_with_real("git", &["add", "-A"], env.clone(), &shim, "s1") {
            ShimDecision::Block { message, exit_code } => {
                assert_eq!(exit_code, 1);
                assert!(message.contains("git add -A"));
            }
            other => panic!("expected Block, got {other:?}"),
        }
        match decide_with_real("git", &["commit", "--amend"], env, &shim, "s1") {
            ShimDecision::Exec(argv) => {
                assert_eq!(&argv[1..], &["commit".to_string(), "--amend".to_string()])
            }
            other => panic!("expected Exec, got {other:?}"),
        }
    }

    fn assert_bazel_tool_injects_bazelrc_and_proxy_args(name: &str) {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let real = f.mkdir("real");
        let home = f.mkdir("home");
        let session_dir = home.join(".claude/session-env/s1");
        std::fs::create_dir_all(&session_dir).unwrap();
        let bazelrc = session_dir.join("bazelrc");
        std::fs::write(&bazelrc, "# test\n").unwrap();
        let real_binary = f.with_exec(&real, name);
        let mut env = env_for(&f, &shim, &real);
        env.insert("HOME".to_string(), home.display().to_string());
        env.insert(
            "HTTPS_PROXY".to_string(),
            "http://user%20name:p%40ss@localhost:3128".to_string(),
        );

        match decide_with_real(name, &["build", "//..."], env, &shim, "s1") {
            ShimDecision::Exec(argv) => {
                assert_eq!(argv[0], real_binary.display().to_string());
                assert_eq!(argv[1], format!("--bazelrc={}", bazelrc.display()));
                assert!(argv.contains(&"--host_jvm_args=-Dhttps.proxyHost=localhost".to_string()));
                assert!(argv.contains(&"--host_jvm_args=-Dhttps.proxyPort=3128".to_string()));
                assert!(argv.contains(&"--host_jvm_args=-Dhttps.proxyUser=user name".to_string()));
                assert!(argv.contains(&"--host_jvm_args=-Dhttp.proxyPassword=p@ss".to_string()));
                assert_eq!(&argv[10..], &["build".to_string(), "//...".to_string()]);
            }
            other => panic!("expected Exec, got {other:?}"),
        }
    }

    #[test]
    fn bazelisk_injects_bazelrc_and_proxy_args() {
        assert_bazel_tool_injects_bazelrc_and_proxy_args("bazelisk");
    }

    #[test]
    fn bb_does_not_yet_inject_bazel_startup_args() {
        let f = PathFixture::new();
        let shim = f.mkdir("shim");
        let real = f.mkdir("real");
        let home = f.mkdir("home");
        let session_dir = home.join(".claude/session-env/s1");
        std::fs::create_dir_all(&session_dir).unwrap();
        std::fs::write(session_dir.join("bazelrc"), "# test\n").unwrap();
        let real_bb = f.with_exec(&real, "bb");
        let mut env = env_for(&f, &shim, &real);
        env.insert("HOME".to_string(), home.display().to_string());
        env.insert("HTTP_PROXY".to_string(), "http://proxy.example".to_string());

        match decide_with_real("bb", &["build", "//..."], env, &shim, "s1") {
            ShimDecision::Exec(argv) => {
                assert_eq!(
                    argv,
                    vec![
                        real_bb.display().to_string(),
                        "build".to_string(),
                        "//...".to_string()
                    ]
                );
            }
            other => panic!("expected Exec, got {other:?}"),
        }
    }

    #[test]
    fn bazel_proxy_uses_http_first_and_default_ports() {
        let env = HashMap::from([
            ("HTTP_PROXY".to_string(), "http://proxy.example".to_string()),
            (
                "HTTPS_PROXY".to_string(),
                "http://ignored.example:9999".to_string(),
            ),
        ]);
        let args = bazel_proxy_args_from_env(&env);
        assert!(args.contains(&"--host_jvm_args=-Dhttps.proxyHost=proxy.example".to_string()));
        assert!(args.contains(&"--host_jvm_args=-Dhttps.proxyPort=80".to_string()));
        assert!(!args.iter().any(|arg| arg.contains("ignored.example")));
    }

    #[test]
    fn bazel_proxy_ignores_missing_or_invalid_proxy() {
        assert!(bazel_proxy_args_from_env(&HashMap::new()).is_empty());
        assert!(
            bazel_proxy_args_from_env(&HashMap::from([(
                "HTTP_PROXY".to_string(),
                "socks5://proxy.example:1080".to_string()
            )]))
            .is_empty()
        );
    }

    #[test]
    fn installed_git_shim_config_is_opt_in() {
        assert!(!claude_hook_shim_install::git_shim_enabled(
            &git_config_from_env(&HashMap::new())
        ));
        assert!(claude_hook_shim_install::git_shim_enabled(
            &git_config_from_env(&HashMap::from([(
                GIT_BLOCK_STASH_ENV.to_string(),
                "true".to_string()
            )]))
        ));
    }
}
