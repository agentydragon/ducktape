//! Explicit-range selector comparisons, separate from joint spec resolution.

use std::collections::BTreeSet;
use std::fmt::Write;
use std::path::PathBuf;

use anyhow::{Result, bail};
use serde::Serialize;
use source_inspection::{PreparedSource, StatementRange, StatementRow};
use source_match::{
    ParsedSourceMatchSelector, RangeExplanation, RangeExplanationStatus, explain_range,
    template_free_identifiers,
};

use crate::selector_input::{SelectedSelector, inline_selector, load_selectors};
use selector_codemod::source_input::resolve_chunk_source_file;

pub(super) struct ExplanationConfig {
    pub source_file: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub match_source: Option<String>,
    pub selector: Option<String>,
    pub spec_file: Option<PathBuf>,
    pub target_binding: Option<String>,
    pub anonymous: bool,
    pub statements: StatementRange,
}

#[derive(Serialize)]
pub(super) struct ExplanationReport {
    source_file: PathBuf,
    statements: Vec<StatementRow>,
    comparisons: Vec<Comparison>,
}

#[derive(Serialize)]
struct Comparison {
    selector: SelectedSelector,
    /// Free identifier identity and references to other exports are not resolved.
    external_identifiers: BTreeSet<String>,
    /// Source indices use the original inspect-source statement numbering.
    result: RangeExplanation,
}

pub(super) fn explain(config: &ExplanationConfig) -> Result<ExplanationReport> {
    js_ast::with_swc_globals(|| {
        let source_file = resolve_chunk_source_file(
            config.source_file.as_deref(),
            config.source_root.as_deref(),
            config.chunk.as_deref(),
        )?;
        let source = PreparedSource::load(&source_file)?;
        let statements = source.statement_rows(Some(config.statements))?;
        let selected = &source.module().body[config.statements.start..=config.statements.end];
        let selectors = match (&config.match_source, &config.selector) {
            (Some(pattern), None) => vec![inline_selector(
                pattern.clone(),
                config.target_binding.clone(),
                config.anonymous,
            )],
            (None, Some(address)) => load_selectors(
                address,
                config.spec_file.as_deref(),
                config.target_binding.as_deref(),
            )?,
            _ => bail!("choose exactly one of --match or --selector"),
        };
        let mut comparisons = Vec::new();
        for selector in selectors {
            let parsed = ParsedSourceMatchSelector::parse(
                &selector.origin,
                format!("<{}>", selector.origin),
                &selector.selector(),
                "source_match",
            )?;
            let external_identifiers = template_free_identifiers(&parsed);
            let mut result = explain_range(&parsed, selected, selector.kind);
            for index in result.aligned_statements.iter_mut().flatten() {
                *index += config.statements.start;
            }
            if let Some(failure) = &mut result.failure {
                failure.source_statement_index += config.statements.start;
            }
            comparisons.push(Comparison {
                selector,
                external_identifiers,
                result,
            });
        }
        Ok(ExplanationReport {
            source_file,
            statements,
            comparisons,
        })
    })
}

pub(super) fn render(report: &ExplanationReport, out: &mut String) {
    let _ = writeln!(
        out,
        "Local structural comparison: ownership, uniqueness, and external reference constraints are not checked."
    );
    let _ = writeln!(out, "Source: {}", report.source_file.display());
    for row in &report.statements {
        let _ = writeln!(
            out,
            "[{}] {}:{}–{}:{}",
            row.index, row.start_line, row.start_column, row.end_line, row.end_column
        );
        for line in row.pretty_source.lines() {
            let _ = writeln!(out, "    {line}");
        }
    }
    for comparison in &report.comparisons {
        let selector = &comparison.selector;
        let _ = writeln!(
            out,
            "Selector: {} ({:?}, {:?}, target: {})",
            selector.origin,
            selector.kind,
            selector.identifiers,
            selector.target_binding.as_deref().unwrap_or("<inferred>")
        );
        for line in selector.match_source.lines() {
            let _ = writeln!(out, "    {line}");
        }
        if !comparison.external_identifiers.is_empty() {
            let names = comparison
                .external_identifiers
                .iter()
                .cloned()
                .collect::<Vec<_>>()
                .join(", ");
            let _ = writeln!(out, "External identifiers (identity unchecked): {names}");
        }
        render_result(&comparison.result, out);
    }
}

fn render_result(result: &RangeExplanation, out: &mut String) {
    let status = match result.status {
        RangeExplanationStatus::Matched => "matches this range locally",
        RangeExplanationStatus::Mismatch => "does not match this range",
        RangeExplanationStatus::Unsupported => "comparison unsupported (inconclusive)",
        RangeExplanationStatus::Limited => "comparison limit reached (inconclusive)",
    };
    let _ = writeln!(out, "Result: {status}");
    if let Some(binding) = &result.matched_binding {
        let _ = writeln!(out, "Source binding: {binding}");
    }
    for (needle, subject) in result.aligned_statements.iter().enumerate() {
        if let Some(subject) = subject {
            let _ = writeln!(
                out,
                "  selector statement [{needle}] → source [{}]",
                subject
            );
        }
    }
    for (needle, subject) in &result.free_bindings {
        let _ = writeln!(
            out,
            "  observed free identifier: {needle} → {subject} (external identity unchecked)"
        );
    }
    if let Some(failure) = &result.failure {
        let _ = writeln!(
            out,
            "  selector statement [{}] / source [{}]: {}",
            failure.selector_statement_index, failure.source_statement_index, failure.reason
        );
    }
    if let Some(reason) = &result.reason {
        let _ = writeln!(out, "  {reason}");
    }
}
