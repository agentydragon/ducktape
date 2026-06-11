//! Source a shell script and capture the env delta (the "env overlay").
//!
//! Runs `bash -c 'source "$1" >&2 && env -0' _ <script>`:
//! - `source "$1"` with positional arg avoids shell-injection via the script
//!   path (paths with spaces/metacharacters are fine).
//! - `>&2` redirects the script's stdout to stderr so it doesn't mix with
//!   the `env -0` output on stdout.
//! - `env -0` writes NUL-delimited `KEY=VALUE` records.
//!
//! We diff the parsed output against the initial env; the diff (new or
//! changed vars) is the overlay. No mutation of the daemon's own env.

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

#[derive(Debug, Default)]
pub struct StartupResult {
    pub env_overlay: HashMap<String, String>,
    pub exit_code: Option<i32>,
    pub output: String,
}

/// Parse `env -0` output (NUL-delimited `KEY=VALUE`) into a dict.
pub fn parse_env_null_delimited(raw: &[u8]) -> HashMap<String, String> {
    let mut result = HashMap::new();
    for item in raw.split(|&b| b == 0) {
        if item.is_empty() {
            continue;
        }
        let Some(eq) = item.iter().position(|&b| b == b'=') else {
            continue;
        };
        let key = String::from_utf8_lossy(&item[..eq]).into_owned();
        let val = String::from_utf8_lossy(&item[eq + 1..]).into_owned();
        result.insert(key, val);
    }
    result
}

pub fn source_env_script(script_path: &Path, extra_env: &HashMap<String, String>) -> StartupResult {
    if !script_path.exists() {
        return StartupResult {
            exit_code: Some(1),
            output: format!("startup_env_script not found: {}", script_path.display()),
            ..Default::default()
        };
    }

    // Initial env = daemon's env + extra_env (profile-level flags). The overlay
    // is what the script ADDED on top of this baseline.
    let mut initial_env: HashMap<String, String> = std::env::vars().collect();
    for (k, v) in extra_env {
        initial_env.insert(k.clone(), v.clone());
    }

    let mut cmd = Command::new("bash");
    cmd.arg("-c")
        .arg(r#"source "$1" >&2 && env -0"#)
        .arg("_") // $0
        .arg(script_path)
        .env_clear();
    for (k, v) in &initial_env {
        cmd.env(k, v);
    }

    let output = match cmd.output() {
        Ok(o) => o,
        Err(e) => {
            return StartupResult {
                exit_code: Some(1),
                output: format!("bash invocation failed: {e}"),
                ..Default::default()
            };
        }
    };

    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    let exit_code = output.status.code();

    if !output.status.success() {
        return StartupResult {
            exit_code,
            output: stderr,
            env_overlay: HashMap::new(),
        };
    }

    let after = parse_env_null_delimited(&output.stdout);
    let overlay: HashMap<String, String> = after
        .into_iter()
        .filter(|(k, v)| initial_env.get(k) != Some(v))
        .collect();

    StartupResult {
        env_overlay: overlay,
        exit_code,
        output: stderr,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_env_null_delimited_basic() {
        let raw = b"FOO=bar\0BAZ=qux\0";
        let m = parse_env_null_delimited(raw);
        assert_eq!(m.get("FOO").unwrap(), "bar");
        assert_eq!(m.get("BAZ").unwrap(), "qux");
        assert_eq!(m.len(), 2);
    }

    #[test]
    fn parse_env_null_delimited_with_newline_in_value() {
        // Values can contain newlines; only NUL separates records.
        let raw = b"FOO=line1\nline2\0BAR=x\0";
        let m = parse_env_null_delimited(raw);
        assert_eq!(m.get("FOO").unwrap(), "line1\nline2");
        assert_eq!(m.get("BAR").unwrap(), "x");
    }

    #[test]
    fn parse_env_null_delimited_with_equals_in_value() {
        let raw = b"FOO=a=b=c\0";
        let m = parse_env_null_delimited(raw);
        assert_eq!(m.get("FOO").unwrap(), "a=b=c");
    }

    #[test]
    fn source_script_missing_file() {
        let r = source_env_script(Path::new("/tmp/does-not-exist-xyz"), &HashMap::new());
        assert_eq!(r.exit_code, Some(1));
        assert!(r.env_overlay.is_empty());
    }

    #[test]
    fn source_script_captures_overlay() {
        use std::io::Write;
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        writeln!(tmp, "export MY_OVERLAY_VAR=hello").unwrap();
        writeln!(tmp, "export PATH=\"/overridden:$PATH\"").unwrap();
        tmp.flush().unwrap();

        let r = source_env_script(tmp.path(), &HashMap::new());
        assert_eq!(r.exit_code, Some(0));
        assert_eq!(r.env_overlay.get("MY_OVERLAY_VAR").unwrap(), "hello");
        // PATH was modified → should be in overlay.
        assert!(r.env_overlay.contains_key("PATH"));
    }

    #[test]
    fn extra_env_not_in_overlay() {
        use std::io::Write;
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        // Script doesn't touch FROM_PROFILE; it comes from extra_env so should
        // NOT appear in the overlay.
        writeln!(tmp, "export ADDED_BY_SCRIPT=yes").unwrap();
        tmp.flush().unwrap();

        let mut extra = HashMap::new();
        extra.insert("FROM_PROFILE".to_string(), "1".to_string());
        let r = source_env_script(tmp.path(), &extra);
        assert_eq!(r.exit_code, Some(0));
        assert!(r.env_overlay.contains_key("ADDED_BY_SCRIPT"));
        assert!(!r.env_overlay.contains_key("FROM_PROFILE"));
    }
}
