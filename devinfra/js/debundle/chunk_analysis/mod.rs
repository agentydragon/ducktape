//! Per-chunk analysis and dependency-graph construction, as two steps.
//!
//! [`compute_chunk_facts`] analyzes one parsed chunk under explicit
//! [`AnalysisInputs`] (author assumptions already resolved by the caller) and
//! produces per-statement facts: declared bindings, eager/lazy reads,
//! side-effect summaries, purity, top-level-await detection. It also owns the
//! side effects that derive from those facts alone: stderr warnings for
//! redundant purity hints, the top-level `await` bail (the realizability
//! theorem does not cover it), and the input-chunk admission scan
//! (docs/design.md A1/A3/A5; see `chunk_admission`).
//!
//! [`build_chunk_graph`] derives the dependency graph and its structural atomic
//! groups (source-unit SCCs of `G_atomic`, which every valid factorization keeps
//! co-located) from those facts, [`GraphOptions`] and the chunk's resolved
//! `commuting_statements`. The graph inputs are deliberately not part of
//! analysis: an edit to them must not change the facts.
//!
//! Neither step reads the spec, the assignment, chunk renames or the
//! unassigned-mode policy.
//!
mod chunk_admission;

pub use chunk_admission::DynamicImportTarget;
use chunk_admission::enforce_chunk_admission;

use anyhow::{Context, Result, bail};
use swc_common::Span;
use swc_ecma_ast::Module;

use analysis::AnalysisInputs;
use analysis::analysis_inputs::ResolvedCommutingStatements;
use analysis::atomic_groups::{
    DependencyGraphAndAtomicGroups, compute_dependency_graph_and_atomic_groups_with,
};
use analysis::facts::{
    ChunkFactAnalysis, ExportReentryPolicy, StructuralChunkAnalysis, analyze_chunk_with_policy,
};
use analysis::graph::GraphOptions;
use analysis::purity::{
    RedundantPureMemberReason, RedundantPurityProofBasis, RedundantPurityReason,
};
use spec::AdmissionOverrides;

/// Analyze a chunk from its precomputed structural layer: compute its facts,
/// emit any redundant-purity-hint diagnostics, and fail if the chunk has
/// top-level `await` or fails the input-chunk admission scan. Selector
/// resolution uses the same structural layer before semantic member
/// annotations are projected to concrete bindings.
///
/// Returns `Err` for top-level `await` (docs/design.md A2) or a
/// non-overridden admission violation (A1/A3/A5). `resolve_dynamic_import`
/// supplies where a dynamic-import specifier lands, for the A3 check; the
/// analysis itself stays artifact-free.
#[allow(clippy::too_many_arguments)]
pub fn compute_chunk_facts<F>(
    chunk_id: &str,
    module: &Module,
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    source_path: Option<&str>,
    line_range_for_span: F,
    admission_overrides: AdmissionOverrides,
    export_reentry_policy: &ExportReentryPolicy,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Result<ChunkFactAnalysis>
where
    F: FnMut(Span) -> Option<(usize, usize, usize)>,
{
    let fact_analysis = analyze_chunk_with_policy(
        structural,
        hints,
        export_reentry_policy,
        source_path,
        line_range_for_span,
    );
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
        admission_overrides,
        resolve_dynamic_import,
    )?;
    Ok(fact_analysis)
}

/// Build the chunk's dependency graph and structural atomic groups from its
/// facts. A `DependencyGraphBuildError` stays downcastable through the added
/// context, so callers can report rejected `commuting_statements` entries.
pub fn build_chunk_graph(
    chunk_id: &str,
    facts: &ChunkFactAnalysis,
    options: GraphOptions,
    commuting_statements: &[ResolvedCommutingStatements],
) -> Result<DependencyGraphAndAtomicGroups> {
    compute_dependency_graph_and_atomic_groups_with(&facts.facts, options, commuting_statements)
        .with_context(|| format!("building dependency graph for chunk {chunk_id}"))
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
             the analyzer infers {reason} for this binding without this hint. {basis}",
            binding = hint.binding_name,
            reason = match hint.reason {
                RedundantPurityReason::InferredPureFunction =>
                    "pure (the function body classifies Pure by recursive analysis)",
                RedundantPurityReason::InferredPlainDataBinding =>
                    "PlainData (chunk-local const/let plain literal with no chunk-wide writes through the binding)",
            },
            basis = match hint.proof_basis {
                RedundantPurityProofBasis::WithoutPurityHints =>
                    "This proof uses no `purity: pure` hints. Remove the hint and validate the spec.",
                RedundantPurityProofBasis::WithOtherPurityHints =>
                    "This proof requires other `purity: pure` hints. Remove hints individually and revalidate after each edit.",
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
                    "pure via its builtin summary row",
            },
        );
    }
}
