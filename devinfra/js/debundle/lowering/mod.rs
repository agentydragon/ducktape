use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;
use serde::Serialize;
use swc_common::GLOBALS;

use analysis::RedundantPurityHint;
use analysis::reports::author_inputs::AuthorInputLocations;
use analysis_projection::AnalysisSpec;

use artifact::paths::normalize_module_path;
use artifact::reports::{ChunkDecompositionOutput, SelectedModuleLowering};
use artifact::{
    ArtifactIndexes, ChunkBundle, export_policies, normalized_chunk_ids,
    prune_artifact_to_chunk_ids,
};
use gate::{CandidateRelocationAssessmentReport, ChunkGateRejection, UnmatchedSpecClaim};

use output_layout::MODULES_REPORT;

use spec::{
    ChunkExportPurity, ChunkRenames, DependencyGraphOptions, LogicalModule, UnassignedMode,
};

mod anonymous;
mod body_facts;
mod candidate_emission;
mod chunk_renames;
mod commuting_statements;
mod exports;
mod imports;
mod io;
mod lower;
mod materialize;
mod module_output;
mod naturalize;
pub mod ownership_subset;
mod placement;
mod plans;
mod rebind_fold;
mod reentry_report;
pub mod rename_ledger;
#[cfg(test)]
mod rename_ledger_proptest;
mod rewrite_runtime;
mod scope_names;
mod util;
mod visitors;

pub use candidate_emission::{CandidateEmissionDraft, FinalizedCandidateEmissionDraft};
use imports::naturalize_cross_chunk_imports;
use ownership_subset::{OwnershipSubset, SelectedModule};

use imports::VendorReimportOracle;
use io::{ProgramGraphOutput, prepare_output_dir, write_chunk_report_json};

use materialize::{
    ChunkContext, ChunkSpec, EmissionChunkOutputs, FinishedLogicalChunk, GraphConstructionContext,
    MaterializeLogicalChunkInputs, claim_prepared_chunk, collect_materialized_logical_chunks,
    finish_logical_chunk, prepare_logical_chunk,
};
use staged_analysis::{
    ExportReentryReport, export_reentry_reports, load_analysis, loader_surface, program_summary,
};

use util::normalize_optional_relative_dir;

pub struct MaterializeLogicalModulesResult {
    /// Lowered and pass-through files whose whole-program rewrites have not
    /// run yet; only the draft's own `finalize` can run them.
    pub output: CandidateEmissionDraft,
    pub decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
    pub selected_lowerings: Vec<SelectedModuleLowering>,
    pub module_count: usize,
    /// Spec claims that named a binding for which no top-level
    /// declaration exists in the source chunk. Previously dropped
    /// silently — the binding would fall through to the residual and
    /// the named module's export list would be one entry short. Now
    /// collected across every chunk so reconstruction can diagnose
    /// all claims (the chunk lowers as if the missing claim were absent).
    /// Final validation rejects the full list before writing the output. See
    /// [`UnmatchedSpecClaim`].
    pub unmatched_spec_claims: Vec<UnmatchedSpecClaim>,
    /// Chunks the realizability gate rejected but lowered anyway under
    /// [`GateRejectionPolicy::LowerUnrunnable`]; empty under
    /// [`GateRejectionPolicy::Refuse`], which fails the run instead.
    pub gate_rejections: Vec<ChunkGateRejection>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesReport {
    pub chunk_id: String,
    /// Present only for an ownership-subset run; selectors and contracts
    /// still come from the complete authored spec.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ownership_subset: Option<BTreeSet<SelectedModule>>,
    pub counts: ChunkModulesCounts,
    pub final_module_contents: Vec<FinalModuleContent>,
    /// Explicitly opted-in authored owners and the atomic-group source units
    /// they adopted. This is placement provenance, not an analysis input.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub atomic_group_owner_completions: Vec<AtomicGroupOwnerCompletionReport>,
    pub requested_logical_modules: Vec<RequestedLogicalModule>,
    /// Bare imported callable capabilities used by this chunk, with their
    /// defining evidence. Empty when no such import was resolved.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub imported_callables: BTreeMap<String, analysis::CallableSummary>,
    /// `purity: pure` hints the analyzer inferred automatically.
    /// Same content as the stderr warnings the build prints; carried
    /// on the report so JSON consumers can pin behavior across runs.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesCounts {
    pub applied: usize,
    pub selected_bindings: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct FinalModuleContent {
    pub binding_names: Vec<String>,
    /// Declaration source units for each input binding, in `binding_names` order.
    pub binding_source_units: Vec<BindingSourceUnitAssociation>,
    pub file: String,
    pub member_names: Vec<String>,
    /// Canonical [`spec::ModulePath`] (the report is per-chunk, so
    /// the chunk is implicit).
    pub path: spec::ModulePath,
    pub source_unit_ids: Vec<String>,
    /// True for a planned output with `ModuleRole::Unassigned`, never entry.
    pub residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupOwnerCompletionReport {
    /// Canonical authored module path that opted into completing this atom.
    pub owner_path: spec::ModulePath,
    /// Complete source-unit membership of the atomic group.
    pub atomic_group_source_unit_ids: Vec<String>,
    /// Source units assigned to this owner by the opt-in.
    pub adopted_source_unit_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BindingSourceUnitAssociation {
    /// Original input binding name from the module plan.
    pub binding_name: String,
    /// Sorted chunk-local graph IDs of source units declaring this binding.
    pub source_unit_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RequestedLogicalModule {
    /// Canonical [`spec::ModulePath`] of the requested target.
    pub target_path: spec::ModulePath,
    /// True for the synthesized memberless catchall request, not for an
    /// explicit module later assigned `ModuleRole::Unassigned`.
    pub residual: bool,
}

/// Complete diagnostic sinks; never an analysis or full-graph selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum FullReportKind {
    TranslationValidation,
    ExportReentry,
}

impl FullReportKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::TranslationValidation => "translation-validation",
            Self::ExportReentry => "export-reentry",
        }
    }
}

impl std::str::FromStr for FullReportKind {
    type Err = String;
    fn from_str(value: &str) -> std::result::Result<Self, Self::Err> {
        match value {
            "translation-validation" => Ok(Self::TranslationValidation),
            "export-reentry" => Ok(Self::ExportReentry),
            _ => Err(format!(
                "unknown full report {value:?}; expected translation-validation or export-reentry"
            )),
        }
    }
}

/// Full graph serialization is independent of report timing and evidence detail.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GraphReportSelection {
    None,
    Full,
}

impl GraphReportSelection {
    pub fn enabled(self) -> bool {
        self == Self::Full
    }
}

/// Where (and when) per-chunk validation reports are written.
#[derive(Debug, Clone)]
pub enum ReportEmission {
    /// No reports.
    None,
    /// Write per-chunk reports (modules, independently selected graphs,
    /// rejection evidence) under this root. The directory is prepared
    /// (must be empty) up front.
    Full(PathBuf),
    /// Write reports only when the realizability gate rejects. Used by
    /// `debundle run --dry-run`: the accept path writes nothing (the
    /// no-output contract), but a rejection still materializes
    /// rejection evidence. Graph-backed follow-ups require explicit full
    /// graph selection.
    OnRejection(PathBuf),
}

impl ReportEmission {
    /// Root for accept-path reports; graph detail is selected independently.
    pub fn full_dir(&self) -> Option<&Path> {
        match self {
            Self::Full(dir) => Some(dir),
            Self::None | Self::OnRejection(_) => None,
        }
    }

    /// Root for rejection evidence (written on gate rejection).
    pub fn rejection_dir(&self) -> Option<&Path> {
        match self {
            Self::Full(dir) | Self::OnRejection(dir) => Some(dir),
            Self::None => None,
        }
    }
}

/// What a chunk whose assignment the realizability gate rejected becomes.
/// The gate's verdict and evidence reports are the same either way.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateRejectionPolicy {
    /// Fail materialization with the rendered rejection.
    Refuse,
    /// Lower the rejected assignment anyway, with a banner comment on the
    /// chunk's entry file, and report the rejection in
    /// [`MaterializeLogicalModulesResult::gate_rejections`] for the caller to
    /// mark and fail the run (`debundle run --emit-on-rejection`).
    LowerUnrunnable,
}

#[derive(Debug, Clone)]
pub struct MaterializeLogicalModulesOptions {
    pub ownership_subset: Option<OwnershipSubset>,
    pub graph_provenance: Option<graph_provenance::GraphProvenance>,
    pub config: spec::MaterializeLogicalModulesConfig,
    pub chunk_ids: Vec<String>,
    pub keep_going: bool,
    /// See `pipeline::TransformRunOptions::list_template_identifiers`.
    pub list_template_identifiers: bool,
    pub report_emission: ReportEmission,
    pub graph_reports: GraphReportSelection,
    pub effect_conflicts_jsonl: bool,
    pub effect_conflicts_chunks: BTreeSet<String>,
    pub full_reports: BTreeSet<FullReportKind>,
    pub full_report_chunks: BTreeSet<String>,
    pub gate_rejection: GateRejectionPolicy,
    /// Stop materialization after the report-only effect relocation sweep for
    /// each finalized assignment plan. This requires loaded staged analysis
    /// facts and is used by the assessment-only CLI path.
    pub assessment_only: bool,
    pub analysis: AnalysisMode,
}

/// What a materialization does about analysis, the cross-module pass and
/// per-chunk facts.
#[derive(Debug, Clone)]
pub enum AnalysisMode {
    Compute,
    /// Take the analysis from an artifact (`debundle run --analysis`). The
    /// run refuses an artifact computed by another engine, from other
    /// sources, or for a projection other than `base` completed by this
    /// run's selector solve.
    Load {
        base: Box<AnalysisSpec>,
        artifact: Vec<u8>,
        analysis_engine_sha256: String,
    },
}

/// A materialization's result.
pub enum Materialized {
    Lowered(Box<MaterializeLogicalModulesResult>),
    Assessed(Vec<(String, CandidateRelocationAssessmentReport)>),
}

/// Spec-derived inputs to [`materialize_logical_modules`], all keyed by
/// chunk id.
pub struct MaterializeSpecInputs<'a> {
    pub host_behavior: &'a spec::HostBehavior,
    pub program_premises: &'a spec::ProgramPremises,
    /// Host loader per embedded input file (`inputs.embedded_loaders_path`).
    pub embedded_loaders: &'a BTreeMap<String, spec::BunLoader>,
    pub logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    pub chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
}

pub fn materialize_logical_modules(
    mut artifact: ChunkBundle,
    spec_inputs: MaterializeSpecInputs<'_>,
    vendor_plan: &Arc<vendor::VendorResolutionPlan>,
    options: MaterializeLogicalModulesOptions,
) -> Result<Materialized> {
    ensure!(
        !options.assessment_only || matches!(&options.analysis, AnalysisMode::Load { .. }),
        "assessment-only materialization requires loaded staged analysis facts",
    );
    let MaterializeSpecInputs {
        host_behavior,
        program_premises,
        embedded_loaders,
        logical_modules,
        chunk_renames,
        unassigned_mode,
        chunk_analysis_options,
        chunk_export_purity,
    } = spec_inputs;
    if options.chunk_ids.is_empty() {
        bail!("materialize_logical_modules requires at least one chunk_id");
    }
    let target_dir = normalize_optional_relative_dir(&options.config.target_dir)?;
    let selected_chunk_ids = normalized_chunk_ids(&options.chunk_ids)?;
    if let Some(subset) = &options.ownership_subset {
        ensure!(
            !options.graph_reports.enabled(),
            "full graph export for ownership subsets is unsupported"
        );
        subset.validate(logical_modules, &selected_chunk_ids)?;
        eprintln!(
            "Ownership subset: validating the selected assignment with full spec context; the canonical assignment is not being validated."
        );
    }
    let seen: BTreeSet<&str> = selected_chunk_ids.iter().map(String::as_str).collect();

    // A chunk's `commuting_statements` are resolved only when its dependency
    // graph is built, so an entry for a chunk this run does not materialize
    // would never be checked.
    for (chunk, chunk_options) in chunk_analysis_options {
        ensure!(
            chunk_options.commuting_statements.is_empty()
                || seen.contains(normalize_module_path(chunk)?.as_str()),
            "chunk_analysis_options.{chunk}.commuting_statements names a chunk this run does not materialize"
        );
    }

    if let Some(dir) = options.report_emission.full_dir() {
        prepare_output_dir(dir)?;
    }

    let resolved_export_purity =
        export_policies::resolve_source_export_policies(&artifact, chunk_export_purity)?;
    let chunk_export_purity = resolved_export_purity.as_ref();

    if options.config.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &selected_chunk_ids);
    }
    let artifact_indexes = ArtifactIndexes::build(&artifact)?;
    // Program-level pass over the selected chunks' retained static-import
    // closure: cross-module purity verdicts for each chunk's imported
    // function bindings, consumed per chunk via
    // `AnalysisInputs::imported_purities`. Vendor-swapped providers take part
    // through their bundled bodies (SPEC.md § Vendor swap equivalence).
    let program_summary_chunk_closure = analysis_projection::selected_static_import_closure(
        &artifact,
        &artifact_indexes,
        &selected_chunk_ids,
    )?;
    let reparsed = program_summary::reparse_unretained_entries(
        &artifact,
        Some(&program_summary_chunk_closure),
    );
    let mut loaded = match &options.analysis {
        AnalysisMode::Load {
            artifact: bytes,
            analysis_engine_sha256,
            ..
        } => Some(load_analysis(
            bytes,
            &artifact,
            &selected_chunk_ids,
            options.config.file.as_deref(),
            &reparsed,
            options.graph_provenance.as_ref(),
            analysis_engine_sha256,
        )?),
        AnalysisMode::Compute => None,
    };
    let export_reentry_reports = match (&options.analysis, &mut loaded) {
        (_, Some((_, body))) => std::mem::take(&mut body.export_reentry)
            .into_iter()
            .map(|(chunk, mut report)| {
                // The artifact's copies of these echoed sections carry no
                // author notes; the report shows the run's spec.
                report.host_behavior = host_behavior.clone();
                report.program_premises = program_premises.clone();
                let chunk_id = artifact
                    .chunk_table
                    .get(&chunk)
                    .with_context(|| format!("analysis artifact names unknown chunk {chunk}"))?;
                Ok((chunk_id, report))
            })
            .collect::<Result<_>>()?,
        _ => export_reentry_reports(
            &artifact,
            &artifact_indexes,
            loader_surface::LoaderAssumptions {
                host_behavior,
                program_premises,
                embedded_loaders,
            },
            vendor_plan,
            &selected_chunk_ids,
            options.report_emission.rejection_dir().is_some(),
        )?,
    };
    let export_reentry_policies = export_reentry_reports
        .iter()
        .map(|(chunk_id, report)| (*chunk_id, report.policy.clone()))
        .collect();
    let artifact_ref: &ChunkBundle = &artifact;
    // Vendor consultation for lowering's import construction: runtime re-imports whose source
    // directive targets a partially / bundled-partially swapped chunk
    // are constructed as package / facade imports from the plan oracle
    // instead of chunk re-imports the post-materialize wave would
    // rewrite. `None` when no vendor mark could affect construction.
    let vendor_import_oracle =
        VendorReimportOracle::new(vendor_plan, &artifact.chunk_table, &artifact_indexes);
    // SWC's `swc_common::GLOBALS` is a `scoped_tls` thread-local, so the
    // outer `GLOBALS.set` wrap in `main.rs` / `run_agent` does NOT carry
    // into rayon worker threads. Capture a reference to the current
    // `Globals` and re-set inside each worker closure so `Mark::new()`
    // and `Id`-comparisons stay consistent across the whole pipeline.
    //
    // Source analysis, selector eligibility, the joint solve and source-bound
    // premises belong to the independent frontend service. The result is
    // shared with project-analysis; only this caller constructs output plans.
    let frontend = frontend_elaboration::elaborate(
        selected_chunk_ids
            .iter()
            .map(|chunk_id| frontend_elaboration::FrontendChunkInput {
                artifact: artifact_ref,
                chunk_id,
                file: options.config.file.as_deref(),
            })
            .collect(),
        frontend_elaboration::FrontendSpecInputs {
            logical_modules,
            chunk_renames,
            unassigned_mode,
            chunk_analysis_options,
            graph_provenance: options.graph_provenance.as_ref(),
            chunk_export_purity,
            host_behavior,
            intrinsics_never_written: program_premises.intrinsics_never_written.as_ref(),
            fail_fast: !options.keep_going,
            report_warnings: false,
        },
    )?;
    let frontend_elaboration::FrontendElaboration {
        chunks: frontend_chunks,
        projection,
        graph_inputs,
        resolved_member_sites,
    } = frontend;
    // Frontend resolution runs once for every caller. Output-plan construction
    // can proceed in parallel after that shared result exists.
    let prepared = GLOBALS
        .with(|globals| {
            selected_chunk_ids
                .par_iter()
                .zip(frontend_chunks.into_par_iter())
                .map(|(chunk_id, frontend_chunk)| {
                    GLOBALS.set(globals, || {
                        prepare_logical_chunk(
                            MaterializeLogicalChunkInputs {
                                context: ChunkContext {
                                    ownership_subset: options.ownership_subset.as_ref(),
                                    artifact: artifact_ref,
                                    artifact_indexes: &artifact_indexes,
                                    chunk_id,
                                    target_dir: &target_dir,
                                    keep_going: options.keep_going,
                                    list_template_identifiers: options.list_template_identifiers,
                                    report_emission: &options.report_emission,
                                    graph_reports: options.graph_reports,
                                    effect_conflicts_jsonl: options.effect_conflicts_jsonl,
                                    effect_conflicts_chunks: &options.effect_conflicts_chunks,
                                    full_reports: &options.full_reports,
                                    full_report_chunks: &options.full_report_chunks,
                                    gate_rejection: options.gate_rejection,
                                    assessment_only: options.assessment_only,
                                    export_reentry_reports: &export_reentry_reports,
                                    vendor_import_oracle: vendor_import_oracle.as_ref(),
                                    export_reentry_policies: &export_reentry_policies,
                                },
                                spec: ChunkSpec {
                                    chunk_renames,
                                    unassigned_mode,
                                },
                            },
                            frontend_chunk,
                        )
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let claimed = GLOBALS
        .with(|globals| {
            prepared
                .into_par_iter()
                .map(|chunk| GLOBALS.set(globals, || claim_prepared_chunk(chunk)))
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let completed =
        |base: &AnalysisSpec| frontend_elaboration::complete_analysis_spec(base, &projection);
    let (program_summary, mut loaded_chunks) = match (&options.analysis, loaded) {
        (AnalysisMode::Load { base, .. }, Some((header, body))) => {
            let completed = completed(base)?;
            if completed.encode()? != header.projection {
                let differences =
                    completed.differences(&AnalysisSpec::decode(&header.projection)?)?;
                bail!(
                    "analysis artifact was computed for another projection; differing: {}. Rerun `debundle spec project-analysis` and `debundle analyze`",
                    differences.join(", ")
                );
            }
            (body.program_summary, Some(body.chunks))
        }
        _ => (
            program_summary::collect_program_summary(
                &artifact,
                &artifact_indexes,
                &projection,
                program_summary::ProgramSummaryOptions {
                    graph_provenance: options.graph_provenance.as_ref(),
                    vendor_plan: Some(vendor_plan),
                    replacement_packages: &vendor_plan.replacement_packages(),
                },
                Some(&program_summary_chunk_closure),
                &reparsed,
            )?,
            None,
        ),
    };
    let claimed = claimed
        .into_iter()
        .map(|chunk| {
            let facts = loaded_chunks
                .as_mut()
                .map(|chunks| {
                    chunks.remove(chunk.chunk_id()).with_context(|| {
                        format!(
                            "analysis artifact has no facts for chunk {}",
                            chunk.chunk_id()
                        )
                    })
                })
                .transpose()?;
            Ok((chunk, facts))
        })
        .collect::<Result<Vec<_>>>()?;
    let program_graph = ProgramGraphOutput::new(
        options.graph_provenance.as_ref(),
        program_summary.native_integrity.as_ref(),
        AuthorInputLocations::new(options.graph_provenance.as_ref(), chunk_analysis_options)?,
    );
    let chunk_results = GLOBALS
        .with(|globals| {
            claimed
                .into_par_iter()
                .map(|(chunk, facts)| {
                    GLOBALS.set(globals, || {
                        finish_logical_chunk(
                            chunk,
                            &projection,
                            &program_summary,
                            facts,
                            &program_graph,
                            GraphConstructionContext {
                                chunk_inputs: &graph_inputs,
                                graph_provenance: options.graph_provenance.as_ref(),
                                resolved_member_sites: &resolved_member_sites,
                            },
                        )
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;

    if options.assessment_only {
        let assessments = chunk_results
            .into_iter()
            .map(|result| match result {
                FinishedLogicalChunk::Assessed {
                    chunk_id,
                    assessment,
                } => Ok((chunk_id, *assessment)),
                FinishedLogicalChunk::Lowered(_) => {
                    bail!("assessment-only materialization unexpectedly lowered a chunk")
                }
            })
            .collect::<Result<Vec<_>>>()?;
        return Ok(Materialized::Assessed(assessments));
    }
    let chunk_results = chunk_results
        .into_iter()
        .map(|result| match result {
            FinishedLogicalChunk::Lowered(chunk) => Ok(*chunk),
            FinishedLogicalChunk::Assessed { .. } => {
                bail!("normal materialization unexpectedly returned an assessment")
            }
        })
        .collect::<Result<Vec<_>>>()?;

    let mut reports = Vec::with_capacity(chunk_results.len());
    let mut applied = Vec::<SelectedModuleLowering>::new();
    let mut unmatched_spec_claims = Vec::<UnmatchedSpecClaim>::new();
    let mut gate_rejections = Vec::<ChunkGateRejection>::new();
    let mut vendor_reference_rewrites = BTreeMap::<(ChunkId, String), usize>::new();
    for chunk_result in &chunk_results {
        if let Some(report_out_dir) = options.report_emission.full_dir() {
            write_chunk_report_json(
                report_out_dir,
                artifact.chunk_table.name(chunk_result.chunk_id),
                MODULES_REPORT,
                &chunk_result.report,
            )?;
        }
        applied.extend(chunk_result.applied.iter().cloned());
        reports.push(chunk_result.report.clone());
        unmatched_spec_claims.extend(chunk_result.unmatched_spec_claims.iter().cloned());
        gate_rejections.extend(chunk_result.gate_rejection.iter().cloned());
        for (key, count) in &chunk_result.vendor_reference_rewrites {
            *vendor_reference_rewrites.entry(key.clone()).or_insert(0) += count;
        }
    }
    let EmissionChunkOutputs {
        files,
        decomposition_by_chunk,
    } = collect_materialized_logical_chunks(artifact, &target_dir, chunk_results)?;
    let output = CandidateEmissionDraft::materialized(
        files,
        Arc::clone(vendor_plan),
        applied.clone(),
        options.chunk_ids.iter().cloned().collect(),
        vendor_reference_rewrites,
    );

    let module_count: usize = reports.iter().map(|r| r.final_module_contents.len()).sum();
    Ok(Materialized::Lowered(Box::new(
        MaterializeLogicalModulesResult {
            output,
            decomposition_by_chunk,
            selected_lowerings: applied,
            module_count,
            unmatched_spec_claims,
            gate_rejections,
        },
    )))
}
