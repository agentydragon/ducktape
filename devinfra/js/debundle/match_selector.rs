//! `debundle spec match-selector`: resolve a candidate `source_match` against a
//! chunk and report what it binds.
//!
//! The interactive prove-gate probe behind the selector-authoring loop
//! (`plans/selector_authoring_agent.md`): the agent forms an anchor hypothesis,
//! writes a candidate `match`, and asks "does this resolve to the singleton I
//! mean, and is it the right one?" before committing the selector to YAML. The
//! batch minimizer answers the same question with a boolean gate; this exposes
//! it as a probe that returns the matched handles instead.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use spec::{SourceMatch, SourceMatchIdentifierMode};

pub struct MatchSelectorConfig {
    pub source_file: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorMatch {
    /// Top-level statement index the selector matched in the chunk.
    pub body_index: usize,
    /// Runtime (minified) name of the binding the selector would claim.
    pub binding_name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorReport {
    /// The headline verdict: exactly one item matched, so the selector is a
    /// valid (unique) pin. Zero or several matches both make it unusable.
    pub unique: bool,
    pub matches: Vec<MatchSelectorMatch>,
}

/// Resolve the chunk path from the `--source-file` / `--source-root` + `--chunk`
/// flag combination shared by the source-aware selector commands.
pub(crate) fn resolve_chunk_source_file(
    source_file: Option<&Path>,
    source_root: Option<&Path>,
    chunk: Option<&Path>,
) -> Result<PathBuf> {
    match (source_file, source_root, chunk) {
        (Some(source_file), _, None) => Ok(source_file.to_path_buf()),
        (None, Some(source_root), Some(chunk)) => Ok(source_root.join(chunk)),
        (Some(_), _, Some(_)) => {
            bail!("use either --source-file or --source-root with --chunk, not both")
        }
        _ => bail!("a source chunk is required: pass --source-file or --source-root + --chunk"),
    }
}

pub fn run_match_selector(config: &MatchSelectorConfig) -> Result<MatchSelectorReport> {
    js_ast::with_swc_globals(|| run_match_selector_impl(config))
}

fn run_match_selector_impl(config: &MatchSelectorConfig) -> Result<MatchSelectorReport> {
    let source_file = resolve_chunk_source_file(
        config.source_file.as_deref(),
        config.source_root.as_deref(),
        config.chunk.as_deref(),
    )?;
    let source = std::fs::read_to_string(&source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    let parsed = js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))?;
    let selector = SourceMatch {
        match_source: config.match_source.clone(),
        identifiers: config.identifiers,
        target_binding: config.target_binding.clone(),
        target_statement: None,
        target_statements: None,
        wildcard_string_literals: BTreeSet::new(),
    }
    .selector();
    let mut matches: Vec<MatchSelectorMatch> = source_match::member_binding_candidate_matches(
        &parsed.module,
        "<match-selector>",
        &selector,
    )?
    .into_iter()
    .map(|matched| MatchSelectorMatch {
        body_index: matched.body_idx,
        binding_name: matched.binding.binding_name,
    })
    .collect();
    matches.sort_by_key(|matched| matched.body_index);
    Ok(MatchSelectorReport {
        unique: matches.len() == 1,
        matches,
    })
}

pub fn render_match_selector_text(report: &MatchSelectorReport, out: &mut String) {
    use std::fmt::Write;
    let verdict = match report.matches.len() {
        1 => "unique",
        0 => "no-match",
        _ => "ambiguous",
    };
    let _ = writeln!(out, "{verdict} ({} match(es))", report.matches.len());
    for matched in &report.matches {
        let _ = writeln!(
            out,
            "  body[{}] -> {}",
            matched.body_index, matched.binding_name
        );
    }
}
