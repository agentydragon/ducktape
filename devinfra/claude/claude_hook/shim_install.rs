//! Install PATH shims: small shell wrappers in `<session_dir>/bin/`
//! that `exec claude-hook shim <name> "$@"`.
//!
//! Ported from the retired Python shim installer. `claude-hook` is
//! resolved via PATH at exec time (not baked in) so the wrapper keeps
//! working across binary upgrades without restarting the session.

use claude_hook_config::GitShimConfig;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

pub const BASE_SHIM_NAMES: &[&str] = &["bazelisk", "bazel", "bb", "bbr"];
pub const SHIM_SESSION_ID_ENV: &str = "__DUCKTAPE_CLAUDE_HOOKS_SHIM_SESSION_ID";
pub const SHIM_DIR_ENV: &str = "__DUCKTAPE_CLAUDE_HOOKS_SHIM_DIR";
pub const GIT_BLOCK_ADD_ALL_ENV: &str = "__DUCKTAPE_CLAUDE_HOOKS_GIT_BLOCK_ADD_ALL";
pub const GIT_BLOCK_STASH_ENV: &str = "__DUCKTAPE_CLAUDE_HOOKS_GIT_BLOCK_STASH";
pub const GIT_BLOCK_AMEND_ENV: &str = "__DUCKTAPE_CLAUDE_HOOKS_GIT_BLOCK_AMEND";

fn bool_env(v: bool) -> &'static str {
    if v { "1" } else { "0" }
}

pub fn git_shim_enabled(git: &GitShimConfig) -> bool {
    git.block_add_all || git.block_stash || git.block_amend
}

pub fn install_shim(
    wrapper_dir: &Path,
    shim_name: &str,
    session_id: &str,
    git: Option<&GitShimConfig>,
) -> std::io::Result<()> {
    std::fs::create_dir_all(wrapper_dir)?;
    let path = wrapper_dir.join(shim_name);
    let mut lines = vec![
        "#!/bin/sh".to_string(),
        format!(
            "export {SHIM_SESSION_ID_ENV}={}",
            shlex::try_quote(session_id).unwrap()
        ),
        format!(
            "export {SHIM_DIR_ENV}={}",
            shlex::try_quote(&wrapper_dir.display().to_string()).unwrap()
        ),
    ];
    if let Some(git) = git {
        lines.push(format!(
            "export {GIT_BLOCK_ADD_ALL_ENV}={}",
            bool_env(git.block_add_all)
        ));
        lines.push(format!(
            "export {GIT_BLOCK_STASH_ENV}={}",
            bool_env(git.block_stash)
        ));
        lines.push(format!(
            "export {GIT_BLOCK_AMEND_ENV}={}",
            bool_env(git.block_amend)
        ));
    }
    lines.push(format!(
        "exec claude-hook shim {} \"$@\"",
        shlex::try_quote(shim_name).unwrap()
    ));
    let content = format!("{}\n", lines.join("\n"));
    std::fs::write(&path, content)?;
    let mut perms = std::fs::metadata(&path)?.permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms)?;
    Ok(())
}

pub fn install_all_shims(
    wrapper_dir: &Path,
    session_id: &str,
    git: &GitShimConfig,
) -> std::io::Result<()> {
    for name in BASE_SHIM_NAMES {
        install_shim(wrapper_dir, name, session_id, None)?;
    }
    if git_shim_enabled(git) {
        install_shim(wrapper_dir, "git", session_id, Some(git))?;
    } else {
        let git_path = wrapper_dir.join("git");
        if git_path.exists() {
            std::fs::remove_file(git_path)?;
        }
    }
    Ok(())
}

pub fn expected_shim_names(git: &GitShimConfig) -> Vec<&'static str> {
    let mut names = BASE_SHIM_NAMES.to_vec();
    if git_shim_enabled(git) {
        names.push("git");
    }
    names
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all_git_blocks() -> GitShimConfig {
        GitShimConfig {
            block_add_all: true,
            block_stash: true,
            block_amend: true,
        }
    }

    #[test]
    fn install_writes_executable_wrapper() {
        let tmp = tempfile::tempdir().unwrap();
        install_shim(tmp.path(), "git", "session-abc", Some(&all_git_blocks())).unwrap();

        let shim = tmp.path().join("git");
        let content = std::fs::read_to_string(&shim).unwrap();
        assert!(content.starts_with("#!/bin/sh\n"));
        assert!(content.contains("__DUCKTAPE_CLAUDE_HOOKS_SHIM_SESSION_ID=session-abc"));
        assert!(content.contains("__DUCKTAPE_CLAUDE_HOOKS_SHIM_DIR="));
        assert!(content.contains("__DUCKTAPE_CLAUDE_HOOKS_GIT_BLOCK_ADD_ALL=1"));
        assert!(content.contains("exec claude-hook shim git \"$@\""));

        let mode = std::fs::metadata(&shim).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o755);
    }

    #[test]
    fn install_all_shims_creates_base_names_and_opt_in_git() {
        let tmp = tempfile::tempdir().unwrap();
        install_all_shims(tmp.path(), "s1", &GitShimConfig::default()).unwrap();
        for name in BASE_SHIM_NAMES {
            assert!(tmp.path().join(name).exists(), "missing shim: {name}");
        }
        assert!(!tmp.path().join("git").exists());

        install_all_shims(tmp.path(), "s1", &all_git_blocks()).unwrap();
        assert!(tmp.path().join("git").exists());
    }

    #[test]
    fn install_all_shims_removes_stale_git_when_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        install_all_shims(tmp.path(), "s1", &all_git_blocks()).unwrap();
        assert!(tmp.path().join("git").exists());
        install_all_shims(tmp.path(), "s1", &GitShimConfig::default()).unwrap();
        assert!(!tmp.path().join("git").exists());
    }

    #[test]
    fn install_quotes_session_id_with_spaces() {
        let tmp = tempfile::tempdir().unwrap();
        install_shim(tmp.path(), "git", "weird id", Some(&all_git_blocks())).unwrap();
        let content = std::fs::read_to_string(tmp.path().join("git")).unwrap();
        assert!(content.contains("'weird id'"));
    }

    #[test]
    fn expected_names_include_git_only_when_enabled() {
        assert_eq!(
            expected_shim_names(&GitShimConfig::default()),
            vec!["bazelisk", "bazel", "bb", "bbr"]
        );
        assert_eq!(
            expected_shim_names(&GitShimConfig {
                block_add_all: true,
                ..Default::default()
            }),
            vec!["bazelisk", "bazel", "bb", "bbr", "git"]
        );
    }
}
