//! Stage A composer for the per-chunk pipeline.
//!
//! Stage A is the **spec-independent** half of the per-chunk
//! pipeline. Given a parsed chunk AST plus analysis hints + per-
//! chunk owner-graph options, it produces:
//!
//! - per-statement static facts (declared bindings, eager/lazy reads,
//!   side-effect summaries, purity classification, top-level-await
//!   detection);
//! - the owner graph derived from those facts;
//! - the structural atomic units (owner-level SCCs of `G_atomic`),
//!   which any valid factorization must keep co-located.
//!
//! The composer also owns the side-effects that derive purely
//! from Stage A output:
//!
//! - stderr warnings for redundant-purity / redundant-pure-member
//!   spec hints the analyzer can already infer;
//! - the fatal bail when a chunk contains a top-level `await` (which
//!   the realizability theorem does not cover);
//! - the input-chunk admission scan for the other statically
//!   checkable input assumptions (docs/design.md A1/A3/A5; see
//!   `chunk_admission`), including its override notices.
//!
//! Stage A is a pure function of `(chunk_id, module, hints,
//! owner_graph_options)` plus the spec-free `source_path` annotation,
//! the line-index callback for source-location resolution, and the
//! caller-supplied dynamic-import resolver (the A3 admission check
//! needs "which chunk does this specifier land in", but Stage A
//! itself stays artifact-free). It does not read the spec, the
//! partition, chunk renames, or the unassigned-mode policy — those
//! are Stage B inputs.
//!
//! Stage A is materialized only in memory, by callers
//! (today: `materialize_logical_chunk`) that call
//! [`compute_stage_one_analysis`] and pass its components to Stage B.
//! A previous design also serialized Stage A's output to disk so a
//! separate-process Stage B could consume the cache; that design was
//! abandoned (see `docs/lessons_learned/cross_process_stage_b.md`).
//! The composer survives as a structural readability boundary.
//!
//! Atomic-unit-rebind folding is the "Stage A.5" step: it runs after
//! the partition seed phases (explicit requests, destructure pull,
//! residual sweep) but is purely a function of Stage A's output
//! (owner graph + atomic units) and the post-seed binding→module
//! assignment. The decision logic lives at
//! [`compute_rebind_folds`]; the lowering-side caller applies the
//! returned folds to its `ModulePlan` list.

mod chunk_admission;
mod rebind_fold;

pub use chunk_admission::{DynamicImportTarget, enforce_chunk_admission};
pub use rebind_fold::{RebindFold, compute_rebind_folds};

use anyhow::{Context, Result, bail};
use swc_common::Span;
use swc_ecma_ast::Module;

use analysis::AnalysisHints;
use analysis::atomic_units::{OwnerGraphAndUnits, compute_owner_graph_and_units_with};
use analysis::facts::{ChunkFactAnalysis, analyze_chunk};
use analysis::graph::OwnerGraphOptions;
use analysis::purity::{RedundantPureMemberReason, RedundantPurityReason};

/// Output of Stage A: the per-chunk analysis that does not depend on
/// the spec.
#[derive(Debug, Clone)]
pub struct StageOneAnalysis {
    /// Per-statement static facts plus chunk-wide flags (top-level
    /// await detection, redundant purity / pure-member hints).
    pub fact_analysis: ChunkFactAnalysis,
    /// Owner graph + structural atomic units derived from
    /// `fact_analysis.facts`. Carries no spec-dependent state; the
    /// atomic units here are the *structural* class (per docs/design.md
    /// §"Two classes of atom") that any valid factorization must
    /// preserve.
    pub owner_graph_and_units: OwnerGraphAndUnits,
}

/// Run Stage A: analyze the chunk's facts, emit any
/// redundant-purity-hint diagnostics, fail fast if the chunk has
/// top-level `await` or fails the input-chunk admission scan, then
/// derive the owner graph + structural atomic units.
///
/// Returns `Err` if the chunk has top-level `await` (the realizability
/// theorem does not cover async modules — see docs/design.md A2) or
/// violates a non-overridden admission check (docs/design.md
/// A1/A3/A5; see `chunk_admission`). `resolve_dynamic_import`
/// supplies the artifact-aware "where does this dynamic-import
/// specifier land" answer for the A3 check — Stage A itself stays
/// artifact-free.
pub fn compute_stage_one_analysis<F>(
    chunk_id: &str,
    module: &Module,
    hints: &AnalysisHints,
    source_path: Option<&str>,
    line_range_for_span: F,
    owner_graph_options: OwnerGraphOptions,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Result<StageOneAnalysis>
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let fact_analysis = analyze_chunk(module, hints, source_path, line_range_for_span);
    report_redundant_hints_to_stderr(chunk_id, &fact_analysis);
    if let Some(ord) = fact_analysis.top_level_await {
        bail!(
            "materialize_logical_modules: chunk {chunk_id} has top-level `await` \
             at statement #{ordinal} (TLA); the debundler's realizability theorem \
             does not cover async modules (docs/design.md A2). Wrap the awaited code \
             in an async function or rewrite as a synchronous initialization.",
            ordinal = ord.0,
        );
    }
    enforce_chunk_admission(
        chunk_id,
        module,
        owner_graph_options.admission_overrides,
        resolve_dynamic_import,
    )?;
    let owner_graph_and_units =
        compute_owner_graph_and_units_with(&fact_analysis.facts, owner_graph_options)
            .with_context(|| format!("building owner graph for chunk {chunk_id}"))?;
    Ok(StageOneAnalysis {
        fact_analysis,
        owner_graph_and_units,
    })
}

/// Emit one stderr warning per spec hint the analyzer inferred
/// automatically. Surfaced every build so spec authors are nudged to
/// prune load-free hints — every such hint is an extra trust
/// assertion the validator can't re-verify, and the shrinking trust
/// surface is the point of recursive purity inference.
fn report_redundant_hints_to_stderr(chunk_id: &str, analysis: &ChunkFactAnalysis) {
    for hint in &analysis.redundant_purity_hints {
        eprintln!(
            "warning: chunk {chunk_id}: `purity: pure` hint on binding `{binding}` is redundant — \
             the analyzer infers {reason} for this binding without the hint and the override is a no-op. \
             Remove the hint from the spec.",
            binding = hint.binding_name,
            reason = match hint.reason {
                RedundantPurityReason::InferredPureFunction =>
                    "pure (the function body classifies Pure by recursive analysis)",
                RedundantPurityReason::InferredPlainDataBinding =>
                    "PlainData (chunk-local const/let plain literal with no chunk-wide writes through the binding)",
            },
        );
    }
    for hint in &analysis.redundant_pure_member_hints {
        eprintln!(
            "warning: chunk {chunk_id}: `pure_members: [{property}]` on binding `{binding}` \
             is redundant — the analyzer infers {reason} without the hint. \
             Remove the entry from the spec.",
            binding = hint.binding_name,
            property = hint.property,
            reason = match hint.reason {
                RedundantPureMemberReason::WhitelistedStaticCall =>
                    "pure via PURE_STATIC_CALLS (already on the global-receiver whitelist)",
            },
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    fn parse(source: &str) -> Module {
        let cm: Lrc<SourceMap> = Default::default();
        let fm = cm.new_source_file(
            FileName::Custom("test.js".into()).into(),
            source.to_string(),
        );
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer)
            .parse_module()
            .expect("parse module")
    }

    /// Two-binding chunk: the composer must surface both the per-
    /// statement facts (one entry per top-level statement) and the
    /// derived owner graph (`compute_owner_graph_and_units_with`
    /// builds at least one node per declared binding plus an owner
    /// for each anonymous statement). The fixture exercises an
    /// at-init read (`const B = A + 1`) so the owner graph carries
    /// an `EagerUse` constraining edge — that edge collapses A's and
    /// B's owners into one structural atomic unit.
    #[test]
    fn composer_runs_facts_and_owner_graph_together() {
        let module = parse("const A = 1;\nconst B = A + 1;\nexport { A, B };\n");
        let stage_one = compute_stage_one_analysis(
            "test_chunk",
            &module,
            &AnalysisHints::default(),
            None,
            |_| None,
            OwnerGraphOptions::default(),
            &|_| DynamicImportTarget::External,
        )
        .expect("stage one");

        // Three top-level items: two consts + one export.
        assert_eq!(stage_one.fact_analysis.facts.len(), 3);
        assert!(stage_one.fact_analysis.top_level_await.is_none());

        let owner_count = stage_one.owner_graph_and_units.owner_graph.num_nodes();
        assert!(
            owner_count >= 2,
            "owner graph must hold at least one node per declared \
             binding; got {owner_count}",
        );

        assert!(
            !stage_one.owner_graph_and_units.atomic_units.is_empty(),
            "atomic-units pass produced no structural units",
        );
    }

    /// TLA bail: any chunk with a top-level `await` is rejected
    /// before owner-graph construction. The error message must name
    /// the chunk id and the offending statement ordinal so the spec
    /// author can locate it.
    #[test]
    fn composer_bails_on_top_level_await() {
        let module = parse("const data = await fetch('/api');\n");
        let result = compute_stage_one_analysis(
            "tla_chunk",
            &module,
            &AnalysisHints::default(),
            None,
            |_| None,
            OwnerGraphOptions::default(),
            &|_| DynamicImportTarget::External,
        );
        let err = result.expect_err("TLA chunks must fail Stage A");
        let msg = format!("{err:#}");
        assert!(msg.contains("tla_chunk"), "error names chunk: {msg}");
        assert!(msg.contains("top-level `await`"), "error names TLA: {msg}");
    }
}
