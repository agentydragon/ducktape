//! Profile YAML configuration.
//!
//! The profile is **repo-generic**: unknown fields are ignored so
//! repo-specific keys (k8s, bazel_bes_proxy, setup_docker, pre_commit, etc.)
//! don't break parsing. They're consumed by the repo's scripts, not the
//! daemon.

use serde::Deserialize;

#[derive(Debug, Default, Deserialize, Clone)]
pub struct ProfileConfig {
    /// Repo-relative path to a shell script sourced once at daemon startup.
    /// The env delta (new/changed vars) becomes the env overlay, applied
    /// deliberately to the session env file + background commands.
    #[serde(default)]
    pub startup_env_script: Option<String>,

    /// Inline shell appended verbatim to the session env file.
    #[serde(default)]
    pub env_exports: Option<String>,

    /// Shell commands to run in the background during session start.
    #[serde(default)]
    pub background_commands: Vec<BackgroundCommand>,

    /// Enable idle watchdog (SIGTERM after N seconds of inactivity).
    #[serde(default)]
    pub idle_watchdog: bool,

    /// Repo-relative path to a Tera template for the session context banner.
    #[serde(default)]
    pub context_template: Option<String>,

    /// Git shim behavior toggles (block dangerous git commands).
    #[serde(default)]
    pub git_shim: GitShimConfig,
}

#[derive(Debug, Deserialize, Clone)]
pub struct BackgroundCommand {
    pub name: String,
    pub command: String,
    #[serde(default = "default_timeout")]
    pub timeout: u64,
    /// If true, delay until after the session env file is written and source
    /// it before running. Otherwise run immediately.
    #[serde(default)]
    pub after_env: bool,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub struct GitShimConfig {
    #[serde(default)]
    pub block_add_all: bool,
    #[serde(default)]
    pub block_stash: bool,
    #[serde(default)]
    pub block_amend: bool,
}

fn default_timeout() -> u64 {
    300
}

impl ProfileConfig {
    pub fn load(path: &std::path::Path) -> Result<Self, String> {
        let raw =
            std::fs::read_to_string(path).map_err(|e| format!("read {}: {e}", path.display()))?;
        serde_yaml::from_str(&raw).map_err(|e| format!("parse {}: {e}", path.display()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_minimal() {
        let yaml = "idle_watchdog: false\n";
        let cfg: ProfileConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(!cfg.idle_watchdog);
        assert!(cfg.background_commands.is_empty());
    }

    #[test]
    fn parse_with_unknown_fields_ignored() {
        let yaml = r#"
startup_env_script: devinfra/secrets/web_env.sh
env_exports: |
  export FOO=bar
idle_watchdog: false
k8s:
  server: https://api.example
  service_account: sa
  namespace: ns
bazel_bes_proxy:
  target: remote.buildbuddy.io:443
setup_docker: true
pre_commit:
  auto_apply_hooks: [ruff-format]
background_commands:
  - name: hello
    command: echo hi
    timeout: 10
    after_env: true
"#;
        let cfg: ProfileConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(
            cfg.startup_env_script.as_deref(),
            Some("devinfra/secrets/web_env.sh")
        );
        assert!(cfg.env_exports.is_some());
        assert_eq!(cfg.background_commands.len(), 1);
        assert_eq!(cfg.background_commands[0].name, "hello");
        assert!(cfg.background_commands[0].after_env);
        assert_eq!(cfg.background_commands[0].timeout, 10);
    }

    #[test]
    fn background_command_default_timeout() {
        let yaml = r#"
background_commands:
  - name: x
    command: echo x
"#;
        let cfg: ProfileConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(cfg.background_commands[0].timeout, 300);
        assert!(!cfg.background_commands[0].after_env);
    }

    #[test]
    fn git_shim_defaults_to_all_false() {
        let cfg: ProfileConfig = serde_yaml::from_str("").unwrap();
        assert!(!cfg.git_shim.block_add_all);
        assert!(!cfg.git_shim.block_stash);
        assert!(!cfg.git_shim.block_amend);
    }

    #[test]
    fn git_shim_parses() {
        let yaml = r#"
git_shim:
  block_add_all: true
  block_stash: true
"#;
        let cfg: ProfileConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(cfg.git_shim.block_add_all);
        assert!(cfg.git_shim.block_stash);
        assert!(!cfg.git_shim.block_amend);
    }
}
