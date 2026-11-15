//! Report-only: existing baseline rows are measurements, never proof inputs.
use anyhow::{Context, Result};
use clap::Parser;
use std::{collections::BTreeSet, path::PathBuf};
use summary_effects::publication::{Candidate, SourceIndex, Violation, positions};

#[derive(Parser)]
struct Cli {
    #[arg(long)]
    chunk: PathBuf,
    #[arg(long)]
    effect_report: PathBuf,
    #[arg(long)]
    modules: PathBuf,
    #[arg(long)]
    optimistic_pairs: PathBuf,
    #[arg(long)]
    conservative_pairs: PathBuf,
    #[arg(long)]
    out: PathBuf,
}
#[derive(serde::Deserialize)]
struct Schedule {
    emitted_order: Vec<usize>,
}
#[derive(serde::Serialize)]
struct CheckedCandidate {
    #[serde(flatten)]
    candidate: Candidate,
    fence_count: usize,
    active: bool,
    violations: Vec<Violation>,
}
#[derive(serde::Serialize)]
struct Pair {
    earlier: usize,
    later: usize,
    baseline_class: String,
}
#[derive(serde::Serialize)]
struct Comparison {
    baseline_pairs: usize,
    discharged_by_satisfied_private_certificate: Vec<Pair>,
    remaining_pairs: usize,
}
#[derive(serde::Serialize)]
struct Report {
    source_units: usize,
    unique_full_ordinal_permutation: bool,
    inversions: usize,
    modules: usize,
    candidates: Vec<CheckedCandidate>,
    optimistic: Comparison,
    conservative: Comparison,
    whole_chunk_proved: bool,
}
fn compare(
    path: &std::path::Path,
    active: &BTreeSet<usize>,
    positions: &[usize],
) -> Result<Comparison> {
    let source = std::fs::read_to_string(path)?;
    let mut baseline_pairs = 0;
    let mut discharged = Vec::new();
    let mut seen = BTreeSet::new();
    for line in source.lines() {
        let fields: Vec<&str> = line.split('\t').collect();
        anyhow::ensure!(fields.len() == 3, "unexpected baseline row {line}");
        let (earlier, later): (usize, usize) = (fields[0].parse()?, fields[1].parse()?);
        anyhow::ensure!(
            earlier < later && later < positions.len(),
            "invalid baseline ordinals"
        );
        anyhow::ensure!(
            positions[earlier] > positions[later],
            "baseline pair is not inverted"
        );
        anyhow::ensure!(seen.insert((earlier, later)), "duplicate baseline pair");
        baseline_pairs += 1;
        if active.contains(&earlier) || active.contains(&later) {
            discharged.push(Pair {
                earlier,
                later,
                baseline_class: fields[2].to_string(),
            });
        }
    }
    Ok(Comparison {
        baseline_pairs,
        remaining_pairs: baseline_pairs - discharged.len(),
        discharged_by_satisfied_private_certificate: discharged,
    })
}
fn run(cli: Cli) -> Result<()> {
    let source = std::fs::read_to_string(&cli.chunk)?;
    let parsed = js_ast::parse_js_module(&cli.chunk.display().to_string(), &source)?;
    let index = SourceIndex::new(&parsed.module);
    let schedule: Schedule = serde_json::from_slice(&std::fs::read(&cli.effect_report)?)?;
    let positions = positions(&schedule.emitted_order, index.unit_count())?;
    // Raw external pipeline input; ownership is used only for reporting its count.
    let modules_json = serde_json::from_slice(&std::fs::read(&cli.modules)?)?;
    let modules = summary_effects::orders::Modules::parse(&modules_json, index.unit_count())?;
    let mut active = BTreeSet::new();
    let candidates = index
        .certificates()
        .into_iter()
        .map(|candidate| {
            let violations = candidate.violations(&positions);
            let satisfied = candidate.refusal.is_none() && violations.is_empty();
            if satisfied {
                active.insert(candidate.init);
            }
            CheckedCandidate {
                fence_count: candidate.fence_count(),
                active: satisfied,
                candidate,
                violations,
            }
        })
        .collect();
    let report = Report {
        source_units: index.unit_count(),
        unique_full_ordinal_permutation: true,
        inversions: (0..positions.len())
            .map(|a| {
                (a + 1..positions.len())
                    .filter(|b| positions[a] > positions[*b])
                    .count()
            })
            .sum(),
        modules: modules.count,
        candidates,
        optimistic: compare(&cli.optimistic_pairs, &active, &positions)?,
        conservative: compare(&cli.conservative_pairs, &active, &positions)?,
        whole_chunk_proved: false,
    };
    std::fs::write(&cli.out, serde_json::to_string_pretty(&report)?)
        .with_context(|| format!("writing {}", cli.out.display()))?;
    println!(
        "units={} inversions={} active_certificates={} optimistic_remaining={} conservative_remaining={} whole_chunk_proved=false",
        report.source_units,
        report.inversions,
        active.len(),
        report.optimistic.remaining_pairs,
        report.conservative.remaining_pairs
    );
    Ok(())
}
fn main() -> Result<()> {
    js_ast::with_swc_globals(|| run(Cli::parse()))
}
