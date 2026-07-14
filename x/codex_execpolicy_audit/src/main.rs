//! codex-execpolicy-audit — replay the current codex execpolicy against the
//! commands recorded in `~/.codex/sessions` rollouts, to surface the coverage
//! gap (high-frequency commands the policy does NOT auto-allow → SSOT candidates).
mod audit;
mod rollout;
mod rules;

use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;

#[derive(Parser)]
#[command(about = "Replay codex execpolicy against rollout logs; report the coverage gap.")]
struct Cli {
    /// Codex home (contains the `sessions/` tree).
    #[arg(long, default_value = "~/.codex")]
    codex_home: String,
    /// Path to the `default.rules` file to evaluate.
    #[arg(long, default_value = "~/.codex/rules/default.rules")]
    rules: String,
    /// Optional: write the full ranked report as JSONL to this path.
    #[arg(long)]
    out: Option<PathBuf>,
    /// How many patterns to print to stdout (ranked: uncovered/dangerous first).
    #[arg(long, default_value_t = 40)]
    top: usize,
}

/// Expand a leading `~` to the user's home directory.
fn expand_tilde(p: &str) -> PathBuf {
    if let Some(rest) = p.strip_prefix("~/") {
        if let Some(home) = dirs::home_dir() {
            return home.join(rest);
        }
    }
    PathBuf::from(p)
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let home = expand_tilde(&cli.codex_home);
    let rules_path = expand_tilde(&cli.rules);

    let cmds = rollout::collect_cmds(&home)?;
    let rules = rules::load(&rules_path)?;

    eprintln!("loaded execpolicy rules from {}", rules_path.display());

    let report = audit::Report::run(&cmds, &rules);
    report.print_top(cli.top);

    if let Some(out) = &cli.out {
        report.write_jsonl(out)?;
        eprintln!("wrote full report to {}", out.display());
    }
    Ok(())
}
