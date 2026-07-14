//! Parse `prefix_rule(pattern=[...], decision="allow")` entries from a codex
//! `default.rules` file. The SSOT generator (`nix/home/codex/execpolicy-rules.nix`)
//! emits only prefix `allow` rules, so a trivial prefix matcher is faithful and
//! the Starlark engine isn't needed.
use anyhow::{Context, Result};
use regex::Regex;
use serde_json;
use std::path::Path;

#[derive(Debug, Clone)]
pub struct PrefixRule {
    pub pattern: Vec<String>,
}

pub struct PrefixRules {
    pub rules: Vec<PrefixRule>,
}

impl PrefixRules {
    /// True if `argv` begins with any rule's pattern tokens.
    pub fn matches(&self, argv: &[String]) -> bool {
        self.rules
            .iter()
            .any(|r| argv.len() >= r.pattern.len() && argv[..r.pattern.len()] == r.pattern[..])
    }
}

pub fn load(path: &Path) -> Result<PrefixRules> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("read rules file {}", path.display()))?;
    let re = Regex::new(
        r#"prefix_rule\(\s*pattern\s*=\s*(\[[^\]]*\])\s*,\s*decision\s*=\s*"([a-z]+)""#,
    )?;
    let mut rules = Vec::new();
    for cap in re.captures_iter(&text) {
        if &cap[2] != "allow" {
            continue; // SSOT emits only allow; ignore prompt/forbidden if ever present.
        }
        let pattern: Vec<String> =
            serde_json::from_str(&cap[1]).with_context(|| format!("parse pattern {}", &cap[1]))?;
        rules.push(PrefixRule { pattern });
    }
    Ok(PrefixRules { rules })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules(patterns: &[&[&str]]) -> PrefixRules {
        PrefixRules {
            rules: patterns
                .iter()
                .map(|p| PrefixRule {
                    pattern: p.iter().map(|s| (*s).to_string()).collect(),
                })
                .collect(),
        }
    }

    #[test]
    fn prefix_match_ignores_trailing_args() {
        let r = rules(&[&["git", "status"]]);
        assert!(r.matches(&["git".into(), "status".into(), "--short".into()]));
        assert!(r.matches(&["git".into(), "status".into()]));
        assert!(!r.matches(&["git".into(), "diff".into()]));
        assert!(!r.matches(&["git".into()]));
    }

    #[test]
    fn parses_prefix_allow_rules_and_skips_non_allow() {
        let text = r#"
# comment
prefix_rule(pattern=["git", "status"], decision="allow")
prefix_rule(pattern=["rm"], decision="forbidden")
prefix_rule(pattern=["bbr", "test"], decision="allow")
"#;
        let tmp = std::env::temp_dir().join("codex_execpolicy_audit_rules_test.txt");
        std::fs::write(&tmp, text).unwrap();
        let r = load(&tmp).unwrap();
        let _ = std::fs::remove_file(&tmp);
        assert_eq!(r.rules.len(), 2, "only allow rules are kept");
        assert!(r.matches(&["bbr".into(), "test".into(), "//foo".into()]));
        assert!(!r.matches(&["rm".into(), "-rf".into(), "/".into()]));
    }
}
