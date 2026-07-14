//! Classify each logged command against (a) the prefix rules and (b) codex's
//! trusted-command heuristic, then aggregate into a ranked gap report.
//!
//! Per leaf command (codex's `parse_shell_script` splits compound/piped strings
//! into leaves): **rule-allow** if a prefix rule matches, else **heuristic-allow**
//! if `is_known_safe_command`, else **dangerous** if `command_might_be_dangerous`,
//! else **uncovered** (the SSOT gap candidates).
//!
//! Fidelity caveat: this models rules + trusted-command heuristic, NOT the
//! sandbox-boundary decision (network / writes outside writable roots). So
//! "uncovered" means "not auto-allowed by rules/heuristic", not "would have
//! prompted" — the right level for SSOT triage.
use std::collections::HashMap;
use std::path::Path;

use anyhow::Result;
use serde::Serialize;
use serde_json;

use codex_protocol::parse_command::ParsedCommand;
use codex_shell_command::is_dangerous_command::command_might_be_dangerous;
use codex_shell_command::is_safe_command::is_known_safe_command;
use codex_shell_command::parse_command::parse_shell_script;

use crate::rules::PrefixRules;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Class {
    RuleAllow,
    HeuristicAllow,
    Dangerous,
    Uncovered,
}

impl Class {
    fn label(self) -> &'static str {
        match self {
            Class::RuleAllow => "rule-allow",
            Class::HeuristicAllow => "heuristic-allow",
            Class::Dangerous => "dangerous",
            Class::Uncovered => "UNCOVERED",
        }
    }
}

#[derive(Default)]
struct Agg {
    total: u64,
    by_class: [u64; 4],
    sample: String,
}

fn class_index(c: Class) -> usize {
    match c {
        Class::RuleAllow => 0,
        Class::HeuristicAllow => 1,
        Class::Dangerous => 2,
        Class::Uncovered => 3,
    }
}

fn classify(argv: &[String], rules: &PrefixRules) -> Class {
    if rules.matches(argv) {
        Class::RuleAllow
    } else if is_known_safe_command(argv) {
        Class::HeuristicAllow
    } else if command_might_be_dangerous(argv) {
        Class::Dangerous
    } else {
        Class::Uncovered
    }
}

/// Aggregation key: the first two argv tokens (executable + subcommand) — matches
/// the granularity of the SSOT prefix rules.
fn signature(argv: &[String]) -> String {
    match argv.len() {
        0 => "<empty>".to_string(),
        1 => argv[0].clone(),
        _ => format!("{} {}", argv[0], argv[1]),
    }
}

pub struct Report {
    rows: HashMap<String, Agg>,
    total_cmds: u64,
    total_leaves: u64,
    parse_failures: u64,
}

impl Report {
    pub fn run(cmds: &[String], rules: &PrefixRules) -> Self {
        let mut rows: HashMap<String, Agg> = HashMap::new();
        let mut total_leaves = 0u64;
        let mut parse_failures = 0u64;

        for cmd in cmds {
            let leaves = parse_shell_script(cmd);
            if leaves.is_empty() {
                parse_failures += 1;
            }
            for leaf in leaves {
                let leaf_cmd = match &leaf {
                    ParsedCommand::Read { cmd, .. }
                    | ParsedCommand::ListFiles { cmd, .. }
                    | ParsedCommand::Search { cmd, .. }
                    | ParsedCommand::Unknown { cmd } => cmd.clone(),
                };
                let Some(argv) = shlex::split(&leaf_cmd) else {
                    parse_failures += 1;
                    continue;
                };
                if argv.is_empty() {
                    continue;
                }
                let class = classify(&argv, rules);
                let key = signature(&argv);
                let agg = rows.entry(key).or_default();
                agg.total += 1;
                agg.by_class[class_index(class)] += 1;
                if agg.sample.is_empty() {
                    agg.sample = leaf_cmd;
                }
                total_leaves += 1;
            }
        }

        Self {
            rows,
            total_cmds: cmds.len() as u64,
            total_leaves,
            parse_failures,
        }
    }

    pub fn print_top(&self, n: usize) {
        println!(
            "commands: {}  leaves: {}  parse-failures: {}  distinct patterns: {}",
            self.total_cmds,
            self.total_leaves,
            self.parse_failures,
            self.rows.len()
        );
        let mut ranked: Vec<_> = self.rows.iter().collect();
        // Rank: uncovered/dangerous first (gap candidates), then by total count.
        ranked.sort_by(|a, b| {
            let gap = |agg: &Agg| {
                agg.by_class[class_index(Class::Uncovered)]
                    + agg.by_class[class_index(Class::Dangerous)]
            };
            gap(b.1)
                .cmp(&gap(a.1))
                .then_with(|| b.1.total.cmp(&a.1.total))
        });
        println!(
            "{:>6}  {:>6}  {:>6}  {:>6}  {:>14}  {}",
            "total", "rule", "heur", "dang", "class", "pattern (sample)"
        );
        for (key, agg) in ranked.into_iter().take(n) {
            let dominant = match (
                agg.by_class[class_index(Class::Uncovered)] > 0,
                agg.by_class[class_index(Class::Dangerous)] > 0,
                agg.by_class[class_index(Class::HeuristicAllow)] > 0,
            ) {
                (true, _, _) => Class::Uncovered,
                (_, true, _) => Class::Dangerous,
                (_, _, true) => Class::HeuristicAllow,
                _ => Class::RuleAllow,
            };
            println!(
                "{:>6}  {:>6}  {:>6}  {:>6}  {:>14}  {} — [{}]",
                agg.total,
                agg.by_class[class_index(Class::RuleAllow)],
                agg.by_class[class_index(Class::HeuristicAllow)],
                agg.by_class[class_index(Class::Dangerous)],
                dominant.label(),
                key,
                agg.sample
            );
        }
    }

    pub fn write_jsonl(&self, path: &Path) -> Result<()> {
        #[derive(Serialize)]
        struct Row<'a> {
            pattern: &'a str,
            total: u64,
            rule_allow: u64,
            heuristic_allow: u64,
            dangerous: u64,
            uncovered: u64,
            sample: &'a str,
        }
        let mut f = std::io::BufWriter::new(std::fs::File::create(path)?);
        let mut rows: Vec<_> = self.rows.iter().collect();
        rows.sort_by(|a, b| b.1.total.cmp(&a.1.total));
        for (pattern, agg) in rows {
            let row = Row {
                pattern,
                total: agg.total,
                rule_allow: agg.by_class[class_index(Class::RuleAllow)],
                heuristic_allow: agg.by_class[class_index(Class::HeuristicAllow)],
                dangerous: agg.by_class[class_index(Class::Dangerous)],
                uncovered: agg.by_class[class_index(Class::Uncovered)],
                sample: &agg.sample,
            };
            serde_json::to_writer(&mut f, &row)?;
            use std::io::Write;
            writeln!(f)?;
        }
        Ok(())
    }
}
