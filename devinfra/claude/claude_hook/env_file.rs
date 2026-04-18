//! Write the session env file (`CLAUDE_ENV_FILE`) — the single write point
//! for all env vars the agent's Bash tool sees.
//!
//! Ports `devinfra/claude/env_file.py::write_env_file`. Order matters:
//! secrets and `env_exports` from the profile are written BEFORE the final
//! `PATH="<shims_dir>:$PATH"` prepend, so the shims dir always wins even if
//! the profile or overlay mangled PATH.

use std::collections::HashMap;
use std::path::Path;

pub fn write_env_file(
    env_file: &Path,
    shims_dir: &Path,
    env_overlay: &HashMap<String, String>,
    extra_env_script: Option<&str>,
) {
    let mut lines: Vec<String> = vec!["# Environment configured by session start hook".to_string()];

    if !env_overlay.is_empty() {
        lines.push(String::new());
        lines.push("# Secrets from startup_env_script".to_string());
        // Sort for determinism in tests + diffs.
        let mut keys: Vec<&String> = env_overlay.keys().collect();
        keys.sort();
        for k in keys {
            lines.push(format!(
                "export {k}={}",
                shlex::try_quote(&env_overlay[k]).unwrap()
            ));
        }
    }

    if let Some(extra) = extra_env_script {
        let trimmed = extra.trim_end();
        if !trimmed.is_empty() {
            lines.push(String::new());
            lines.push("# Extra env script from profile".to_string());
            lines.push(trimmed.to_string());
        }
    }

    // Session bin dir must be first on PATH regardless of what env_overlay or
    // extra_env_script did above. Emit this last so it always wins.
    lines.push(String::new());
    lines.push(format!("export PATH=\"{}:$PATH\"", shims_dir.display()));

    let content = format!("{}\n", lines.join("\n"));

    if let Some(parent) = env_file.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            eprintln!("env_file: failed to create parent dir: {e}");
        }
    }

    // Atomic write via tempfile + persist; set 0o600 since the file
    // contains decrypted secrets.
    use std::os::unix::fs::PermissionsExt;
    let mut tmp =
        tempfile::NamedTempFile::new_in(env_file.parent().unwrap()).expect("create env tmp");
    std::io::Write::write_all(&mut tmp, content.as_bytes()).expect("write env tmp");
    if let Err(e) = tmp
        .as_file()
        .set_permissions(std::fs::Permissions::from_mode(0o600))
    {
        eprintln!("env_file: failed to set permissions: {e}");
    }
    tmp.persist(env_file).expect("persist env file");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_env_file_basic() {
        let tmp = tempfile::tempdir().unwrap();
        let env_file = tmp.path().join("env.sh");
        let shims_dir = tmp.path().join("bin");
        let mut overlay = HashMap::new();
        overlay.insert("FOO".to_string(), "bar".to_string());
        overlay.insert("GITHUB_TOKEN".to_string(), "secret".to_string());

        write_env_file(&env_file, &shims_dir, &overlay, Some("export EXTRA=x\n"));

        let content = std::fs::read_to_string(&env_file).unwrap();
        assert!(content.contains("export FOO=bar"));
        assert!(content.contains("export GITHUB_TOKEN=secret"));
        assert!(content.contains("export EXTRA=x"));
        // Final PATH export always wins.
        let last_line = content.lines().last().unwrap();
        assert_eq!(
            last_line,
            &format!("export PATH=\"{}:$PATH\"", shims_dir.display())
        );
    }

    #[test]
    fn write_env_file_no_overlay() {
        let tmp = tempfile::tempdir().unwrap();
        let env_file = tmp.path().join("env.sh");
        let shims_dir = tmp.path().join("bin");
        write_env_file(&env_file, &shims_dir, &HashMap::new(), None);

        let content = std::fs::read_to_string(&env_file).unwrap();
        assert!(content.contains("export PATH="));
    }
}
