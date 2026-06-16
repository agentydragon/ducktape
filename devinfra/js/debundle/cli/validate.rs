//! `debundle spec validate --keep-going` — one keep-going pass over every
//! selector in a spec that emits a machine-readable report of every selector
//! problem instead of stopping at the first failing selector.
//!
//! The keep-going classification itself lives in the materialize pass
//! (`lowering::materialize::plan_builder`), which already writes a per-chunk
//! `selector_diagnostics.json` under [`ReportEmission::OnRejection`]. This
//! verb is a thin frontend: it runs the dry-run keep-going pipeline with
//! reports forced into a capture directory, reads the per-chunk reports back
//! through the shared [`SelectorDiagnosticsReport`] contract, and re-emits a
//! combined report on stdout in the standard `--format text|json|ndjson`
//! convention.

use std::path::Path;

use anyhow::{Context, Result};
use clap::Args as ClapArgs;
use output_layout::SELECTOR_DIAGNOSTICS_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli_with_options};
use selector_diagnostics::SelectorDiagnosticsReport;
use serde::Serialize;

/// Args for `debundle spec validate`. The spec source and package-root flags
/// mirror `debundle run` (`--spec` / `--tree-config` + roots) so the same
/// inputs validate and run.
#[derive(Debug, ClapArgs)]
pub struct ValidateArgs {
    // The flattened `--spec` / `--tree-config` / `--package-root` / `--keep-going`
    // / `--fail-fast` flags. Keep-going is the default; pass `--fail-fast` to
    // stop at the first supported failure instead of collecting every problem.
    #[command(flatten)]
    pub transform: TransformArgs,

    /// Output format. Default `text` on tty, `json` on pipe. `ndjson` emits one
    /// JSON object per diagnostic plus a final `summary` line.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Combined keep-going report across every chunk the spec materializes.
#[derive(Debug, Serialize)]
pub struct ValidateReport {
    /// Per-failure-class totals summed across all chunks.
    pub counts: std::collections::BTreeMap<String, usize>,
    /// Total selector problems found.
    pub total: usize,
    /// Per-chunk reports, sorted by chunk id. A chunk with no selector
    /// problems contributes no entry.
    pub chunks: Vec<SelectorDiagnosticsReport>,
}

pub fn run_validate_cmd(args: ValidateArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    let keep_going = !args.transform.fail_fast;
    let cli = args.transform.resolve()?;

    // Force reports into a private capture dir: dry-run + a report dir makes
    // the materialize pass emit `selector_diagnostics.json` per chunk on
    // rejection, independent of how the spec configures `report_out_dir`.
    let capture = tempfile::tempdir().context("creating selector-diagnostics capture dir")?;
    // The keep-going pass writes the per-chunk diagnostics *and then* fails the
    // pipeline at the end with the collected findings — that rejection is the
    // contract for `debundle run`. `validate` treats the findings as data, not a
    // tool failure: when the run produced reports, we emit them and exit zero;
    // only a run that errored *without* producing any report is a real failure
    // (bad spec path, parse error, …).
    let pass = run_transform_cli_with_options(
        &cli,
        TransformRunOptions {
            dry_run: true,
            keep_going,
            report_dir_override: Some(capture.path().to_path_buf()),
        },
    );

    let mut chunks = collect_chunk_reports(capture.path())?;
    if let Err(error) = pass
        && chunks.is_empty()
    {
        return Err(error).context("running keep-going validation pass");
    }
    chunks.sort_by(|a, b| a.chunk_id.cmp(&b.chunk_id));

    let mut counts = std::collections::BTreeMap::new();
    for chunk in &chunks {
        for (category, count) in &chunk.counts {
            *counts.entry(category.clone()).or_insert(0) += count;
        }
    }
    let total = counts.values().sum();
    let report = ValidateReport {
        counts,
        total,
        chunks,
    };

    if format == OutputFormat::Ndjson {
        emit_validate_ndjson(&report)?;
        return Ok(());
    }
    print_report(&report, format, render_validate_text).context("writing validate output")
}

/// Recursively gather every `selector_diagnostics.json` under the capture
/// directory. The materialize pass nests each report at
/// `<capture>/<chunk_id parts>/selector_diagnostics.json`.
fn collect_chunk_reports(capture: &Path) -> Result<Vec<SelectorDiagnosticsReport>> {
    let mut reports = Vec::new();
    collect_chunk_reports_into(capture, &mut reports)?;
    Ok(reports)
}

fn collect_chunk_reports_into(
    dir: &Path,
    reports: &mut Vec<SelectorDiagnosticsReport>,
) -> Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    let mut entries = std::fs::read_dir(dir)
        .with_context(|| format!("reading {}", dir.display()))?
        .collect::<std::io::Result<Vec<_>>>()
        .with_context(|| format!("reading {}", dir.display()))?;
    entries.sort_by_key(std::fs::DirEntry::path);
    for entry in entries {
        let path = entry.path();
        if path.is_dir() {
            collect_chunk_reports_into(&path, reports)?;
        } else if path.file_name().and_then(|name| name.to_str())
            == Some(SELECTOR_DIAGNOSTICS_REPORT)
        {
            let text = std::fs::read_to_string(&path)
                .with_context(|| format!("reading {}", path.display()))?;
            reports.push(
                serde_json::from_str(&text)
                    .with_context(|| format!("parsing selector diagnostics {}", path.display()))?,
            );
        }
    }
    Ok(())
}

fn render_validate_text(report: &ValidateReport, buf: &mut String) {
    use std::fmt::Write;

    if report.total == 0 {
        buf.push_str("No selector problems found.");
        return;
    }
    let summary = report
        .counts
        .iter()
        .map(|(category, count)| format!("{category}={count}"))
        .collect::<Vec<_>>()
        .join(", ");
    let _ = writeln!(
        buf,
        "{} selector problem(s) across {} chunk(s): {summary}",
        report.total,
        report.chunks.len(),
    );
    for chunk in &report.chunks {
        let _ = writeln!(buf, "\nchunk {}:", chunk.chunk_id);
        for diagnostic in &chunk.diagnostics {
            let export = diagnostic.export_name.as_deref().unwrap_or("-");
            let module = diagnostic
                .module_path
                .as_deref()
                .unwrap_or(&diagnostic.module_id);
            let _ = writeln!(
                buf,
                "  [{}] {module} as `{export}` ({}): {}",
                diagnostic.category, diagnostic.selector_kind, diagnostic.message,
            );
            let _ = writeln!(buf, "    -> {}", diagnostic.recommended_next_action);
        }
    }
}

/// One JSON object per diagnostic tagged with its chunk, then a final
/// `summary` line — the streaming shape `jq -c` consumers dispatch on.
fn emit_validate_ndjson(report: &ValidateReport) -> Result<()> {
    #[derive(Serialize)]
    struct DiagnosticLine<'a> {
        section: &'a str,
        chunk_id: &'a str,
        #[serde(flatten)]
        diagnostic: &'a selector_diagnostics::SelectorDiagnosticEntry,
    }
    #[derive(Serialize)]
    struct SummaryLine<'a> {
        section: &'a str,
        total: usize,
        counts: &'a std::collections::BTreeMap<String, usize>,
    }
    for chunk in &report.chunks {
        for diagnostic in &chunk.diagnostics {
            println!(
                "{}",
                serde_json::to_string(&DiagnosticLine {
                    section: "diagnostic",
                    chunk_id: &chunk.chunk_id,
                    diagnostic,
                })?
            );
        }
    }
    println!(
        "{}",
        serde_json::to_string(&SummaryLine {
            section: "summary",
            total: report.total,
            counts: &report.counts,
        })?
    );
    Ok(())
}
