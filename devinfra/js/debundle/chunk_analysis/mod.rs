//! Chunk-analysis composer for the per-chunk pipeline.
//!
//! Chunk analysis is independent of module placement. Given a parsed chunk AST,
//! explicit [`AnalysisInputs`] (including author-certified assumptions), and
//! per-chunk dependency-graph options, it produces:
//!
//! - per-statement static facts (declared bindings, eager/lazy reads,
//!   side-effect summaries, purity classification, top-level-await
//!   detection);
//! - the dependency graph derived from those facts;
//! - the structural atomic groups (source-unit-level SCCs of `G_atomic`),
//!   which any valid factorization must keep co-located.
//!
//! The composer also owns the side-effects that derive purely from
//! chunk-analysis output:
//!
//! - stderr warnings for redundant-purity / redundant-pure-member
//!   spec hints the analyzer can already infer;
//! - the fatal bail when a chunk contains a top-level `await` (which
//!   the realizability theorem does not cover);
//! - the input-chunk admission scan for the other statically
//!   checkable input assumptions (docs/design.md A1/A3/A5; see
//!   `chunk_admission`), including its override notices.
//!
//! Analysis results depend on `(chunk_id, module, analysis_inputs,
//! dependency_graph_options)` plus the `source_path` annotation,
//! the line-index callback for source-location resolution, and the
//! caller-supplied dynamic-import resolver (the A3 admission check
//! needs "which chunk does this specifier land in", but chunk
//! analysis itself stays artifact-free). It does not read the spec directly,
//! the assignment, chunk renames, or the unassigned-mode policy. Its caller
//! resolves author annotations into assumptions before invoking analysis; the
//! resulting facts are conditional on those contracts. Admission diagnostics
//! and redundant-assumption warnings are emitted by this composer.
//!
//! Chunk analysis is materialized only in memory, by callers
//! (today: `materialize_logical_chunk`) that call
//! [`compute_chunk_analysis`] and pass its components to the
//! materializer. A previous design also serialized chunk-analysis
//! output to disk so a separate-process materialization action could
//! consume the cache; that design was abandoned (see
//! `docs/lessons_learned/cross_process_stage_b.md`).
//! The composer survives as a structural readability boundary.
//!
mod chunk_admission;

pub use chunk_admission::DynamicImportTarget;
use chunk_admission::enforce_chunk_admission;

use anyhow::{Context, Result, bail};
use swc_common::Span;
use swc_ecma_ast::Module;

use analysis::AnalysisInputs;
use analysis::atomic_groups::{
    DependencyGraphAndAtomicGroups, compute_dependency_graph_and_atomic_groups_with,
};
use analysis::facts::{
    ChunkFactAnalysis, ExportReentryPolicy, StructuralChunkAnalysis, analyze_chunk_with_policy,
};
use analysis::graph::DependencyGraphOptions;
use analysis::purity::{
    RedundantPureMemberReason, RedundantPurityProofBasis, RedundantPurityReason,
};

/// Output of chunk analysis: per-chunk facts, dependency graph, and atomic
/// groups that do not depend on the module assignment.
#[derive(Debug, Clone)]
pub struct ChunkAnalysisOutput {
    /// Per-statement static facts plus chunk-wide flags (top-level
    /// await detection, redundant purity / pure-member hints).
    pub fact_analysis: ChunkFactAnalysis,
    /// Dependency graph + structural atomic groups derived from
    /// `fact_analysis.facts`. Carries no assignment-dependent state; the
    /// atomic groups here are the *structural* class (per docs/design.md
    /// §"Two classes of atom") that any valid factorization must
    /// preserve.
    pub dependency_graph_and_atomic_groups: DependencyGraphAndAtomicGroups,
}

/// Run chunk analysis from a precomputed structural layer: analyze the
/// chunk's facts, emit any redundant-purity-hint diagnostics, fail fast
/// if the chunk has top-level `await` or fails the input-chunk
/// admission scan, then derive the dependency graph + structural atomic
/// groups. Selector resolution uses that same structural layer before
/// semantic member annotations can be projected to concrete bindings,
/// so materialization reuses the structural analysis when building the final
/// dependency graph.
///
/// Returns `Err` if the chunk has top-level `await` (the realizability
/// theorem does not cover async modules — see docs/design.md A2) or
/// violates a non-overridden admission check (docs/design.md
/// A1/A3/A5; see `chunk_admission`). `resolve_dynamic_import`
/// supplies the artifact-aware "where does this dynamic-import
/// specifier land" answer for the A3 check — chunk analysis itself
/// stays artifact-free.
#[allow(clippy::too_many_arguments)]
pub fn compute_chunk_analysis<F>(
    chunk_id: &str,
    module: &Module,
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    source_path: Option<&str>,
    line_range_for_span: F,
    dependency_graph_options: DependencyGraphOptions,
    export_reentry_policy: ExportReentryPolicy,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Result<ChunkAnalysisOutput>
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
        dependency_graph_options.admission_overrides,
        resolve_dynamic_import,
    )?;
    let dependency_graph_and_atomic_groups = compute_dependency_graph_and_atomic_groups_with(
        &fact_analysis.facts,
        dependency_graph_options,
    )
    .with_context(|| format!("building dependency graph for chunk {chunk_id}"))?;
    Ok(ChunkAnalysisOutput {
        fact_analysis,
        dependency_graph_and_atomic_groups,
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
                    "pure via PURE_STATIC_CALLS (already on the global-receiver whitelist)",
            },
        );
    }
}
