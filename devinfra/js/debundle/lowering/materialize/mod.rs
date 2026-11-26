//! Per-chunk materialization: take an `DependencyGraphAndAtomicGroups` + spec plan, run the
//! chunk through candidate preparation/emission, and emit a `MaterializedLogicalChunk` whose
//! files/applied/report stay separate from the source bundle until the
//! finalized emit-stage file set is constructed.

mod apply;
pub(super) mod assignment;
mod gate_reports;
mod outcome_sink;
mod plan_builder;
mod residual_atomic_report;
mod residual_scc;

use super::commuting_statements::resolve_chunk_commuting_statements;
use super::io::{ProgramGraphOutput, write_chunk_report_json};
use super::target_paths::target_file_for_request;
use crate::imports::{ArtifactSourceImportResolutionCache, VendorReimportOracle};
use crate::lower::{
    LowerChunkAst, LowerChunkContext, LowerChunkInputs, LowerChunkPlan, LowerChunkSpecFacts,
    LoweredChunk, prepare_candidate_emission_plan,
};
use crate::placement::EmissionPlacement;
use crate::plans::ModulePlan;
use crate::{
    AtomicGroupOwnerCompletionReport, ChunkModulesCounts, ChunkModulesReport, ExportReentryReport,
    GateRejectionPolicy, GraphReportSelection, ReportEmission, RequestedLogicalModule,
};
use analysis::facts::{ChunkFactAnalysis, ExportReentryPolicy, StructuralChunkSourceFacts};
use analysis::{EntryDestination, ImportedSourceTarget, PlannedModule, top_level_id};
use analysis_projection::AnalysisProjection;
use anyhow::{Context, Result, ensure};
pub(super) use apply::{EmissionChunkOutputs, collect_materialized_logical_chunks};
use artifact::reports::{ChunkValidationSummary, DirectoryDependencyFact, SelectedModuleLowering};
use artifact::{ArtifactIndexes, ChunkBundle, JsFile};
use assignment::{
    FinalizedLogicalAssignment, GraphConstructionContext, SelectedAssignmentBuildInput,
    finalize_selected_assignment,
};
use chunk_analysis::build_chunk_graph;
use frontend_elaboration::chunk_ast::ChunkAstAnalysis;
use frontend_elaboration::{FrontendChunk, MemberClaimProjection};
use gate::ChunkGateRejection;
use gate_reports::{
    GateOutcome, build_directory_dependency_facts, build_final_module_report,
    evaluate_and_gate_chunk, evaluate_with_sinks, write_selector_diagnostics,
    write_spec_lint_report,
};
use graph::report::source_unit_key;
use graph::{CommutingStatementsDiagnostic, PlacementProjectionInput, SourceUnitLayout};
use js_ast::{ParsedJsModule, format_comment_block_lines};
use output_layout::{COMMUTING_STATEMENTS_DIAGNOSTICS_REPORT, GATE_REJECTED_MARKER};
use ownership_subset::OwnershipSubset;
use plan_builder::{ChunkPlanBuilder, ExplicitRequestContext};
use progress::{Phase, State};
use source_identity::ChunkId;
use spec::{ChunkRenames, UnassignedMode};
use spec_resolution::LogicalRequest;
use staged_analysis::chunk_facts::{
    ChunkFacts, ChunkFactsContext, LogicalChunkFacts, compute_logical_chunk_facts,
};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

/// Per-chunk inputs that identify the chunk and where its outputs
/// should land. Bundled into `MaterializeLogicalChunkInputs::context`.
#[derive(Clone, Copy)]
pub(super) struct ChunkContext<'a> {
    pub(super) diagnostics: &'a analysis::diagnostics::Diagnostics<'a>,
    pub(super) ownership_subset: Option<&'a OwnershipSubset>,
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) chunk_id: &'a str,
    pub(super) target_dir: &'a str,
    pub(super) keep_going: bool,
    pub(super) list_template_identifiers: bool,
    pub(super) report_emission: &'a ReportEmission,
    pub(super) graph_reports: GraphReportSelection,
    pub(super) effect_conflicts_jsonl: bool,
    pub(super) effect_conflicts_chunks: &'a BTreeSet<String>,
    pub(super) full_reports: &'a BTreeSet<super::FullReportKind>,
    pub(super) full_report_chunks: &'a BTreeSet<String>,
    pub(super) gate_rejection: GateRejectionPolicy,
    pub(super) assessment_only: bool,
    pub(super) export_reentry_policies: &'a BTreeMap<ChunkId, ExportReentryPolicy>,
    pub(super) export_reentry_reports: &'a BTreeMap<ChunkId, ExportReentryReport>,
    /// Vendor-plan consultation for runtime re-import construction;
    /// `None` when no vendor mark could
    /// affect construction.
    pub(super) vendor_import_oracle: Option<&'a VendorReimportOracle<'a>>,
}

/// Spec-derived per-chunk planning inputs: logical-module layout, chunk
/// renames and unassigned-mode. Analysis reads the spec through
/// `AnalysisProjection` instead. Bundled into
/// `MaterializeLogicalChunkInputs::spec`.
#[derive(Clone, Copy)]
pub(super) struct ChunkSpec<'a> {
    pub(super) chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub(super) unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub(super) import_order: &'a BTreeMap<String, Vec<spec::ModulePath>>,
}

pub(super) struct MaterializeLogicalChunkInputs<'a> {
    pub(super) context: ChunkContext<'a>,
    pub(super) spec: ChunkSpec<'a>,
}

pub(super) struct MaterializedLogicalChunk {
    pub(super) chunk_id: ChunkId,
    target_file: String,
    source_path: String,
    pub(super) files: Vec<JsFile>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    pub(super) directory_dependency_facts: Vec<DirectoryDependencyFact>,
    pub(super) validation: ChunkValidationSummary,
    pub(super) report: ChunkModulesReport,
    /// Per-symbol vendor-swap rewrite counts applied at construction
    /// time in this chunk's materialized module bodies; rolled up by
    /// `materialize_logical_modules` for the pipeline's manifest fold.
    pub(super) vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
    /// Spec member claims that named a binding for which no
    /// top-level declaration exists in this chunk. Materialization
    /// continues — the binding silently falls through to the
    /// residual sweep — but `materialize_logical_modules` rolls
    /// these up across every chunk and fails the pipeline at the
    /// end with the full list.
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
    /// The gate rejected this chunk and [`GateRejectionPolicy::LowerUnrunnable`]
    /// lowered it anyway; its entry file carries the banner.
    pub(super) gate_rejection: Option<ChunkGateRejection>,
}

pub(super) enum FinishedLogicalChunk {
    Lowered(Box<MaterializedLogicalChunk>),
    Assessed {
        chunk_id: String,
        assessment: Box<gate::CandidateRelocationAssessmentReport>,
    },
}

/// Chunk plan and frontend claims still to apply.
pub(super) struct PreparedLogicalChunk<'a> {
    plan: LogicalChunkPlan<'a>,
    explicit_requests: Vec<LogicalRequest>,
    resolution: selector_resolve::Resolution,
    resolved_members: Vec<Vec<usize>>,
    member_claims: MemberClaimProjection,
}

pub(super) struct LogicalChunkPlan<'a> {
    context: ChunkContext<'a>,
    spec: ChunkSpec<'a>,
    chunk_unassigned_mode: UnassignedMode,
    chunk_id_interned: ChunkId,
    runtime_file: &'a JsFile,
    runtime_ast: &'a ParsedJsModule,
    ast_analysis: ChunkAstAnalysis,
    requests: Vec<LogicalRequest>,
    placement_anchors: Option<selector_resolve::PlacementAnchors>,
    structural_analysis: StructuralChunkSourceFacts<'a>,
    source_only_initializer_bindings: BTreeSet<String>,
    external_initializer_contracts: BTreeMap<String, spec::InitializerExternalEffects>,
    builder: ChunkPlanBuilder,
}

/// Construct output-plan state from source-level facts and the exact shared
/// frontend resolution. This phase does not project or solve selectors.
pub(super) fn prepare_logical_chunk<'a>(
    inputs: MaterializeLogicalChunkInputs<'a>,
    frontend: FrontendChunk<'a>,
) -> Result<PreparedLogicalChunk<'a>> {
    let MaterializeLogicalChunkInputs { context, spec } = inputs;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        diagnostics,
        target_dir,
        keep_going,
        list_template_identifiers,
        ..
    } = context;
    // The spec validator (`validate_transform_spec`) enforces that
    // every materialised chunk has an `unassigned_mode` entry, so
    // this lookup must not miss. Missing here is a bug in the
    // validator, not a recoverable spec error.
    let chunk_unassigned_mode = spec
        .unassigned_mode
        .get(chunk_id)
        .cloned()
        .with_context(|| {
            format!("materialize_logical_modules missing unassigned_mode for chunk: {chunk_id}")
        })?;
    let FrontendChunk {
        chunk_id: frontend_chunk_id,
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        explicit_requests,
        structural_analysis,
        resolution,
        placement_anchors,
        resolved_members,
        member_claims,
        source_only_initializer_bindings,
        external_initializer_contracts,
        ..
    } = frontend;
    ensure!(frontend_chunk_id == chunk_id, "frontend chunk id mismatch");
    validate_partial_atomic_group_completion_owners(
        &chunk_unassigned_mode,
        &explicit_requests,
        chunk_id,
    )?;
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    diagnostics.progress(chunk_id, Phase::MaterializeLogicalChunk, State::Start);
    let mut explicit_requests = explicit_requests;

    let mut builder = ChunkPlanBuilder::new(!keep_going, list_template_identifiers);
    let mut imported_binding_resolver =
        ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);
    let mut imported_source_by_src = BTreeMap::<String, ImportedSourceTarget>::new();
    let explicit_request_ctx = ExplicitRequestContext {
        body: &runtime_ast.module.body,
        declaration_by_name: &ast_analysis.declaration_by_name,
        chunk_top_level_mark,
        target_dir,
        chunk_id,
        target_file: &runtime_file.path,
        runtime_import_facts: &ast_analysis.runtime_import_facts,
    };
    for (index, request) in explicit_requests.iter_mut().enumerate() {
        builder.add_explicit_request(
            index,
            request,
            &explicit_request_ctx,
            &mut imported_binding_resolver,
            &mut imported_source_by_src,
        )?;
    }
    builder.drop_explicit_request_scratch();
    builder.apply_destructuring_group_placement(&ast_analysis.destructuring_groups);

    Ok(PreparedLogicalChunk {
        plan: LogicalChunkPlan {
            context,
            spec,
            chunk_unassigned_mode,
            chunk_id_interned,
            runtime_file,
            runtime_ast,
            ast_analysis,
            requests,
            placement_anchors,
            structural_analysis,
            source_only_initializer_bindings,
            external_initializer_contracts,
            builder,
        },
        explicit_requests,
        resolution,
        resolved_members,
        member_claims,
    })
}

/// Validate the coarse-mode owner opt-in against the complete authored request
/// list, before ownership-subset filtering can remove a requested module.
fn validate_partial_atomic_group_completion_owners(
    unassigned_mode: &UnassignedMode,
    authored_requests: &[LogicalRequest],
    chunk_id: &str,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    for owner_path in unassigned_mode.partial_atomic_group_completion_owners() {
        ensure!(
            seen.insert(owner_path),
            "unassigned_mode for chunk {chunk_id} repeats complete_partial_atomic_groups_for module {owner_path:?}"
        );
        ensure!(
            authored_requests
                .iter()
                .any(|request| request.target_path == *owner_path),
            "unassigned_mode for chunk {chunk_id} names {owner_path:?} in complete_partial_atomic_groups_for, but that path is not an authored logical_modules request"
        );
    }
    Ok(())
}

/// Apply source-selected claims before the cross-chunk purity analysis.
pub(super) fn claim_prepared_chunk(
    prepared: PreparedLogicalChunk<'_>,
) -> Result<LogicalChunkPlan<'_>> {
    let PreparedLogicalChunk {
        mut plan,
        explicit_requests,
        resolution,
        resolved_members,
        member_claims,
    } = prepared;
    let chunk_top_level_mark = plan.runtime_ast.top_level_mark;
    plan.builder.claim_resolution_at_offset(
        0,
        &explicit_requests,
        resolution,
        resolved_members,
        &member_claims,
        chunk_top_level_mark,
        plan.context.chunk_id,
        &plan.runtime_ast.module.body,
        &plan.ast_analysis.declaration_by_name,
    )?;
    plan.builder
        .apply_destructuring_group_placement(&plan.ast_analysis.destructuring_groups);
    plan.builder
        .adopt_bindings_of_claimed_anonymous_statements(&plan.ast_analysis.declarations);
    Ok(plan)
}

impl LogicalChunkPlan<'_> {
    pub(super) fn chunk_id(&self) -> &str {
        self.context.chunk_id
    }
}

/// Analyze, plan and lower a chunk whose selector claims are already resolved.
pub(super) fn finish_logical_chunk(
    claimed: LogicalChunkPlan<'_>,
    projection: &AnalysisProjection<'_>,
    program_summary: &super::program_summary::ProgramSummary,
    loaded_facts: Option<ChunkFactAnalysis>,
    program_graph: &ProgramGraphOutput<'_>,
    graph_context: GraphConstructionContext<'_>,
) -> Result<FinishedLogicalChunk> {
    let LogicalChunkPlan {
        structural_analysis,
        source_only_initializer_bindings,
        external_initializer_contracts,
        context,
        spec,
        chunk_unassigned_mode,
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        builder,
        placement_anchors,
        ..
    } = claimed;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        diagnostics,
        target_dir,
        ownership_subset,
        assessment_only,
        vendor_import_oracle,
        export_reentry_policies,
        ..
    } = context;
    let placement_modules = ownership_subset
        .map(|view| view.placements_for_chunk(chunk_id))
        .transpose()?
        .unwrap_or_default()
        .into_iter()
        .map(|(path, module)| (path.to_string(), module))
        .collect::<BTreeMap<_, _>>();
    // Keep the canonical census for the later placement-only solve. This is
    // one bounded preparation copy for a chunk with placement claims, never a
    // reparse, a second structural analysis, or work repeated per peel seed.
    let placement_source = (!placement_modules.is_empty()).then(|| {
        let census = Cow::Owned(structural_analysis.clone());
        selector_resolve::Chunk::new(chunk_id, &runtime_ast.module, census)
    });
    let graph_inputs = graph_context
        .chunk_inputs
        .get(chunk_id)
        .cloned()
        .unwrap_or_default();
    let graph_options = graph_inputs.options;
    let commuting_statements = resolve_chunk_commuting_statements(
        chunk_id,
        &runtime_ast.module,
        &structural_analysis,
        &graph_inputs.commuting_statements,
        graph_context.graph_provenance,
        graph_context.resolved_member_sites,
    )?;
    // Chunk analysis: hint-sensitive facts, then the dependency graph and
    // structural atomic groups. See `chunk_analysis/mod.rs`.
    diagnostics.progress(chunk_id, Phase::ComputeChunkAnalysis, State::Start);
    let LogicalChunkFacts {
        facts: mut analysis,
        imported_callables,
        redundant_admission_overrides,
    } = compute_logical_chunk_facts(
        ChunkFactsContext {
            artifact,
            artifact_indexes,
            chunk_id,
            chunk_id_interned,
            target_file: &runtime_file.path,
            runtime_ast,
            source_path: &runtime_file.metadata.source_path,
            diagnostics: context.diagnostics,
            export_reentry_policy: export_reentry_policies
                .get(&chunk_id_interned)
                .unwrap_or(&ExportReentryPolicy::Conservative),
        },
        match loaded_facts {
            Some(facts) => ChunkFacts::Loaded(facts),
            None => ChunkFacts::Compute(structural_analysis),
        },
        projection,
        program_summary,
    )?;
    analysis.apply_initializer_binding_effects(&source_only_initializer_bindings);
    analysis
        .validate_initializer_binding_effects(&source_only_initializer_bindings)
        .with_context(|| format!("validating initializer binding effects in chunk `{chunk_id}`"))?;
    analysis
        .apply_initializer_external_effects(&external_initializer_contracts)
        .with_context(|| format!("validating initializer heap effects in chunk `{chunk_id}`"))?;
    let precomputed = build_chunk_graph_or_report_contradiction(
        context,
        &analysis,
        graph_options,
        &commuting_statements,
        &builder,
    )?;
    record_unused_commuting_statements(context, &precomputed.unused_commuting_statements)?;
    write_spec_lint_report(
        context,
        &precomputed.unused_commuting_statements,
        &redundant_admission_overrides,
        &analysis,
        graph_context.resolved_member_sites,
        projection,
    )?;
    diagnostics.progress(chunk_id, Phase::ComputeChunkAnalysis, State::End);
    let finalized = finalize_selected_assignment(SelectedAssignmentBuildInput {
        context,
        spec,
        chunk_unassigned_mode,
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        builder,
        placement_anchors,
        placement_source,
        placement_modules,
        precomputed,
        projection,
    })?;
    let FinalizedLogicalAssignment {
        chunk_unassigned_mode,
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        selected_ownership: ownership_subset,
        source_unit_layout,
        module_plans,
        whole_statement_assignment,
        atomic_group_owner_completions,
        unmatched_spec_claims,
        assignment_analysis,
        sealed_renames,
        chunk_renames_map,
        ..
    } = finalized;
    let ChunkAstAnalysis {
        runtime_import_facts,
        declarations,
        declaration_by_name,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
        ..
    } = &ast_analysis;
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    let redundant_purity_hints = analysis.redundant_purity_hints;
    let assignment = assignment_analysis.assignment();
    let graph = assignment_analysis.inputs().dependency_graph();
    let local_bindings = assignment_analysis
        .inputs()
        .local_bindings(chunk_id_interned)?;
    let placement_projection = graph::project_placement(PlacementProjectionInput {
        graph,
        assignment,
        local_bindings,
        body: &runtime_ast.module.body,
        layout: &source_unit_layout,
        whole_statements: &whole_statement_assignment,
        module_count: module_plans.len(),
        destructuring_groups: &ast_analysis.destructuring_groups,
        nested_var_statements: &ast_analysis.nested_var_statements,
    });
    let preflight = gate::CandidatePreflight::prepare(&assignment_analysis, &placement_projection);
    if assessment_only {
        let evaluation = evaluate_with_sinks(preflight, &analysis.facts, None, None)?;
        let assessment = evaluation.into_candidate_assessment();
        return Ok(FinishedLogicalChunk::Assessed {
            chunk_id: chunk_id.to_string(),
            assessment: Box::new(assessment),
        });
    }
    let gate_outcome = evaluate_and_gate_chunk(
        context,
        ownership_subset.as_ref(),
        preflight,
        &analysis.facts,
        program_graph,
        chunk_id_interned,
    )?;

    let (linker_order, gate_rejection) = match &gate_outcome {
        GateOutcome::Accepted(validated) => (validated.report().linker_order.clone(), None),
        GateOutcome::Rejected {
            assignment,
            rejection,
        } => (
            assignment.report().linker_order.clone(),
            Some(rejection.clone()),
        ),
    };
    let placement =
        EmissionPlacement::from_projection(&assignment_analysis, &placement_projection)?;

    let mut prepared_emission = prepare_candidate_emission_plan(LowerChunkInputs {
        context: LowerChunkContext {
            artifact,
            artifact_indexes,
            chunk_id,
            chunk_id_interned,
            source_path: &runtime_file.metadata.source_path,
            entry_file: &runtime_file.path,
            header_lines: &runtime_file.header_lines,
            vendor_import_oracle,
        },
        ast: LowerChunkAst {
            runtime_ast,
            declarations,
            declaration_by_name,
            chunk_top_level_mark,
        },
        plan: LowerChunkPlan {
            module_plans: &module_plans,
            placement: &placement,
        },
        spec_facts: LowerChunkSpecFacts {
            runtime_import_facts,
            sealed_renames: &sealed_renames,
            chunk_renames: &chunk_renames_map,
            pre_existing_entry_exports,
            pre_existing_public_export_names,
        },
    })?;
    if let Some(rejection) = &gate_rejection {
        // The banner leads the entry file so a reader (or a loader) meets
        // it before any code; the chunk's own header follows.
        prepared_emission.prepend_entry_header_lines(gate_rejected_banner_lines(rejection));
    }
    let LoweredChunk {
        files,
        applied,
        vendor_reference_rewrites,
    } = prepared_emission.emit();

    let final_modules = build_final_module_report(
        &module_plans,
        &assignment_analysis,
        chunk_id_interned,
        chunk_top_level_mark,
    );
    let entry_destination_report_target_file = chunk_unassigned_mode
        .catchall_file_target()
        .map(|target| target_file_for_request(target_dir, target))
        .transpose()?
        .unwrap_or_else(|| runtime_file.path.clone());
    let directory_dependency_facts = build_directory_dependency_facts(
        chunk_id,
        &entry_destination_report_target_file,
        &assignment_analysis,
    );
    let validation = ChunkValidationSummary { linker_order };
    let report = ChunkModulesReport {
        ownership_subset,
        chunk_id: chunk_id.to_string(),
        counts: ChunkModulesCounts {
            applied: applied.len(),
            selected_bindings: placement.bindings().len(),
        },
        final_module_contents: final_modules,
        atomic_group_owner_completions: atomic_group_owner_completion_reports(
            atomic_group_owner_completions,
        ),
        requested_logical_modules: requested_logical_module_reports(&requests),
        imported_callables,
        redundant_purity_hints,
    };
    Ok(FinishedLogicalChunk::Lowered(Box::new(
        MaterializedLogicalChunk {
            chunk_id: chunk_id_interned,
            unmatched_spec_claims,
            target_file: runtime_file.path.clone(),
            source_path: runtime_file.metadata.source_path.clone(),
            files,
            applied,
            directory_dependency_facts,
            validation,
            report,
            vendor_reference_rewrites,
            gate_rejection,
        },
    )))
}

fn build_chunk_graph_or_report_contradiction(
    context: ChunkContext<'_>,
    facts: &ChunkFactAnalysis,
    graph_options: graph::GraphOptions,
    commuting_statements: &[analysis_vocabulary::inputs::ResolvedCommutingStatements],
    builder: &ChunkPlanBuilder,
) -> Result<graph::DependencyGraphAndAtomicGroups> {
    let ChunkContext {
        chunk_id,
        report_emission,
        ..
    } = context;
    match build_chunk_graph(chunk_id, facts, graph_options, commuting_statements) {
        Ok(precomputed) => Ok(precomputed),
        Err(error) => {
            // A contradicted `commuting_statements` entry is an
            // author-premise diagnostic, not a pipeline fault: record it,
            // with the selector outcomes gathered so far, where `spec
            // validate` collects the chunk's reports; the chunk still fails.
            if let Some(contradiction) = error
                .downcast_ref::<graph::DependencyGraphBuildError>()
                .and_then(graph::DependencyGraphBuildError::commuting_statements_contradiction)
                && let Some(report_out_dir) = report_emission.rejection_dir()
            {
                write_selector_diagnostics(builder, report_out_dir, chunk_id)?;
                write_chunk_report_json(
                    report_out_dir,
                    chunk_id,
                    COMMUTING_STATEMENTS_DIAGNOSTICS_REPORT,
                    &[CommutingStatementsDiagnostic::Contradicted(
                        contradiction.clone(),
                    )],
                )?;
            }
            Err(error)
        }
    }
}

fn record_unused_commuting_statements(
    context: ChunkContext<'_>,
    unused_commuting_statements: &[analysis::reports::spec_lint::CommutingStatementsUnused],
) -> Result<()> {
    let ChunkContext {
        chunk_id,
        report_emission,
        ..
    } = context;
    // An unused entry is a warning: the graph is exactly the graph without it.
    for unused in unused_commuting_statements {
        context.diagnostics.record_chunk(
            chunk_id,
            analysis::diagnostics::ChunkDiagnostic::UnusedCommutingStatements(Box::new(
                unused.clone(),
            )),
        );
    }
    if !unused_commuting_statements.is_empty()
        && let Some(report_out_dir) = report_emission.rejection_dir()
    {
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            COMMUTING_STATEMENTS_DIAGNOSTICS_REPORT,
            &unused_commuting_statements
                .iter()
                .map(|unused| CommutingStatementsDiagnostic::Unused(Box::new(unused.clone())))
                .collect::<Vec<_>>(),
        )?;
    }
    Ok(())
}

fn atomic_group_owner_completion_reports(
    completions: Vec<plan_builder::AtomicGroupOwnerCompletion>,
) -> Vec<AtomicGroupOwnerCompletionReport> {
    completions
        .into_iter()
        .map(|completion| AtomicGroupOwnerCompletionReport {
            owner_path: spec::ModulePath::parse(&completion.owner_path, "")
                .expect("owner path was validated against authored requests"),
            atomic_group_source_unit_ids: completion
                .atomic_group_source_unit_ids
                .into_iter()
                .map(source_unit_key)
                .collect(),
            adopted_source_unit_ids: completion
                .adopted_source_unit_ids
                .into_iter()
                .map(source_unit_key)
                .collect(),
        })
        .collect()
}

fn requested_logical_module_reports(requests: &[LogicalRequest]) -> Vec<RequestedLogicalModule> {
    requests
        .iter()
        .map(|request| RequestedLogicalModule {
            target_path: spec::ModulePath::parse(&request.target_path, "")
                .expect("request target_path is a canonical module path"),
            residual: request.residual,
        })
        .collect()
}

/// `// …` lines announcing a gate-rejected chunk at the top of its entry file.
fn gate_rejected_banner_lines(rejection: &ChunkGateRejection) -> Vec<String> {
    format_comment_block_lines(&format!(
        "{GATE_REJECTED_MARKER}: NOT RUNNABLE. The realizability gate rejected chunk \
         `{chunk}` ({verdict}); `debundle run --emit-on-rejection` wrote this tree for \
         reading only.\n{evidence}\nSee the {GATE_REJECTED_MARKER} marker at the output root.",
        chunk = rejection.chunk_id,
        verdict = rejection.verdict,
        evidence = match &rejection.evidence_report {
            Some(path) => format!("Evidence: {}", path.display()),
            None => "No report directory was configured; the rejection is on the run's stderr."
                .to_string(),
        },
    ))
}

/// Project the materializer's real output plans into the analysis view. The
/// entry is a typed destination after the planned outputs, not a fabricated
/// `PlannedModule`; its wire label is chosen only when constructing reports.
pub(super) fn project_planned_modules(
    module_plans: &[ModulePlan],
    source_unit_layout: &SourceUnitLayout,
    chunk_top_level_mark: swc_common::Mark,
) -> Result<(Vec<PlannedModule>, EntryDestination)> {
    let mut planned_modules = Vec::with_capacity(module_plans.len());
    for plan in module_plans {
        let mut anonymous_source_unit_ordinals = Vec::new();
        for statement in &plan.anonymous_statements {
            let first = source_unit_layout.for_statement(*statement).next().with_context(|| {
                format!("source statement at body index {statement} has no checked source-unit layout")
            })?;
            // Preserve the existing assignment-assembly contract: one
            // anonymous request points at the raw statement's first ordinal.
            // The shared projection checks every source unit for that body
            // item before emission and reports any split as a blocker.
            anonymous_source_unit_ordinals.push(first.ordinal);
        }
        planned_modules.push(PlannedModule {
            id: plan.id.clone(),
            target_file: plan.target_file.clone(),
            role: plan.role,
            rename_map: plan
                .bindings
                .iter()
                .map(|(local, exported)| {
                    (
                        top_level_id(local, chunk_top_level_mark),
                        exported.as_str().into(),
                    )
                })
                .collect(),
            // AssignmentAnalysis's dependency graph uses post-comma-list-split
            // `SourceUnitOrdinals`; convert each `StatementId` (the original
            // `Module.body` position) here so the destination override targets
            // the corresponding source unit. Anonymous body items are single
            // post-split positions, but earlier comma-list declarations shift
            // the source-unit ordinal.
            anonymous_source_unit_ordinals,
        });
    }
    let entry_destination = EntryDestination::after_planned_modules(planned_modules.len());
    Ok((planned_modules, entry_destination))
}
