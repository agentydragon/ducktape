//! Load the codex execpolicy `default.rules` via codex's real Starlark engine
//! (`codex_execpolicy`) — no reimplementation. The file is a Starlark program
//! calling `prefix_rule`/`exact_rule`/`host_executable` primitives; the engine
//! parses and evaluates it into a `Policy`. This is faithful to any hand-written
//! rules, not just the SSOT-generated prefix-allow lines.
use anyhow::{Context, Result};
use codex_execpolicy::{Policy, PolicyParser};
use std::path::Path;

pub struct Rules(pub Policy);

pub fn load(path: &Path) -> Result<Rules> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("read rules file {}", path.display()))?;
    let mut parser = PolicyParser::new();
    parser
        .parse(&path.display().to_string(), &text)
        .with_context(|| format!("parse rules file {}", path.display()))?;
    Ok(Rules(parser.build()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_execpolicy::{Decision, MatchOptions};

    fn policy_from(text: &str) -> Policy {
        let tmp = std::env::temp_dir().join("codex_execpolicy_audit_rules_test.txt");
        std::fs::write(&tmp, text).unwrap();
        let r = load(&tmp).unwrap();
        let _ = std::fs::remove_file(&tmp);
        r.0
    }

    fn allows(policy: &Policy, argv: &[&str]) -> bool {
        let argv: Vec<String> = argv.iter().map(|s| (*s).to_string()).collect();
        policy
            .matches_for_command_with_options(
                &argv,
                None,
                &MatchOptions {
                    resolve_host_executables: false,
                },
            )
            .iter()
            .any(|m| m.decision() == Decision::Allow)
    }

    #[test]
    fn prefix_match_ignores_trailing_args() {
        let p = policy_from("prefix_rule(pattern=[\"git\", \"status\"], decision=\"allow\")\n");
        assert!(allows(&p, &["git", "status", "--short"]));
        assert!(allows(&p, &["git", "status"]));
        assert!(!allows(&p, &["git", "diff"]));
        assert!(!allows(&p, &["git"]));
    }

    #[test]
    fn parses_allow_rules_and_skips_non_allow() {
        let text = r#"
prefix_rule(pattern=["git", "status"], decision="allow")
prefix_rule(pattern=["rm"], decision="forbidden")
prefix_rule(pattern=["bbr", "test"], decision="allow")
"#;
        let p = policy_from(text);
        assert!(allows(&p, &["bbr", "test", "//foo"]));
        assert!(!allows(&p, &["rm", "-rf", "/"]));
    }
}
