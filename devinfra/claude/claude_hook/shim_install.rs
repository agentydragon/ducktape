//! Install PATH shims: two-line shell wrappers in `<session_dir>/bin/`
//! that `exec claude-hook shim <name> "$@"`.
//!
//! Ports `devinfra/claude/hook_daemon/shim_install.py`. `claude-hook` is
//! resolved via PATH at exec time (not baked in) so the wrapper keeps
//! working across binary upgrades without restarting the session.

use std::os::unix::fs::PermissionsExt;
use std::path::Path;

pub const SHIM_NAMES: &[&str] = &["bazelisk", "git", "bazel", "bb", "bbr"];
pub const SHIM_SESSION_ID_ENV: &str = "__DUCKTAPE_CLAUDE_HOOKS_SHIM_SESSION_ID";

pub fn install_shim(wrapper_dir: &Path, shim_name: &str, session_id: &str) -> std::io::Result<()> {
    std::fs::create_dir_all(wrapper_dir)?;
    let path = wrapper_dir.join(shim_name);
    let content = format!(
        "#!/bin/sh\nexport {SHIM_SESSION_ID_ENV}={}\nexec claude-hook shim {} \"$@\"\n",
        shlex::try_quote(session_id).unwrap(),
        shlex::try_quote(shim_name).unwrap()
    );
    std::fs::write(&path, content)?;
    let mut perms = std::fs::metadata(&path)?.permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms)?;
    Ok(())
}

pub fn install_all_shims(wrapper_dir: &Path, session_id: &str) -> std::io::Result<()> {
    for name in SHIM_NAMES {
        install_shim(wrapper_dir, name, session_id)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn install_writes_executable_wrapper() {
        let tmp = tempfile::tempdir().unwrap();
        install_shim(tmp.path(), "git", "session-abc").unwrap();

        let shim = tmp.path().join("git");
        let content = std::fs::read_to_string(&shim).unwrap();
        assert!(content.starts_with("#!/bin/sh\n"));
        assert!(content.contains("__DUCKTAPE_CLAUDE_HOOKS_SHIM_SESSION_ID=session-abc"));
        assert!(content.contains("exec claude-hook shim git \"$@\""));

        let mode = std::fs::metadata(&shim).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o755);
    }

    #[test]
    fn install_all_shims_creates_all_names() {
        let tmp = tempfile::tempdir().unwrap();
        install_all_shims(tmp.path(), "s1").unwrap();
        for name in SHIM_NAMES {
            assert!(tmp.path().join(name).exists(), "missing shim: {name}");
        }
    }

    #[test]
    fn install_quotes_session_id_with_spaces() {
        let tmp = tempfile::tempdir().unwrap();
        install_shim(tmp.path(), "git", "weird id").unwrap();
        let content = std::fs::read_to_string(tmp.path().join("git")).unwrap();
        assert!(content.contains("'weird id'"));
    }
}
