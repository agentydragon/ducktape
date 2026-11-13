//! Per-chunk analysis and dependency-graph construction, as two steps.
//!
//! [`compute_chunk_facts`] analyzes one parsed chunk under explicit
//! [`AnalysisInputs`] (author assumptions already resolved by the caller) and
//! produces per-statement facts: declared bindings, eager/lazy reads,
//! side-effect summaries, purity, top-level-await detection. It also owns the
//! checks that derive from those facts alone: structured diagnostics for
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
use chunk_admission::{enforce_chunk_admission, enforce_split_admission};

use anyhow::{Context, Result, bail};
use swc_ecma_ast::Module;

use analysis::AnalysisInputs;
use analysis::analysis_inputs::ResolvedCommutingStatements;
use analysis::atomic_groups::{
    DependencyGraphAndAtomicGroups, compute_dependency_graph_and_atomic_groups_with,
};
use analysis::diagnostics::{Diagnostic, Diagnostics};
use analysis::execution_evidence::SourceLocator;
use analysis::facts::{
    ChunkFactAnalysis, ExportReentryPolicy, StructuralChunkAnalysis, analyze_chunk_with_policy,
};
use analysis::graph::GraphOptions;
use spec::{AdmissionCheck, AdmissionOverrides};

/// Analyze a chunk from its precomputed structural layer: compute its facts,
/// record redundant-purity-hint diagnostics, and fail if the chunk has
/// top-level `await` or fails the input-chunk admission scan. Selector
/// resolution uses the same structural layer before semantic member
/// annotations are projected to concrete bindings.
///
/// Returns `Err` for top-level `await` (docs/design.md A2) or a
/// non-overridden admission violation (A1/A3/A5). `resolve_dynamic_import`
/// supplies where a dynamic-import specifier lands, for the A3 check; the
/// analysis itself stays artifact-free.
#[allow(clippy::too_many_arguments)]
pub fn compute_chunk_facts(
    chunk_id: &str,
    diagnostics: &Diagnostics,
    module: &Module,
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    locator: Option<SourceLocator<'_>>,
    admission_overrides: AdmissionOverrides,
    export_reentry_policy: &ExportReentryPolicy,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Result<(ChunkFactAnalysis, Vec<AdmissionCheck>)> {
    let fact_analysis =
        analyze_chunk_with_policy(structural, hints, export_reentry_policy, locator);
    let redundant_admission_overrides = check_chunk_facts(
        chunk_id,
        diagnostics,
        module,
        &fact_analysis,
        admission_overrides,
        resolve_dynamic_import,
    )?;
    Ok((fact_analysis, redundant_admission_overrides))
}

/// What [`compute_chunk_facts`] does once the facts exist: the redundant-hint
/// diagnostics, the top-level `await` bail and the admission scan. A run that
/// takes its facts from an analysis artifact calls it directly, so its
/// diagnostics and verdict match a run that computes them. Returns the
/// configured admission overrides that suppressed no violation.
pub fn check_chunk_facts(
    chunk_id: &str,
    diagnostics: &Diagnostics,
    module: &Module,
    fact_analysis: &ChunkFactAnalysis,
    admission_overrides: AdmissionOverrides,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Result<Vec<AdmissionCheck>> {
    for hint in &fact_analysis.redundant_purity_hints {
        diagnostics.record(chunk_id, Diagnostic::RedundantPurityHint(hint.clone()));
    }
    for hint in &fact_analysis.redundant_pure_member_hints {
        diagnostics.record(chunk_id, Diagnostic::RedundantPureMemberHint(hint.clone()));
    }
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
        diagnostics,
        module,
        admission_overrides,
        resolve_dynamic_import,
    )
}

/// The admission checks that hold only for a transformed chunk (split across
/// modules, reordered or renamed): a lazy direct `eval` names bindings at
/// run time. Run once the chunk's assignment is known; `transformed` says
/// whether the emitted tree differs from the source.
pub fn check_transformed_chunk_admission(
    chunk_id: &str,
    module: &Module,
    admission_overrides: AdmissionOverrides,
    transformed: bool,
) -> Result<()> {
    enforce_split_admission(chunk_id, module, admission_overrides, transformed)
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn analysis_records_diagnostics_without_printing() {
        // Execute the real library path in a child so the test can observe its
        // process streams independently of libtest's output capture.
        const CHILD: &str = "DEBUNDLE_DIAGNOSTICS_PROBE_CHILD";
        if std::env::var_os(CHILD).is_none() {
            let output = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "tests::analysis_records_diagnostics_without_printing",
                    "--nocapture",
                ])
                .env(CHILD, "1")
                .output()
                .unwrap();
            assert!(output.status.success(), "{output:?}");
            assert!(output.stderr.is_empty(), "{output:?}");
            let stdout = String::from_utf8(output.stdout).unwrap();
            assert!(
                !stdout.contains("warning:") && !stdout.contains("notice:"),
                "{stdout}"
            );
            return;
        }
        swc_common::GLOBALS.set(&swc_common::Globals::new(), || {
            let module =
                raw_js_test_support::parse("function leaf() { return 7; } export { leaf };\n");
            let structural = analysis::facts::complete_chunk_structural(
                analysis::facts::collect_chunk_source_facts(&module, None),
            );
            let mut inputs = AnalysisInputs::default();
            inputs.assumptions.declared_pure.insert("leaf".to_owned());
            let diagnostics = Diagnostics::default();
            let (facts, redundant) = compute_chunk_facts(
                "main",
                &diagnostics,
                &module,
                structural,
                &inputs,
                None,
                AdmissionOverrides {
                    a1_eval: true,
                    ..AdmissionOverrides::default()
                },
                &ExportReentryPolicy::Conservative,
                &|_| DynamicImportTarget::External,
            )
            .unwrap();
            assert_eq!(facts.redundant_purity_hints.len(), 1);
            assert_eq!(redundant, vec![AdmissionCheck::A1Eval]);
            let records = diagnostics.drain();
            assert!(matches!(
                records["main"].as_slice(),
                [
                    Diagnostic::RedundantPurityHint(_),
                    Diagnostic::RedundantAdmissionOverride(AdmissionCheck::A1Eval)
                ]
            ));
        });
    }
}
