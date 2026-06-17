//! `debundle spec match-selector`: resolve a candidate `source_match` against a
//! chunk and report what it binds — and, when it pins a unique target, how much
//! further it could be holed.
//!
//! The interactive prove-gate probe behind the selector-authoring loop
//! (`plans/selector_authoring_agent.md`): the agent forms an anchor hypothesis,
//! writes a candidate `match`, and asks "does this resolve to the singleton I
//! mean, and the right one — and did I over-pin it?" before committing the
//! selector to YAML. Matching and slack share the same parse + baseline resolve,
//! so they are answered together.
//!
//! **Slack** is the mechanical half of "report over-narrow selectors as debt even
//! when they match": each entry is a kept value-expression that could be holed to
//! `ANYTHING` while still pinning the same unique target. It is a _heuristic_ for
//! which pins to revisit, never a verdict — a zero-slack selector can still be
//! anchored on an incidental key, and the agent still judges whether the
//! surviving anchor is the right one. Scope: expression-value relaxations (the
//! dominant over-pin — a pinned literal, argument, or property value); structural
//! run-hole relaxations (dropping whole properties / members / statements) are a
//! future extension.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use spec::{SourceMatch, SourceMatchIdentifierMode};
use swc_ecma_ast::{Expr, Module};
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

pub struct MatchSelectorConfig {
    pub source_file: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
    /// Also compute holing slack when the selector pins a unique target.
    pub check_slack: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorMatch {
    /// Top-level statement index the selector matched in the chunk.
    pub body_index: usize,
    /// Runtime (minified) name of the binding the selector would claim.
    pub binding_name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SlackRelaxation {
    /// A strictly looser selector — the input with one kept value holed to
    /// `ANYTHING` — that still resolves to the same unique target.
    pub relaxed_match: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorReport {
    /// The headline verdict: exactly one item matched, so the selector is a
    /// valid (unique) pin. Zero or several matches both make it unusable.
    pub unique: bool,
    pub matches: Vec<MatchSelectorMatch>,
    /// Kept value-expressions that could be holed to `ANYTHING` without losing
    /// uniqueness — a non-empty list flags a likely over-pin. `None` when the
    /// selector is not unique (slack is undefined) or `--no-slack` skipped it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slack: Option<Vec<SlackRelaxation>>,
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

    let resolve = |match_source: String| -> Result<Vec<source_match::MemberBindingMatch>> {
        let selector = SourceMatch {
            match_source,
            identifiers: config.identifiers,
            target_binding: config.target_binding.clone(),
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
        .selector();
        source_match::member_binding_candidate_matches(
            &parsed.module,
            "<match-selector>",
            &selector,
        )
    };

    let baseline = resolve(config.match_source.clone())?;
    let mut matches: Vec<MatchSelectorMatch> = baseline
        .iter()
        .map(|matched| MatchSelectorMatch {
            body_index: matched.body_idx,
            binding_name: matched.binding.binding_name.clone(),
        })
        .collect();
    matches.sort_by_key(|matched| matched.body_index);

    let unique = matches.len() == 1;
    let slack = match (unique, config.check_slack) {
        (true, true) => Some(compute_slack(
            &config.match_source,
            baseline[0].body_idx,
            &baseline[0].binding.binding_name,
            &resolve,
        )?),
        _ => None,
    };

    Ok(MatchSelectorReport {
        unique,
        matches,
        slack,
    })
}

/// For each kept value-expression in the selector, try holing it to `ANYTHING`
/// and keep the relaxation if the selector still resolves to the same unique
/// `(body_idx, binding_name)` target.
fn compute_slack(
    match_source: &str,
    target_body_idx: usize,
    target_binding_name: &str,
    resolve: &impl Fn(String) -> Result<Vec<source_match::MemberBindingMatch>>,
) -> Result<Vec<SlackRelaxation>> {
    let mut selector_module =
        js_ast::parse_js_module_consuming("<match-selector slack>", match_source.to_string())
            .with_context(|| "parsing the candidate selector for slack analysis")?
            .module;
    js_ast::strip_parens(&mut selector_module);

    let mut slack = Vec::new();
    for index in 0..count_holeable_exprs(&selector_module) {
        let mut relaxed = selector_module.clone();
        hole_nth_expr(&mut relaxed, index);
        let relaxed_match = js_ast::emit_module_source(&relaxed)?;
        if let [only] = resolve(relaxed_match.clone())?.as_slice() {
            if only.body_idx == target_body_idx && only.binding.binding_name == target_binding_name
            {
                slack.push(SlackRelaxation { relaxed_match });
            }
        }
    }
    Ok(slack)
}

/// A value expression that holing to `ANYTHING` would relax. Bare identifiers are
/// excluded: under `alpha_all` they are already alpha-wildcards (and a hole
/// keyword is itself an identifier), so holing one removes no real anchor.
fn is_holeable(expr: &Expr) -> bool {
    !matches!(expr, Expr::Ident(_) | Expr::Invalid(_))
}

fn count_holeable_exprs(module: &Module) -> usize {
    struct Counter {
        count: usize,
    }
    impl Visit for Counter {
        fn visit_expr(&mut self, expr: &Expr) {
            if is_holeable(expr) {
                self.count += 1;
            }
            expr.visit_children_with(self);
        }
    }
    let mut counter = Counter { count: 0 };
    module.visit_with(&mut counter);
    counter.count
}

/// Replace the `target`-th holeable expression (pre-order) with `ANYTHING`. The
/// pre-order walk matches [`count_holeable_exprs`], so index `i` names the same
/// node across the count and hole passes.
fn hole_nth_expr(module: &mut Module, target: usize) {
    struct Holer {
        target: usize,
        seen: usize,
        done: bool,
    }
    impl VisitMut for Holer {
        fn visit_mut_expr(&mut self, expr: &mut Expr) {
            if self.done {
                return;
            }
            if is_holeable(expr) {
                if self.seen == self.target {
                    *expr = crate::render::anything_expr();
                    self.done = true;
                    return;
                }
                self.seen += 1;
            }
            expr.visit_mut_children_with(self);
        }
    }
    let mut holer = Holer {
        target,
        seen: 0,
        done: false,
    };
    module.visit_mut_with(&mut holer);
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
    match &report.slack {
        None => {}
        Some(slack) if slack.is_empty() => {
            let _ = writeln!(out, "slack: none (no kept value is holeable)");
        }
        Some(slack) => {
            let _ = writeln!(
                out,
                "slack: {} holeable value(s) — likely over-pin",
                slack.len()
            );
            for (i, relaxation) in slack.iter().enumerate() {
                let _ = writeln!(out, "  [{i}] still unique after holing one value:");
                for line in relaxation.relaxed_match.lines() {
                    let _ = writeln!(out, "        {line}");
                }
            }
        }
    }
}
