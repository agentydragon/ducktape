//! Per-chunk materialization: assess a concrete spec assignment, run the
//! chunk through candidate preparation/emission, and emit a
//! `MaterializedLogicalChunk` whose
//! files/applied/report stay separate from the source bundle until the
//! finalized emit-stage file set is constructed.

mod apply;
mod outcome_sink;
mod plan_builder;
mod vendor_replacements;

pub(super) use apply::{
    EmissionChunkOutputs, collect_materialized_logical_chunks, raw_source_correspondence,
};
use frontend_elaboration::ResolvedMemberSite;
use plan_builder::{ChunkPlan, ChunkPlanBuilder, ExplicitRequestContext};
use source_identity::{ChunkId, StatementId};
use staged_analysis::chunk_facts::{ChunkFacts, ChunkFactsContext, compute_logical_chunk_facts};
use staged_analysis::progress::emit_debundle_progress;

use super::commuting_statements::resolve_chunk_commuting_statements;
use super::io::{JsonlReport, ProgramGraphOutput, chunk_report_path, write_chunk_report_json};
use super::util::{render_atomic_group_cause_guidance, target_file_for_request};
use crate::chunk_renames::collect_chunk_renames;
use crate::exports::PublicExportSurface;
use crate::imports::{ArtifactSourceImportResolutionCache, VendorReimportOracle};
use crate::lower::{
    LowerChunkAst, LowerChunkContext, LowerChunkInputs, LowerChunkPlan, LowerChunkSpecFacts,
    LoweredChunk, prepare_candidate_emission_plan,
};
use crate::naturalize::collect_plan_export_rename_intents;
use crate::ownership_subset::{OwnershipSubset, SelectedModule};
use crate::placement::{EmissionPlacement, diagnostic_with_destination_context};
use crate::plans::ModulePlan;
use crate::rename_ledger::{RenameLedger, SealValidation};
use crate::{
    AtomicGroupOwnerCompletionReport, BindingSourceUnitAssociation, ChunkModulesCounts,
    ChunkModulesReport, ExportReentryReport, FinalModuleContent, GateRejectionPolicy,
    GraphReportSelection, ReportEmission, RequestedLogicalModule,
};
use analysis::facts::{ChunkFactAnalysis, ExportReentryPolicy, StructuralChunkSourceFacts};
use analysis::reports::source_unit_key;
use analysis::{
    BindingCatalogue, BindingId, BindingKind, EntryDestination, ModuleId, ModuleRole,
    PlacementProjectionInput, PlannedModule, SourceUnitLayout, top_level_id,
};
use analysis_projection::{AnalysisProjection, ChunkGraphInputs};
use anyhow::{Context, Result, bail, ensure};
use artifact::paths::join_module_path;
use artifact::reports::{
    CandidateImportRoute, CandidateModuleCellDeclaration, CandidateSourceBindingRoute,
    CandidateSourceCorrespondence, CandidateSourceExportRoutes, CandidateSourceImportRoutes,
    CandidateSourcePosition, CandidateSourceReExportRoutes, CandidateSourceRequestRoute,
    CandidateSourceStaticRequestRoutes, CandidateSourceUnitRoute, CandidateSourceUseRoute,
    ChunkValidationSummary, DirectoryDependencyFact, OutputFileAddress, ParsedSourceImports,
    ParsedSourceRequest, SelectedModuleLowering, SourceCorrespondenceStatus,
    SourceStaticRequestKind,
};
use artifact::{ArtifactIndexes, ChunkBundle, FileRole, JsFile};
use chunk_analysis::build_chunk_graph;
use frontend_elaboration::chunk_ast::{ChunkAstAnalysis, SourceExportIdentity};
use frontend_elaboration::{FrontendChunk, MemberClaimProjection};
use gate::{
    AssignmentAnalysis, CandidatePreparationOutcome, CandidatePreparationReadiness,
    RejectedAssignment, SccRejection, render_atomic_group_conflict_summary, render_cycle_summary,
};
use gate::{ChunkGateRejection, GateVerdict};
use graph_provenance::GraphProvenance;
use js_ast::{ParsedJsModule, format_comment_block_lines};
use output_layout::{
    ATOMIC_GROUP_CONFLICTS_REPORT, CANDIDATE_RELOCATION_ASSESSMENT_REPORT,
    COMMUTING_STATEMENTS_DIAGNOSTICS_REPORT, CYCLES_REPORT, DEPENDENCY_GRAPH_REPORT,
    EFFECT_TRANSLATION_VALIDATION_REPORT, EXPORT_REENTRY_REPORT, GATE_REJECTED_MARKER,
    SELECTOR_DIAGNOSTICS_REPORT, TRANSLATION_VALIDATION_REPORT,
};
use spec::{ChunkRenames, UnassignedMode};
use spec_resolution::LogicalRequest;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_common::{Mark, Span, Spanned};
use swc_ecma_ast::*;

/// Per-chunk inputs that identify the chunk and where its outputs
/// should land. Bundled into `MaterializeLogicalChunkInputs::context`.
pub(super) struct ChunkContext<'a> {
    pub(super) ownership_subset: Option<&'a OwnershipSubset>,
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) materialized_chunks: &'a BTreeSet<ChunkId>,
    pub(super) vendor_unchanged: bool,
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
pub(super) struct ChunkSpec<'a> {
    pub(super) chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub(super) unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
}

pub(super) struct MaterializeLogicalChunkInputs<'a> {
    pub(super) context: ChunkContext<'a>,
    pub(super) spec: ChunkSpec<'a>,
}

pub(super) struct MaterializedLogicalChunk {
    pub(super) chunk_id: ChunkId,
    pub(super) target_file: String,
    pub(super) source_path: String,
    pub(super) files: Vec<JsFile>,
    pub(super) file_records: Vec<(String, FileRole)>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    pub(super) directory_dependency_facts: Vec<DirectoryDependencyFact>,
    pub(super) validation: ChunkValidationSummary,
    pub(super) report: ChunkModulesReport,
    pub(super) source_correspondence: CandidateSourceCorrespondence,
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
    Lowered {
        chunk: Box<MaterializedLogicalChunk>,
        assessment: Box<gate::CandidateRelocationAssessmentReport>,
    },
    AssessedAndLowered {
        chunk: Box<MaterializedLogicalChunk>,
        assessment: Box<gate::CandidateRelocationAssessmentReport>,
    },
    Assessed {
        chunk_id: String,
        assessment: Box<gate::CandidateRelocationAssessmentReport>,
    },
}

/// Lowering state prepared before the shared spec-resolution service projects
/// and solves selector entities.
pub(super) struct PreparedLogicalChunk<'a> {
    context: ChunkContext<'a>,
    spec: ChunkSpec<'a>,
    chunk_unassigned_mode: UnassignedMode,
    chunk_id_interned: ChunkId,
    target_file: String,
    runtime_ast: &'a ParsedJsModule,
    header_lines: Vec<String>,
    source_path: String,
    ast_analysis: ChunkAstAnalysis,
    requests: Vec<LogicalRequest>,
    explicit_requests: Vec<LogicalRequest>,
    resolution: selector_resolve::Resolution,
    resolved_members: Vec<Vec<usize>>,
    member_claims: MemberClaimProjection,
    structural_analysis: Option<StructuralChunkSourceFacts<'a>>,
    source_only_initializer_bindings: BTreeSet<String>,
    builder: ChunkPlanBuilder,
}

/// A prepared chunk after exact selector resolution has claimed its selected
/// members and anonymous statements. The structural facts are retained for the
/// hint-sensitive analysis that follows claim application.
pub(super) struct ClaimedLogicalChunk<'a> {
    prepared: PreparedLogicalChunk<'a>,
    structural_analysis: StructuralChunkSourceFacts<'a>,
    /// Selector-resolved author overlay, applied after raw facts are computed or loaded.
    source_only_initializer_bindings: BTreeSet<String>,
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
        target_file,
        runtime_ast,
        header_lines,
        source_path,
        ast_analysis,
        requests,
        explicit_requests,
        structural_analysis,
        resolution,
        resolved_members,
        member_claims,
        source_only_initializer_bindings,
        ..
    } = frontend;
    ensure!(frontend_chunk_id == chunk_id, "frontend chunk id mismatch");
    validate_partial_atomic_group_completion_owners(
        &chunk_unassigned_mode,
        &explicit_requests,
        chunk_id,
    )?;
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    emit_debundle_progress(chunk_id, "materialize_logical_chunk", "start");
    let mut explicit_requests = explicit_requests;

    let mut builder = ChunkPlanBuilder::new(!keep_going, list_template_identifiers);
    let mut imported_binding_resolver =
        ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);
    let mut imported_from_by_src = BTreeMap::<String, String>::new();
    let explicit_request_ctx = ExplicitRequestContext {
        body: &runtime_ast.module.body,
        declaration_by_name: &ast_analysis.declaration_by_name,
        chunk_top_level_mark,
        target_dir,
        chunk_id,
        target_file: &target_file,
        runtime_import_facts: &ast_analysis.runtime_import_facts,
    };
    for (index, request) in explicit_requests.iter_mut().enumerate() {
        builder.add_explicit_request(
            index,
            request,
            &explicit_request_ctx,
            &mut imported_binding_resolver,
            &mut imported_from_by_src,
        )?;
    }
    builder.drop_explicit_request_scratch();
    builder.apply_destructuring_group_placement(&ast_analysis.destructuring_groups);

    Ok(PreparedLogicalChunk {
        context,
        spec,
        chunk_unassigned_mode,
        chunk_id_interned,
        target_file,
        runtime_ast,
        header_lines,
        source_path,
        ast_analysis,
        requests,
        explicit_requests,
        resolution,
        resolved_members,
        member_claims,
        structural_analysis: Some(structural_analysis),
        source_only_initializer_bindings,
        builder,
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

/// The file a chunk's modules are cut from (`materialize_logical_modules.file`,
/// else the chunk's entry) and its retained AST.
/// Resolve source-selected bindings before the cross-chunk purity oracle runs.
/// The same resolved hints are later used by this chunk's source unit analysis.
pub(super) fn claim_prepared_chunk<'a>(
    mut prepared: PreparedLogicalChunk<'a>,
) -> Result<ClaimedLogicalChunk<'a>> {
    let chunk_top_level_mark = prepared.runtime_ast.top_level_mark;
    prepared.builder.claim_resolution(
        &prepared.explicit_requests,
        std::mem::take(&mut prepared.resolution),
        std::mem::take(&mut prepared.resolved_members),
        &prepared.member_claims,
        chunk_top_level_mark,
        prepared.context.chunk_id,
        &prepared.runtime_ast.module.body,
        &prepared.ast_analysis.declaration_by_name,
    )?;
    prepared
        .builder
        .apply_destructuring_group_placement(&prepared.ast_analysis.destructuring_groups);
    prepared
        .builder
        .adopt_bindings_of_claimed_anonymous_statements(&prepared.ast_analysis.declarations);
    let source_only_initializer_bindings =
        std::mem::take(&mut prepared.source_only_initializer_bindings);
    let structural_analysis = prepared
        .structural_analysis
        .take()
        .expect("prepared frontend structure is present");
    Ok(ClaimedLogicalChunk {
        prepared,
        structural_analysis,
        source_only_initializer_bindings,
    })
}

impl ClaimedLogicalChunk<'_> {
    pub(super) fn chunk_id(&self) -> &str {
        self.prepared.context.chunk_id
    }
}

/// What resolving a chunk's `commuting_statements` reads besides the chunk:
/// the spec path its evidence names and the resolved logical-module members a
/// `resolved_member` endpoint may name.
pub(super) struct GraphConstructionContext<'a> {
    pub(super) chunk_inputs: &'a BTreeMap<String, ChunkGraphInputs>,
    pub(super) graph_provenance: Option<&'a GraphProvenance>,
    pub(super) resolved_member_sites: &'a BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
}

/// Analyze, plan and lower a chunk whose selector claims are already resolved.
pub(super) fn finish_logical_chunk(
    claimed: ClaimedLogicalChunk<'_>,
    projection: &AnalysisProjection<'_>,
    program_summary: &super::program_summary::ProgramSummary,
    loaded_facts: Option<ChunkFactAnalysis>,
    program_graph: &ProgramGraphOutput<'_>,
    graph_context: GraphConstructionContext<'_>,
) -> Result<FinishedLogicalChunk> {
    let ClaimedLogicalChunk {
        prepared,
        structural_analysis,
        source_only_initializer_bindings,
        ..
    } = claimed;
    let PreparedLogicalChunk {
        context,
        spec,
        chunk_unassigned_mode,
        chunk_id_interned,
        target_file,
        runtime_ast,
        header_lines,
        source_path,
        ast_analysis,
        requests,
        mut builder,
        ..
    } = prepared;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        target_dir,
        ownership_subset,
        materialized_chunks,
        vendor_unchanged,
        report_emission,
        graph_reports,
        effect_conflicts_jsonl,
        effect_conflicts_chunks,
        full_reports,
        full_report_chunks,
        gate_rejection: gate_rejection_policy,
        assessment_only,
        vendor_import_oracle,
        export_reentry_policies,
        export_reentry_reports,
        ..
    } = context;
    let ChunkSpec { chunk_renames, .. } = spec;
    let ChunkAstAnalysis {
        runtime_import_facts,
        declarations,
        declaration_by_name,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
        source_public_export_identities_by_statement,
        ..
    } = ast_analysis;
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    let graph_inputs = graph_context
        .chunk_inputs
        .get(chunk_id)
        .cloned()
        .unwrap_or_default();
    let graph_options = graph_inputs.options;
    let commuting_statements = resolve_chunk_commuting_statements(
        chunk_id,
        &runtime_ast.module.body,
        &graph_inputs.commuting_statements,
        graph_context.graph_provenance,
        graph_context.resolved_member_sites,
    )?;
    // Chunk analysis: hint-sensitive facts, then the dependency graph and
    // structural atomic groups. See `chunk_analysis/mod.rs`.
    emit_debundle_progress(chunk_id, "compute_chunk_analysis", "start");
    let (mut analysis, imported_callables) = compute_logical_chunk_facts(
        ChunkFactsContext {
            artifact,
            artifact_indexes,
            chunk_id,
            chunk_id_interned,
            target_file: &target_file,
            runtime_ast,
            source_path: &source_path,
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
    let precomputed =
        match build_chunk_graph(chunk_id, &analysis, graph_options, &commuting_statements) {
            Ok(precomputed) => precomputed,
            Err(error) => {
                // A rejected `commuting_statements` entry is an author-premise
                // diagnostic, not a pipeline fault: record it, with the selector
                // outcomes gathered so far, where `spec validate` collects the
                // chunk's reports; the chunk still fails.
                if let Some(diagnostic) = error
                    .downcast_ref::<analysis::DependencyGraphBuildError>()
                    .and_then(analysis::DependencyGraphBuildError::commuting_statements_diagnostic)
                    && let Some(report_out_dir) = report_emission.rejection_dir()
                {
                    write_selector_diagnostics(&builder, report_out_dir, chunk_id)?;
                    write_chunk_report_json(
                        report_out_dir,
                        chunk_id,
                        COMMUTING_STATEMENTS_DIAGNOSTICS_REPORT,
                        &[diagnostic],
                    )?;
                }
                return Err(error);
            }
        };
    emit_debundle_progress(chunk_id, "compute_chunk_analysis", "end");
    let source_unit_layout = SourceUnitLayout::checked(
        &precomputed.dependency_graph,
        &ast_analysis.source_unit_syntax_locations,
        runtime_ast.module.body.len(),
    );
    let single_authored_owner_groups = match &chunk_unassigned_mode {
        UnassignedMode::CoarseAtomicGroups { .. } => builder.single_authored_owner_atomic_groups(
            &precomputed,
            &source_unit_layout,
            chunk_unassigned_mode.partial_atomic_group_completion_owners(),
        )?,
        _ => BTreeMap::new(),
    };
    // Selector outcomes, resolved-member anchors, and analysis assumptions
    // have already been collected from the complete authored ownership.
    let ownership_subset = ownership_subset.map(|subset| subset.modules_for_chunk(chunk_id));
    if let Some(selected) = &ownership_subset {
        builder.retain_modules(selected)?;
        // Recreate only the derived owners required by retained claims.
        builder.apply_destructuring_group_placement(&ast_analysis.destructuring_groups);
        builder.adopt_bindings_of_claimed_anonymous_statements(&declarations);
    }
    let catchall_request = chunk_unassigned_mode
        .catchall_file_target()
        .map(|target| {
            requests
                .iter()
                .find(|request| request.target_path == target)
                .with_context(|| format!("missing catchall request for {chunk_id}::{target}"))
        })
        .transpose()?;
    builder.add_residual_sweep(catchall_request, &declarations, target_dir)?;
    if matches!(chunk_unassigned_mode, UnassignedMode::PerAtomicGroup) {
        builder.synthesize_per_atomic_group(&precomputed, &source_unit_layout, target_dir)?;
    }
    if let UnassignedMode::CoarseAtomicGroups { .. } = &chunk_unassigned_mode {
        builder.route_partial_atomic_groups_to_named_owners(
            &precomputed,
            &source_unit_layout,
            chunk_unassigned_mode.partial_atomic_group_completion_owners(),
            &single_authored_owner_groups,
        )?;
        builder.synthesize_coarse_atomic_groups(
            &precomputed,
            &source_unit_layout,
            &source_public_export_identities_by_statement,
            target_dir,
        )?;
    }
    builder.route_nested_var_statements(&ast_analysis.nested_var_statements);
    if matches!(chunk_unassigned_mode, UnassignedMode::CatchallFile { .. }) {
        builder.route_unclaimed_anonymous_to_catchall(&precomputed, &source_unit_layout)?;
    }
    if let Some(report_out_dir) = report_emission.rejection_dir() {
        write_selector_diagnostics(&builder, report_out_dir, chunk_id)?;
    }
    // Plan construction is finished — consume the builder and use
    // owned state from here on.
    let ChunkPlan {
        mut module_plans,
        bindings_catalogue,
        mut whole_statement_assignment,
        mut generated_residual_plans,
        atomic_group_owner_completions,
        unmatched_spec_claims,
    } = builder.finalize(chunk_id_interned)?;
    let coarse_residual_mode = matches!(
        &chunk_unassigned_mode,
        UnassignedMode::CoarseAtomicGroups { .. }
    );
    let initial_generated_residual_count = generated_residual_plans.len();
    let maximum_coarsening_rounds = initial_generated_residual_count.saturating_sub(1);
    let refine_coarse_residuals = coarse_residual_mode && initial_generated_residual_count > 0;
    let mut graph_for_assignment = Some(precomputed);
    let mut catalogue_for_assignment = Some(bindings_catalogue);
    let mut coarsening_rounds = 0usize;
    // Each pass uses the real assignment/gate result for the current complete
    // layout. Only a whole typed Pass 1 SCC made entirely from plans created by
    // coarse synthesis can be contracted; cuts, rendered reports and the
    // diagnostic-only Pass 2 summary are not inputs here.
    let (sealed_renames, chunk_renames_map, assignment_analysis) = loop {
        let sealed_renames = {
            let mut ledger = RenameLedger::default();
            if let Some(renames) = chunk_renames.get(chunk_id) {
                collect_chunk_renames(renames, chunk_top_level_mark, &mut ledger)?;
            }
            collect_plan_export_rename_intents(&module_plans, chunk_top_level_mark, &mut ledger);
            ledger.seal(&SealValidation::default())
        }?;
        let chunk_renames_map = sealed_renames.chunk_renames_by_name();
        let (planned_modules, entry_destination) =
            project_planned_modules(&module_plans, &source_unit_layout, chunk_top_level_mark)?;
        let assignment_chunk_renames: HashMap<Id, swc_atoms::Atom> = chunk_renames_map
            .iter()
            .map(|(local, exported)| {
                (
                    top_level_id(local, chunk_top_level_mark),
                    exported.as_str().into(),
                )
            })
            .collect();
        let (precomputed_for_assignment, bindings_for_assignment) = if refine_coarse_residuals {
            (
                graph_for_assignment
                    .as_ref()
                    .expect("coarse assignment graph is retained for rebuilds")
                    .clone(),
                catalogue_for_assignment
                    .as_ref()
                    .expect("coarse binding catalogue is retained for rebuilds")
                    .clone(),
            )
        } else {
            (
                graph_for_assignment
                    .take()
                    .expect("non-coarse assignment graph is consumed once"),
                catalogue_for_assignment
                    .take()
                    .expect("non-coarse binding catalogue is consumed once"),
            )
        };
        let assignment_analysis = AssignmentAnalysis::build_with(
            chunk_id.to_string(),
            chunk_id_interned,
            precomputed_for_assignment,
            bindings_for_assignment,
            planned_modules,
            assignment_chunk_renames,
            entry_destination,
        )?;
        if !refine_coarse_residuals {
            break (sealed_renames, chunk_renames_map, assignment_analysis);
        }
        let verdict = assignment_analysis.realizability_verdict();
        let residual_sccs = generated_residual_mutual_sccs(
            &verdict.unrealizable_sccs,
            &generated_residual_plans,
            module_plans.len(),
            &module_plans,
            &source_public_export_identities_by_statement,
            chunk_top_level_mark,
        );
        if residual_sccs.is_empty() {
            drop(graph_for_assignment.take());
            drop(catalogue_for_assignment.take());
            break (sealed_renames, chunk_renames_map, assignment_analysis);
        }

        ensure!(
            coarsening_rounds < maximum_coarsening_rounds,
            "coarse residual SCC contraction exceeded its finite plan-count bound"
        );
        let old_plan_count = module_plans.len();
        let old_generated_count = generated_residual_plans.len();
        contract_generated_residual_sccs(
            &mut module_plans,
            catalogue_for_assignment
                .as_mut()
                .expect("coarse binding catalogue is retained for contractions"),
            &mut whole_statement_assignment,
            &mut generated_residual_plans,
            &residual_sccs,
        )?;
        ensure!(
            module_plans.len() < old_plan_count
                && generated_residual_plans.len() < old_generated_count,
            "coarse residual SCC contraction must reduce generated plan count"
        );
        coarsening_rounds += 1;
    };
    let redundant_purity_hints = analysis.redundant_purity_hints;
    let stable_set_intervals = program_summary
        .stable_set_spreads
        .get(chunk_id)
        .map(|intervals| intervals.values().cloned().collect::<Vec<_>>())
        .unwrap_or_default();
    let assignment = assignment_analysis.assignment();
    let graph = assignment_analysis.inputs().dependency_graph();
    let local_bindings = assignment_analysis
        .inputs()
        .local_bindings(chunk_id_interned)?;
    let placement_projection = analysis::project_placement(PlacementProjectionInput {
        graph,
        assignment,
        local_bindings,
        body: &runtime_ast.module.body,
        layout: &source_unit_layout,
        whole_statements: &whole_statement_assignment,
        module_count: module_plans.len(),
        destructuring_groups: &ast_analysis.destructuring_groups,
        nested_var_statements: &ast_analysis.nested_var_statements,
        stable_set_intervals: &stable_set_intervals,
    });
    // Select only full-detail sinks. Facts, graph construction, emitted order
    // and every chunk's bounded summary use the unchanged full input.
    let effect_conflicts_jsonl = !assessment_only
        && effect_conflicts_jsonl
        && (effect_conflicts_chunks.is_empty() || effect_conflicts_chunks.contains(chunk_id));
    let no_reports = ReportEmission::None;
    let report_context = ChunkReportContext {
        ownership_subset: ownership_subset.as_ref(),
        chunk_id,
        report_emission: if assessment_only {
            &no_reports
        } else {
            report_emission
        },
        graph_reports: if assessment_only {
            GraphReportSelection::None
        } else {
            graph_reports
        },
        // A report-only preview may build a refused, mappable candidate in
        // memory so its final binding assessment matches rejected emission.
        // The application verdict still prevents validation and writes.
        policy: if assessment_only {
            GateRejectionPolicy::LowerUnrunnable
        } else {
            gate_rejection_policy
        },
        effect_conflicts_jsonl,
        full_export_reentry: !assessment_only
            && full_reports.contains(&super::FullReportKind::ExportReentry)
            && (full_report_chunks.is_empty() || full_report_chunks.contains(chunk_id)),
        full_translation_validation: !assessment_only
            && full_reports.contains(&super::FullReportKind::TranslationValidation)
            && (full_report_chunks.is_empty() || full_report_chunks.contains(chunk_id)),
    };
    let preflight = gate::CandidatePreflight::prepare(
        &assignment_analysis,
        &placement_projection,
        options.candidate_assessment_context.clone(),
    );
    let emission_preparation = EmissionPlacement::from_preflight(&preflight, &placement_projection)
        .and_then(|placement| {
            prepare_candidate_emission_plan(LowerChunkInputs {
                context: LowerChunkContext {
                    artifact,
                    artifact_indexes,
                    chunk_id,
                    chunk_id_interned,
                    source_path: &source_path,
                    entry_file: &target_file,
                    header_lines: &header_lines,
                    vendor_import_oracle,
                },
                ast: LowerChunkAst {
                    runtime_ast,
                    declarations: &declarations,
                    declaration_by_name: &declaration_by_name,
                    chunk_top_level_mark,
                },
                plan: LowerChunkPlan {
                    module_plans: &module_plans,
                    placement: &placement,
                },
                spec_facts: LowerChunkSpecFacts {
                    runtime_import_facts: &runtime_import_facts,
                    sealed_renames: &sealed_renames,
                    chunk_renames: &chunk_renames_map,
                    pre_existing_entry_exports: &pre_existing_entry_exports,
                    pre_existing_public_export_names: &pre_existing_public_export_names,
                },
            })
            .map(|prepared| (placement, prepared))
        });
    let initialization_context = crate::module_initialization::ModuleInitializationContext {
        artifact,
        indexes: artifact_indexes,
        assignment_analysis: &assignment_analysis,
        consumer: chunk_id_interned,
        materialized_chunks,
        vendor_unchanged,
        prepared: emission_preparation
            .as_ref()
            .ok()
            .map(|(_, prepared)| prepared),
        source_initialized_cells: program_summary
            .source_initialized_module_cells
            .get(chunk_id),
        runtime_import_facts: &runtime_import_facts,
    };
    let module_initialization = crate::module_initialization::assess_module_initialization(
        initialization_context,
        &analysis.facts,
    );
    let provider_targets = crate::module_initialization::assess_conditional_provider_targets(
        &initialization_context,
        &analysis.facts,
    );
    let (evaluation, report_out_dir) = evaluate_chunk_candidate(
        report_context,
        preflight,
        &analysis.facts,
        module_initialization,
        provider_targets,
    )?;
    let preflight_status = evaluation.candidate_assessment().status();
    let candidate_assessment = evaluation.relocation_assessment_report();
    if assessment_only
        && preflight_status != gate::CandidateRelocationStatus::AcceptedWithinCheckedScope
        && emission_preparation.is_err()
    {
        // A candidate with no prepared placement/body cannot contribute
        // to a final artifact. Preserve its local refusal and make the
        // unavailable binding check explicit at the application boundary.
        return Ok(FinishedLogicalChunk::Assessed {
            chunk_id: chunk_id.to_string(),
            assessment: Box::new(candidate_assessment),
        });
    }
    let gate_outcome = emit_reports_and_resolve_gate(
        report_context,
        program_graph,
        evaluation,
        report_out_dir,
        &export_reentry_reports[&chunk_id_interned],
    )?;

    let (linker_order, gate_rejection) = match &gate_outcome {
        GateOutcome::Prepared { linker_order } => (linker_order.clone(), None),
        GateOutcome::Rejected {
            assignment,
            rejection,
        } => (
            assignment.report().linker_order.clone(),
            Some(rejection.clone()),
        ),
    };
    let (placement, mut prepared_emission) = emission_preparation?;
    let parsed_source_imports = artifact
        .find_chunk(chunk_id_interned)
        .and_then(|chunk| chunk.analysis.runtime_facts.parsed_source_imports.as_ref());
    let source_correspondence = build_source_correspondence(
        chunk_id_interned,
        chunk_id,
        source_path.clone(),
        runtime_ast,
        parsed_source_imports,
        &prepared_emission,
        &source_unit_layout,
        &assignment_analysis,
        program_summary,
    );
    if let GateOutcome::Rejected { rejection, .. } = &gate_outcome {
        // The banner is file metadata, so add it after the verdict without
        // rebuilding any prepared AST bodies.
        prepared_emission.prepend_entry_header_lines(&gate_rejected_banner_lines(rejection));
    }
    let lowered = prepared_emission.emit();
    let LoweredChunk {
        files,
        file_records,
        applied,
        vendor_reference_rewrites,
    } = lowered;

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
        .unwrap_or_else(|| target_file.clone());
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
        atomic_group_owner_completions: atomic_group_owner_completions
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
            .collect(),
        requested_logical_modules: requests
            .iter()
            .map(|request| RequestedLogicalModule {
                target_path: spec::ModulePath::parse(&request.target_path, "")
                    .expect("request target_path is a canonical module path"),
                residual: request.residual,
            })
            .collect(),
        imported_callables,
        redundant_purity_hints,
    };
    let chunk = Box::new(MaterializedLogicalChunk {
        chunk_id: chunk_id_interned,
        unmatched_spec_claims,
        target_file,
        source_path,
        files,
        file_records,
        applied,
        directory_dependency_facts,
        validation,
        report,
        source_correspondence,
        vendor_reference_rewrites,
        gate_rejection,
    });
    let assessment = Box::new(candidate_assessment);
    Ok(if assessment_only {
        FinishedLogicalChunk::AssessedAndLowered { chunk, assessment }
    } else {
        FinishedLogicalChunk::Lowered { chunk, assessment }
    })
}

/// `// …` lines announcing a gate-rejected chunk at the top of its entry file.

fn build_source_correspondence(
    source_chunk: ChunkId,
    chunk_name: &str,
    source_file: String,
    source_ast: &ParsedJsModule,
    parsed: Option<&ParsedSourceImports>,
    prepared: &crate::lower::PreparedCandidateEmissionPlan<'_>,
    source_units: &SourceUnitLayout,
    assignment: &AssignmentAnalysis,
    summary: &super::program_summary::ProgramSummary,
) -> CandidateSourceCorrespondence {
    let Some(parsed) = parsed else {
        return CandidateSourceCorrespondence::unavailable(source_chunk, source_file, None);
    };
    let source_file_identity = parsed.source_file.clone();
    let parsed_identity_is_complete = !source_file_identity.is_empty()
        && source_ast.source_file_name() == source_file_identity
        && summary
            .global_uses
            .get(chunk_name)
            .is_some_and(|uses| uses.source_file == source_file_identity);
    let module_cells = summary.module_cells.get(chunk_name);
    let source_initialized = summary.source_initialized_module_cells.get(chunk_name);
    let empty_cells = BTreeMap::new();
    let prepared_requests = prepared.prepared_request_routes(
        source_chunk,
        &source_ast.module.body,
        module_cells.unwrap_or(&empty_cells),
    );
    let entry_destination = assignment.assignment().entry_destination().module_id();

    let mut source_unit_routes = BTreeMap::new();
    let mut source_units_complete = source_units.issues().is_empty();
    let mut complete = source_units_complete && parsed_identity_is_complete;
    for location in source_units.by_id().values() {
        let Some(source_item) = source_ast.module.body.get(location.statement.0) else {
            complete = false;
            continue;
        };
        let declarator_span = source_declarator_span(source_item, location.declarator_offset);
        let destination = assignment.assignment().of(location.source_unit);
        let positions = prepared.candidate_source_unit_positions(
            source_chunk,
            source_item,
            declarator_span,
            destination,
            entry_destination,
        );
        let status = match positions.len() {
            0 => SourceCorrespondenceStatus::MissingCandidateRoute,
            1 => SourceCorrespondenceStatus::Complete,
            _ => SourceCorrespondenceStatus::Ambiguous,
        };
        complete &= status == SourceCorrespondenceStatus::Complete;
        source_units_complete &= status == SourceCorrespondenceStatus::Complete;
        let outputs = positions
            .iter()
            .map(|(output, _, _, _, _)| output.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let positions = positions
            .into_iter()
            .map(
                |(output, item_index, item_span, declarator_span, declarator_offset)| {
                    artifact::reports::CandidateSourcePosition {
                        output,
                        item_index,
                        item_span,
                        declarator_span,
                        declarator_offset,
                    }
                },
            )
            .collect();
        source_unit_routes.insert(
            location.ordinal,
            CandidateSourceUnitRoute {
                source_unit: location.ordinal,
                source_statement: location.statement,
                source_span: source_item.span(),
                source_declarator_span: declarator_span,
                outputs,
                positions,
                status,
            },
        );
    }

    let source_requests = parsed
        .requests
        .iter()
        .cloned()
        .map(|source| {
            let matches = source_request_matches(&source, parsed, &prepared_requests);
            let routes = matches
                .iter()
                .map(|(request, source_bindings)| CandidateSourceRequestRoute {
                    output: request.output.clone(),
                    item_index: request.item_index,
                    item_span: request.item_span,
                    source_statement: request.source_statement,
                    emitted_request: request.emitted_request.clone(),
                    mode: request.mode,
                    targets: if source_bindings.is_empty() {
                        request.targets.clone()
                    } else {
                        source_bindings
                            .iter()
                            .map(|binding| binding.target.clone())
                            .collect()
                    },
                    bindings: source_bindings
                        .iter()
                        .map(|binding| (*binding).clone())
                        .collect(),
                    status: request.status,
                })
                .collect::<Vec<_>>();
            let status = source_request_status(&source, &routes);
            complete &= status == SourceCorrespondenceStatus::Complete;
            CandidateSourceStaticRequestRoutes {
                source,
                routes,
                status,
            }
        })
        .collect::<Vec<_>>();

    let imports = parsed
        .bindings
        .iter()
        .cloned()
        .map(|source| {
            let candidates = prepared_requests
                .iter()
                .filter(|request| {
                    request.bindings.iter().any(|binding| {
                        binding.source_binding.as_ref() == Some(&source.local)
                            && binding.imported_name.as_deref() == Some(source.imported_name.as_str())
                            && binding.kind == source.kind
                    })
                })
                .flat_map(|request| {
                    request
                        .bindings
                        .iter()
                        .filter(|binding| {
                            binding.source_binding.as_ref() == Some(&source.local)
                                && binding.imported_name.as_deref()
                                    == Some(source.imported_name.as_str())
                                && binding.kind == source.kind
                        })
                        .map(move |binding| (request, binding))
                })
                .collect::<Vec<_>>();
            let routes = candidates
                .iter()
                .filter_map(|(request, binding)| {
                    let candidate_local = binding.candidate_local.as_ref()?;
                    Some(artifact::reports::CandidateImportRoute {
                    output: request.output.clone(),
                    item_span: request.item_span,
                    candidate_local: candidate_local.clone(),
                    specifier_index: binding.specifier_index,
                    specifier_span: binding.specifier_span,
                    emitted_request: request.emitted_request.clone(),
                    imported_name: binding
                        .imported_name
                        .as_ref()
                        .expect("source import route has an imported name")
                        .clone(),
                    kind: binding.kind,
                    target: binding.target.clone(),
                    source_cell: binding.source_cell,
                    })
                })
                .collect::<Vec<_>>();
            let uses = source
                .use_spans
                .iter()
                .copied()
                .map(|source_span| {
                    let routes = prepared.candidate_identifier_routes(source_chunk, source_span);
                    let status = if routes.is_empty() {
                        SourceCorrespondenceStatus::MissingCandidateRoute
                    } else {
                        SourceCorrespondenceStatus::Complete
                    };
                    CandidateSourceUseRoute {
                        source_span,
                        routes,
                        status,
                    }
                })
                .collect::<Vec<_>>();
            let targets_supported = candidates.iter().all(|(_, binding)| {
                !matches!(
                    binding.target,
                    artifact::reports::CandidateImportTarget::Unsupported
                        | artifact::reports::CandidateImportTarget::OriginalSourceUnknownExports { .. }
                )
            });
            let status = if source.type_only {
                SourceCorrespondenceStatus::UnsupportedRewrite
            } else if candidates.is_empty()
                || routes.is_empty()
                || uses.iter().any(|use_route| use_route.routes.is_empty())
            {
                SourceCorrespondenceStatus::MissingCandidateRoute
            } else if !targets_supported {
                SourceCorrespondenceStatus::UnsupportedRewrite
            } else {
                SourceCorrespondenceStatus::Complete
            };
            complete &= status == SourceCorrespondenceStatus::Complete;
            CandidateSourceImportRoutes {
                source,
                routes,
                uses,
                status,
            }
        })
        .collect::<Vec<_>>();

    let reexports = parsed
        .reexports
        .iter()
        .cloned()
        .map(|source| {
            let source_request = parsed.requests.iter().find(|request| {
                request.statement == source.statement
                    && request.kind != SourceStaticRequestKind::Import
                    && request.request == source.request
            });
            let routes = source_request
                .and_then(|source_request| {
                    source_requests.iter().find(|routes| {
                        routes.source.statement == source_request.statement
                            && routes.source.kind == source_request.kind
                            && routes.source.request == source_request.request
                    })
                })
                .map(|routes| routes.routes.clone())
                .unwrap_or_default();
            let status = if source.type_only {
                SourceCorrespondenceStatus::UnsupportedRewrite
            } else if routes.is_empty() {
                SourceCorrespondenceStatus::MissingCandidateRoute
            } else {
                routes
                    .iter()
                    .map(|route| route.status)
                    .find(|status| *status != SourceCorrespondenceStatus::Complete)
                    .unwrap_or(SourceCorrespondenceStatus::Complete)
            };
            complete &= status == SourceCorrespondenceStatus::Complete;
            CandidateSourceReExportRoutes {
                source,
                routes,
                status,
            }
        })
        .collect::<Vec<_>>();

    let exports = parsed
        .exports
        .iter()
        .cloned()
        .map(|source| {
            let routes = source
                .local
                .as_ref()
                .and_then(|local| {
                    find_source_binding_owner(local, source_units, &source_ast.module.body)
                        .and_then(|location| {
                            prepared.source_binding_route(
                                source_chunk,
                                local,
                                assignment.assignment().of(location.source_unit),
                                entry_destination,
                            )
                        })
                })
                .into_iter()
                .filter(|route| route.exported_name.as_deref() == Some(source.public_name.as_str()))
                .collect::<Vec<_>>();
            let status = if source.type_only {
                SourceCorrespondenceStatus::UnsupportedRewrite
            } else {
                match routes.len() {
                    0 => SourceCorrespondenceStatus::MissingCandidateRoute,
                    1 => SourceCorrespondenceStatus::Complete,
                    _ => SourceCorrespondenceStatus::Ambiguous,
                }
            };
            complete &= status == SourceCorrespondenceStatus::Complete;
            CandidateSourceExportRoutes {
                source,
                routes,
                status,
            }
        })
        .collect::<Vec<_>>();

    let source_module_cells = module_cells.cloned();
    let source_initialized_module_cells = source_initialized.cloned();
    let module_cell_declarations = module_cells
        .into_iter()
        .flat_map(|cells| cells.iter())
        .filter(|(_, cell)| cell.chunk == source_chunk)
        .map(|(binding, source_cell)| {
            let invariant = summary.module_cell_invariants.get(source_cell);
            let owner = invariant
                .map(|invariant| invariant.declaration.owner)
                .or_else(|| {
                    find_source_binding_owner(binding, source_units, &source_ast.module.body)
                        .map(|location| location.ordinal)
                });
            let location = owner.and_then(|ordinal| source_units.by_ordinal(ordinal));
            let route = location.and_then(|location| {
                prepared.source_binding_route(
                    source_chunk,
                    binding,
                    assignment.assignment().of(location.source_unit),
                    entry_destination,
                )
            });
            let routes = route.into_iter().collect::<Vec<_>>();
            let declaration_span = invariant
                .map(|invariant| invariant.declaration.binding_span)
                .or_else(|| {
                    location.and_then(|location| {
                        source_ast
                            .module
                            .body
                            .get(location.statement.0)
                            .map(|item| item.span())
                    })
                });
            let status = match (owner, routes.as_slice()) {
                (Some(_), [_]) => SourceCorrespondenceStatus::Complete,
                (None, _) => SourceCorrespondenceStatus::MissingCandidateRoute,
                (_, []) => SourceCorrespondenceStatus::MissingCandidateRoute,
                (_, _) => SourceCorrespondenceStatus::Ambiguous,
            };
            complete &= status == SourceCorrespondenceStatus::Complete;
            artifact::reports::CandidateModuleCellDeclaration {
                source_cell: *source_cell,
                source_binding: Some(binding.clone()),
                source_owner: owner,
                declaration_span,
                routes,
                status,
            }
        })
        .collect::<Vec<_>>();

    if module_cells.is_none() || source_initialized.is_none() {
        complete = false;
    }
    let status = if complete {
        SourceCorrespondenceStatus::Complete
    } else if source_unit_routes.is_empty() && parsed.requests.is_empty() && imports.is_empty() {
        SourceCorrespondenceStatus::MissingSourceFacts
    } else {
        SourceCorrespondenceStatus::MissingCandidateRoute
    };
    let mut correspondence = CandidateSourceCorrespondence {
        source_chunk,
        source_file: source_file_identity,
        source_units: source_unit_routes,
        source_unit_status: if source_units_complete {
            SourceCorrespondenceStatus::Complete
        } else {
            SourceCorrespondenceStatus::MissingCandidateRoute
        },
        source_requests,
        imports,
        reexports,
        exports,
        prepared_requests,
        module_cell_declarations,
        source_module_cells,
        source_initialized_module_cells,
        rewrite_receipts: Vec::new(),
        status,
    };
    vendor_replacements::record_materialized_vendor_replacements(
        &mut correspondence,
        parsed,
        prepared,
        module_cells.unwrap_or(&empty_cells),
    );
    correspondence
}

fn source_request_matches<'a>(
    source: &ParsedSourceRequest,
    parsed: &ParsedSourceImports,
    prepared: &'a [artifact::reports::CandidatePreparedRequestRoute],
) -> Vec<(
    &'a artifact::reports::CandidatePreparedRequestRoute,
    Vec<&'a artifact::reports::CandidatePreparedRequestBinding>,
)> {
    let source_bindings = parsed
        .bindings
        .iter()
        .filter(|binding| binding.statement == source.statement)
        .map(|binding| &binding.local)
        .collect::<Vec<_>>();
    prepared
        .iter()
        .filter_map(|candidate| {
            let statement_match = candidate.source_statement == Some(source.statement);
            let bindings = if source.kind == SourceStaticRequestKind::Import {
                candidate
                    .bindings
                    .iter()
                    .filter(|binding| {
                        source_bindings.iter().any(|source_binding| {
                            Some(*source_binding) == binding.source_binding.as_ref()
                        })
                    })
                    .collect::<Vec<_>>()
            } else {
                candidate.bindings.iter().collect::<Vec<_>>()
            };
            let matched = match source.kind {
                SourceStaticRequestKind::Import => statement_match || !bindings.is_empty(),
                SourceStaticRequestKind::NamedReExport | SourceStaticRequestKind::ExportAll => {
                    statement_match
                }
            };
            if matched {
                Some((candidate, bindings))
            } else {
                None
            }
        })
        .collect()
}

fn source_request_status(
    source: &ParsedSourceRequest,
    routes: &[CandidateSourceRequestRoute],
) -> SourceCorrespondenceStatus {
    if source.type_only || source.mode != artifact::reports::StaticModuleRequestMode::OrdinaryEsm {
        return SourceCorrespondenceStatus::UnsupportedRewrite;
    }
    if routes.is_empty() {
        return SourceCorrespondenceStatus::MissingCandidateRoute;
    }
    routes
        .iter()
        .map(|route| route.status)
        .find(|status| *status != SourceCorrespondenceStatus::Complete)
        .unwrap_or(SourceCorrespondenceStatus::Complete)
}

fn source_declarator_span(item: &ModuleItem, offset: Option<usize>) -> Option<Span> {
    let offset = offset?;
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => declaration
            .decls
            .get(offset)
            .map(|declarator| declarator.span),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            let Decl::Var(declaration) = &export.decl else {
                return None;
            };
            declaration
                .decls
                .get(offset)
                .map(|declarator| declarator.span)
        }
        _ => None,
    }
}

fn find_source_binding_owner<'a>(
    binding: &swc_ecma_ast::Id,
    source_units: &'a SourceUnitLayout,
    body: &[ModuleItem],
) -> Option<&'a analysis::SourceUnitLocation> {
    source_units.by_id().values().find(|location| {
        let Some(item) = body.get(location.statement.0) else {
            return false;
        };
        source_unit_declares(item, location.declarator_offset, binding)
    })
}

fn source_unit_declares(
    item: &ModuleItem,
    offset: Option<usize>,
    binding: &swc_ecma_ast::Id,
) -> bool {
    struct BindingFinder<'a> {
        target: &'a swc_ecma_ast::Id,
        found: bool,
    }
    impl swc_ecma_visit::Visit for BindingFinder<'_> {
        fn visit_binding_ident(&mut self, ident: &swc_ecma_ast::BindingIdent) {
            if ident.to_id() == *self.target {
                self.found = true;
            }
        }
    }
    let mut finder = BindingFinder {
        target: binding,
        found: false,
    };
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => {
            if let Some(offset) = offset {
                if let Some(declarator) = declaration.decls.get(offset) {
                    swc_ecma_visit::VisitWith::visit_with(&declarator.name, &mut finder);
                }
            } else {
                swc_ecma_visit::VisitWith::visit_with(declaration, &mut finder);
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            if let Decl::Var(declaration) = &export.decl {
                if let Some(offset) = offset {
                    if let Some(declarator) = declaration.decls.get(offset) {
                        swc_ecma_visit::VisitWith::visit_with(&declarator.name, &mut finder);
                    }
                } else {
                    swc_ecma_visit::VisitWith::visit_with(&export.decl, &mut finder);
                }
            } else {
                swc_ecma_visit::VisitWith::visit_with(&export.decl, &mut finder);
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
            finder.found = import.specifiers.iter().any(|specifier| match specifier {
                ImportSpecifier::Named(named) => named.local.to_id() == *binding,
                ImportSpecifier::Default(default) => default.local.to_id() == *binding,
                ImportSpecifier::Namespace(namespace) => namespace.local.to_id() == *binding,
            });
        }
        _ => {
            swc_ecma_visit::VisitWith::visit_with(item, &mut finder);
        }
    }
    finder.found
}

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
fn project_planned_modules(
    module_plans: &[ModulePlan],
    source_unit_layout: &SourceUnitLayout,
    chunk_top_level_mark: swc_common::Mark,
) -> Result<(Vec<PlannedModule>, EntryDestination)> {
    let mut planned_modules = Vec::with_capacity(module_plans.len());
    for plan in module_plans {
        let mut anonymous_source_unit_ordinals = Vec::new();
        for statement in &plan.anonymous_statements {
            let locations = source_unit_layout.for_statement(*statement);
            if locations.is_empty() {
                bail!(
                    "source statement at body index {statement} has no checked source-unit layout"
                );
            }
            // Preserve the existing assignment-assembly contract: one
            // anonymous request points at the raw statement's first ordinal.
            // The shared projection checks every source unit for that body
            // item before emission and reports any split as a blocker.
            anonymous_source_unit_ordinals.push(locations[0].ordinal);
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

/// Return only complete Pass 1 SCCs whose module IDs have explicit provenance
/// as coarse-generated residual plans. This consumes the gate's typed in-memory
/// diagnosis; the rendered cycle report and its cut are intentionally ignored.
fn generated_residual_mutual_sccs(
    diagnoses: &[gate::SccDiagnosis],
    generated_residual_plans: &BTreeSet<usize>,
    module_count: usize,
    module_plans: &[ModulePlan],
    source_exports_by_statement: &HashMap<StatementId, Vec<SourceExportIdentity>>,
    chunk_top_level_mark: Mark,
) -> Vec<Vec<usize>> {
    diagnoses
        .iter()
        .filter(|diagnosis| diagnosis.rejection == SccRejection::MutualConstrainingCycle)
        .filter_map(|diagnosis| {
            let modules = &diagnosis.core.modules;
            if modules.len() < 2
                || modules.iter().any(|module| {
                    module.0 >= module_count || !generated_residual_plans.contains(&module.0)
                })
            {
                return None;
            }
            let mut merged_surface = PublicExportSurface::default();
            for module in modules {
                let plan = &module_plans[module.0];
                let mut plan_surface = PublicExportSurface::default();
                for (local, public) in &plan.bindings {
                    // `top_level_id` reconstructs the chunk-level Id used by
                    // the resolved AST and dependency graph. Source export
                    // aliases therefore compare against the same identity as
                    // generated residual exports.
                    plan_surface
                        .add_binding(public.clone(), top_level_id(local, chunk_top_level_mark));
                }
                for statement in &plan.anonymous_statements {
                    if let Some(exports) = source_exports_by_statement.get(statement) {
                        plan_surface.add_source_identities(exports);
                    }
                }
                if !merged_surface.is_compatible_with(&plan_surface) {
                    // Keep this SCC separate; the original gate diagnosis
                    // remains blocking and is handled by the ordinary path.
                    return None;
                }
                merged_surface.merge(&plan_surface);
            }
            Some(modules.iter().map(|module| module.0).collect())
        })
        .collect()
}

/// Contract disjoint, complete SCCs of generated residual plans and reproject
/// every positional owner. Authored plans, source-unit identities, and module
/// contents survive; the survivor is the earliest plan in original plan order.
fn contract_generated_residual_sccs(
    module_plans: &mut Vec<ModulePlan>,
    bindings_catalogue: &mut BindingCatalogue,
    whole_statement_assignment: &mut BTreeMap<source_identity::StatementId, usize>,
    generated_residual_plans: &mut BTreeSet<usize>,
    sccs: &[Vec<usize>],
) -> Result<()> {
    let old_plan_count = module_plans.len();
    ensure!(
        generated_residual_plans
            .iter()
            .all(|module| *module < old_plan_count),
        "generated residual provenance contains an invalid module ID"
    );
    let mut absorbed_by = vec![None; old_plan_count];
    let mut seen_members = BTreeSet::new();

    for scc in sccs {
        let mut members = scc.clone();
        members.sort_unstable();
        members.dedup();
        ensure!(members.len() >= 2, "cannot contract a singleton SCC");
        let survivor = members[0];
        ensure!(
            survivor < old_plan_count,
            "SCC contains an invalid module ID"
        );
        ensure!(
            members.iter().all(|member| {
                *member < old_plan_count && generated_residual_plans.contains(member)
            }),
            "SCC contraction received a module without generated-residual provenance"
        );
        for member in &members {
            ensure!(
                seen_members.insert(*member),
                "SCC module appears more than once"
            );
        }

        let mut merged = module_plans[survivor].clone();
        for member in members.iter().copied().skip(1) {
            merge_generated_residual_plan(&mut merged, &module_plans[member])?;
            absorbed_by[member] = Some(survivor);
        }
        module_plans[survivor] = merged;
    }

    let mut old_to_new = vec![ModuleId(usize::MAX); old_plan_count + 1];
    let mut compacted_plans = Vec::with_capacity(old_plan_count - seen_members.len() + sccs.len());
    for (old_id, plan) in std::mem::take(module_plans).into_iter().enumerate() {
        if let Some(survivor) = absorbed_by[old_id] {
            ensure!(
                survivor < old_id,
                "SCC survivor must preserve original order"
            );
            old_to_new[old_id] = old_to_new[survivor];
        } else {
            old_to_new[old_id] = ModuleId(compacted_plans.len());
            compacted_plans.push(plan);
        }
    }
    old_to_new[old_plan_count] = ModuleId(compacted_plans.len());

    let chunk_id = bindings_catalogue.chunk_id();
    let mut remapped_bindings = bindings_catalogue.local_view(chunk_id)?.as_map().clone();
    for binding in remapped_bindings.values_mut() {
        match binding {
            BindingKind::Owned { module } => {
                *module = remap_planned_or_entry_module(*module, &old_to_new, old_plan_count)?;
            }
            BindingKind::Imported { re_exporter, .. } => {
                *re_exporter =
                    remap_planned_or_entry_module(*re_exporter, &old_to_new, old_plan_count)?;
            }
        }
    }
    *bindings_catalogue = BindingCatalogue::new(chunk_id, remapped_bindings);

    for module in whole_statement_assignment.values_mut() {
        ensure!(
            *module < old_plan_count,
            "anonymous statement has invalid module ID"
        );
        *module = old_to_new[*module].0;
    }
    *generated_residual_plans = generated_residual_plans
        .iter()
        .map(|module| old_to_new[*module].0)
        .collect();
    *module_plans = compacted_plans;
    Ok(())
}

fn remap_planned_or_entry_module(
    module: ModuleId,
    old_to_new: &[ModuleId],
    old_plan_count: usize,
) -> Result<ModuleId> {
    ensure!(
        module.0 <= old_plan_count,
        "binding catalogue has invalid module ID"
    );
    Ok(old_to_new[module.0])
}

fn merge_generated_residual_plan(target: &mut ModulePlan, source: &ModulePlan) -> Result<()> {
    ensure!(
        target.role == ModuleRole::Unassigned && source.role == ModuleRole::Unassigned,
        "generated residual plans must retain their unassigned role"
    );
    for (name, export) in &source.bindings {
        if let Some(existing) = target.bindings.get(name) {
            ensure!(
                existing == export,
                "generated residual plans disagree on export for {name:?}"
            );
        } else {
            target.bindings.insert(name.clone(), export.clone());
        }
    }
    target
        .spec_named_bindings
        .extend(source.spec_named_bindings.iter().cloned());
    target
        .anonymous_statements
        .extend(source.anonymous_statements.iter().copied());
    target.anonymous_statements.sort_unstable();
    target.anonymous_statements.dedup();
    merge_equal_keyed_values(
        &mut target.anonymous_statement_comments,
        &source.anonymous_statement_comments,
    )?;
    merge_equal_keyed_values(&mut target.binding_comments, &source.binding_comments)?;
    merge_equal_keyed_values(
        &mut target.binding_claim_origins,
        &source.binding_claim_origins,
    )?;
    match (&target.comment, &source.comment) {
        (Some(target), Some(source)) => ensure!(target == source, "residual comments disagree"),
        (None, Some(source)) => target.comment = Some(source.clone()),
        _ => {}
    }
    Ok(())
}

fn merge_equal_keyed_values<K, V>(
    target: &mut BTreeMap<K, V>,
    source: &BTreeMap<K, V>,
) -> Result<()>
where
    K: Ord + Clone + std::fmt::Debug,
    V: Clone + PartialEq,
{
    for (key, value) in source {
        if let Some(existing) = target.get(key) {
            ensure!(
                existing == value,
                "merged residual metadata conflicts at {key:?}"
            );
        } else {
            target.insert(key.clone(), value.clone());
        }
    }
    Ok(())
}

/// The gate's judgement of one chunk's assignment, with the evidence reports
/// already written.
enum GateOutcome<'a> {
    /// Syntax preparation is permitted, but whole-output assessment is still
    /// pending and no accepted-plan capability exists at this stage.
    Prepared { linker_order: Vec<spec::ModulePath> },
    /// Only under [`GateRejectionPolicy::LowerUnrunnable`]; `Refuse` fails
    /// with the rendered diagnostic instead.
    Rejected {
        assignment: RejectedAssignment<'a>,
        rejection: ChunkGateRejection,
    },
}

/// Write per-chunk validation reports (dependency graph, atomic-group
/// conflicts, cycles) and, when the spec is unrealizable, either bail with a
/// human-readable summary or hand the rejected assignment back for the
/// reading-only lowering of `debundle run --emit-on-rejection`.
///
/// Under [`ReportEmission::OnRejection`] (the `debundle run
/// --dry-run` mode) the accept path writes nothing, but a rejection
/// still writes rejection evidence (and explicitly selected full graphs) so
/// the documented `debundle gate list/describe` follow-up works.
/// The chunk's `selector_diagnostics.json`, where `spec validate` collects
/// per-chunk reports; nothing to write while no outcome was recorded.
fn write_selector_diagnostics(
    builder: &ChunkPlanBuilder,
    report_out_dir: &std::path::Path,
    chunk_id: &str,
) -> Result<()> {
    if let Some(report) = builder.selector_outcome_report() {
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            SELECTOR_DIAGNOSTICS_REPORT,
            &report,
        )?;
    }
    Ok(())
}

/// The selected chunk's output policy, independent of its analysis facts.
#[derive(Clone, Copy)]
struct ChunkReportContext<'a> {
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    chunk_id: &'a str,
    report_emission: &'a ReportEmission,
    graph_reports: GraphReportSelection,
    policy: GateRejectionPolicy,
    effect_conflicts_jsonl: bool,
    full_translation_validation: bool,
    full_export_reentry: bool,
}

/// Exact counts over the canonical in-memory graph, with no node/edge table.
#[derive(serde::Serialize)]
struct GraphSummary<'a> {
    chunk_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    source_units: usize,
    edges: usize,
    edge_kinds: std::collections::BTreeMap<analysis_model::DepKind, usize>,
    atomic_groups: usize,
    atomic_group_conflicts: usize,
    cycles: usize,
    candidate_preparation_permitted: bool,
    translation_validation_accepts: bool,
    full_graphs: bool,
}

/// Run the final assessment once, independently of report selection. Optional
/// rejection-only streams are published only after that same assessment rejects.
fn evaluate_chunk_candidate<'a, 'b>(
    context: ChunkReportContext<'b>,
    preflight: gate::CandidatePreflight<'a>,
    facts: &[analysis::facts::StatementFacts],
    module_initialization: gate::CandidateModuleInitializationEvidence,
    provider_targets: gate::CandidateProviderTargetEvidence,
) -> Result<(gate::CandidateEvaluation<'a>, Option<&'b std::path::Path>)> {
    let ChunkReportContext {
        chunk_id,
        report_emission,
        full_translation_validation,
        effect_conflicts_jsonl,
        graph_reports,
        full_export_reentry,
        ..
    } = context;
    let full_dir = report_emission.full_dir();
    let mut full_tv = full_translation_validation
        .then(|| {
            JsonlReport::create(full_dir.map(|dir| {
                chunk_report_path(
                    dir,
                    chunk_id,
                    output_layout::TRANSLATION_VALIDATION_FULL_REPORT,
                )
            }))
        })
        .transpose()?;
    let mut jsonl =
        effect_conflicts_jsonl
            .then(|| {
                JsonlReport::create(full_dir.map(|dir| {
                    chunk_report_path(dir, chunk_id, output_layout::EFFECT_CONFLICTS_JSONL)
                }))
            })
            .transpose()?;
    let evaluation = {
        let mut write_tv = |row: &gate::TranslationValidationEvidenceReport| -> Result<()> {
            full_tv
                .as_mut()
                .expect("active full-report sink")
                .write(row)
        };
        let mut write_effect = |row: &gate::EffectConflictDiagnosticRow<'_>| -> Result<()> {
            jsonl
                .as_mut()
                .expect("active effect-conflict sink")
                .write(row)
        };
        preflight.evaluate(
            facts,
            module_initialization,
            provider_targets,
            full_translation_validation.then_some(&mut write_tv),
            effect_conflicts_jsonl.then_some(&mut write_effect),
        )?
    };
    let report_out_dir = if matches!(
        evaluation.preparation_readiness(),
        CandidatePreparationReadiness::Refused(_)
    ) {
        report_emission.rejection_dir()
    } else {
        full_dir
    };
    ensure!(
        !effect_conflicts_jsonl || report_out_dir.is_some(),
        "--effect-conflicts-jsonl requested for chunk {chunk_id}, but the current report policy suppresses output; run without --dry-run",
    );
    ensure!(
        !graph_reports.enabled() || report_out_dir.is_some(),
        "--graphs requested for chunk {chunk_id}, but the current report policy suppresses output; run without --dry-run",
    );
    ensure!(
        !(full_translation_validation || full_export_reentry) || report_out_dir.is_some(),
        "--full-report requested for chunk {chunk_id}, but the current report policy suppresses output; run without --dry-run",
    );
    if let Some(writer) = full_tv {
        writer.finish(report_out_dir.map(|dir| {
            chunk_report_path(
                dir,
                chunk_id,
                output_layout::TRANSLATION_VALIDATION_FULL_REPORT,
            )
        }))?;
    }
    if let Some(writer) = jsonl {
        writer
            .finish(report_out_dir.map(|dir| {
                chunk_report_path(dir, chunk_id, output_layout::EFFECT_CONFLICTS_JSONL)
            }))?;
    }
    Ok((evaluation, report_out_dir))
}

fn graph_export_guidance(
    dir: &std::path::Path,
    chunk_id: &str,
    selection: GraphReportSelection,
    ownership_subset: bool,
) -> String {
    if ownership_subset {
        return " Bounded reports describe this ownership subset; full graph export for subsets is unsupported.".to_string();
    }
    if selection.enabled() {
        format!(
            " Dependency graph at {}.",
            chunk_report_path(dir, chunk_id, DEPENDENCY_GRAPH_REPORT).display()
        )
    } else {
        " Full graph queries require rerunning with --graphs or building the dependency_graphs Bazel output group.".to_string()
    }
}

fn emit_reports_and_resolve_gate<'a>(
    context: ChunkReportContext<'_>,
    program_graph: &ProgramGraphOutput<'_>,
    evaluation: gate::CandidateEvaluation<'a>,
    report_out_dir: Option<&std::path::Path>,
    export_reentry_report: &ExportReentryReport,
) -> Result<GateOutcome<'a>> {
    let ChunkReportContext {
        chunk_id,
        ownership_subset,
        graph_reports,
        policy,
        full_export_reentry,
        ..
    } = context;
    let assignment_analysis = evaluation.assignment_analysis();
    let assignment_report = evaluation.report();
    let preparation_readiness = evaluation.preparation_readiness();
    let rejected = matches!(
        preparation_readiness,
        CandidatePreparationReadiness::Refused(_)
    );
    let translation_validation = evaluation.translation_validation();
    // The final verdict is complete before optional report serialization.
    if let Some(report_out_dir) = report_out_dir {
        let graph = assignment_analysis.inputs().dependency_graph();
        let mut edge_kinds = std::collections::BTreeMap::new();
        for edge in graph.iter_edges() {
            *edge_kinds.entry(edge.reason.kind()).or_insert(0usize) += 1;
        }
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            output_layout::GRAPH_SUMMARY_REPORT,
            &GraphSummary {
                chunk_id,
                ownership_subset,
                source_units: graph.num_nodes(),
                edges: graph.num_edges(),
                edge_kinds,
                atomic_groups: assignment_analysis.atomic_groups().len(),
                atomic_group_conflicts: assignment_report.atomic_group_conflicts.len(),
                cycles: assignment_report.cycles.len(),
                candidate_preparation_permitted: !rejected,
                translation_validation_accepts: translation_validation.report.accepts,
                full_graphs: graph_reports.enabled(),
            },
        )?;
        let candidate_assessment = evaluation.candidate_assessment();
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            EFFECT_TRANSLATION_VALIDATION_REPORT,
            &candidate_assessment.effect.report,
        )?;
        if rejected && policy == GateRejectionPolicy::Refuse {
            let mut report = candidate_assessment.report();
            report.mark_candidate_not_materialized()?;
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                CANDIDATE_RELOCATION_ASSESSMENT_REPORT,
                &report,
            )?;
        }
        if graph_reports.enabled() {
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                DEPENDENCY_GRAPH_REPORT,
                &assignment_analysis.dependency_graph_report(
                    program_graph.reference_for(report_out_dir, chunk_id)?,
                    program_graph.author_inputs(),
                ),
            )?;
        }
        if full_export_reentry {
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                output_layout::EXPORT_REENTRY_FULL_REPORT,
                export_reentry_report,
            )?;
        }
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            EXPORT_REENTRY_REPORT,
            &super::reentry_report::ExportReentrySummary::from(export_reentry_report),
        )?;
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            TRANSLATION_VALIDATION_REPORT,
            &translation_validation.report,
        )?;
    }
    // Legacy SCC/atomic evidence remains available for comparison, including
    // candidates the final effect/structural verdict accepts.
    if let Some(dir) = report_out_dir {
        if !assignment_report.atomic_group_conflicts.is_empty() {
            let wire = ::analysis::AtomicGroupConflictReport::from_conflicts(
                &assignment_report.atomic_group_conflicts,
                &|id| assignment_analysis.module_path(id),
                assignment_analysis.inputs().dependency_graph(),
                &|id| {
                    ::analysis::DependencyEdgeReport::from_edge(
                        assignment_analysis.inputs().dependency_graph().edge(id),
                    )
                },
            );
            write_chunk_report_json(dir, chunk_id, ATOMIC_GROUP_CONFLICTS_REPORT, &wire)?;
        }
        if !assignment_report.cycles.is_empty() {
            let wire = ::gate::BlockingSccEntry::from_cycle_reports(&assignment_report.cycles);
            write_chunk_report_json(dir, chunk_id, CYCLES_REPORT, &wire)?;
        }
    }
    if !rejected {
        return match evaluation.into_preparation() {
            CandidatePreparationOutcome::Prepared { linker_order } => {
                Ok(GateOutcome::Prepared { linker_order })
            }
            CandidatePreparationOutcome::Refused(_) => {
                unreachable!("ready preparation changed while consuming the evaluation")
            }
        };
    }
    let assessment = evaluation.candidate_assessment();
    let status = assessment.status();
    let legacy_diagnostic = if !assignment_report.atomic_group_conflicts.is_empty() {
        format!(
            " Legacy atomic-group comparison: {}{}",
            render_atomic_group_cause_guidance(&assignment_report.atomic_group_conflicts),
            render_atomic_group_conflict_summary(
                &assignment_report.atomic_group_conflicts,
                &|id| assignment_analysis.module_path(id),
                assignment_analysis.inputs().dependency_graph(),
            )
        )
    } else if !assignment_report.cycles.is_empty() {
        format!(
            " Legacy SCC comparison: {}",
            render_cycle_summary(&assignment_report.cycles)
        )
    } else {
        String::new()
    };
    let evidence = report_out_dir.map_or_else(String::new, |dir| {
        format!(
            " Final assessment at {}.{}",
            chunk_report_path(dir, chunk_id, CANDIDATE_RELOCATION_ASSESSMENT_REPORT).display(),
            graph_export_guidance(dir, chunk_id, graph_reports, ownership_subset.is_some()),
        )
    });
    let placement_diagnostic = if assessment.placement.emission_mappable {
        String::new()
    } else {
        let violations = assessment
            .placement
            .projection_violations
            .iter()
            .map(|violation| diagnostic_with_destination_context(assignment_analysis, violation))
            .collect::<Vec<_>>()
            .join("; ");
        format!(
            " source/destination placement projection is unmappable; typed violation(s): {violations}; unresolved checks: {:?}.",
            assessment.placement.coverage.unresolved_checks
        )
    };
    let initialization_diagnostic = if assessment.module_initialization.is_complete() {
        String::new()
    } else {
        format!(
            " Provider initialization remains unproved for {} cell(s): {:?}.",
            assessment.module_initialization.unproved_cells.len(),
            assessment.module_initialization.refusal,
        )
    };
    let provider_target_diagnostic = if assessment.provider_targets.is_complete() {
        String::new()
    } else {
        format!(
            " Conditional imported-body target preservation remains unproved for {} target(s): {:?}.",
            assessment.provider_targets.unproved_targets.len(),
            assessment.provider_targets.refusal,
        )
    };
    let diagnostic = format!(
        "materialize_logical_modules: chunk {chunk_id} — candidate relocation {status:?}; effects: {:?}.{placement_diagnostic}{initialization_diagnostic}{provider_target_diagnostic}{evidence}{legacy_diagnostic}",
        assessment.effect.assessment.decision,
    );
    match (policy, evaluation.into_preparation()) {
        (
            GateRejectionPolicy::LowerUnrunnable,
            CandidatePreparationOutcome::Refused(assignment),
        ) => Ok(GateOutcome::Rejected {
            assignment,
            rejection: ChunkGateRejection {
                chunk_id: chunk_id.to_string(),
                verdict: GateVerdict::Relocation { status },
                evidence_report: report_out_dir.map(|dir| {
                    chunk_report_path(dir, chunk_id, CANDIDATE_RELOCATION_ASSESSMENT_REPORT)
                }),
                dependency_graph_report: report_out_dir
                    .filter(|_| graph_reports.enabled())
                    .map(|dir| chunk_report_path(dir, chunk_id, DEPENDENCY_GRAPH_REPORT)),
                diagnostic,
            },
        }),
        (GateRejectionPolicy::Refuse, CandidatePreparationOutcome::Refused(_)) => {
            Err(anyhow::anyhow!(diagnostic))
        }
        (_, CandidatePreparationOutcome::Prepared { .. }) => {
            unreachable!("refused preparation changed while consuming the evaluation")
        }
    }
}

/// Build the per-plan `FinalModuleContent` rows for `ChunkModulesReport`.
/// Each plan's binding map is iterated in name-sorted order so the
/// resulting JSON is deterministic across runs.
fn build_final_module_report(
    module_plans: &[ModulePlan],
    assignment_analysis: &AssignmentAnalysis,
    chunk_identity: ChunkId,
    chunk_top_level_mark: swc_common::Mark,
) -> Vec<FinalModuleContent> {
    module_plans
        .iter()
        .map(|plan| {
            let mut sorted: Vec<(&String, &String)> = plan.bindings.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(b.0));
            let binding_names: Vec<String> = sorted.iter().map(|(k, _)| (*k).clone()).collect();
            let member_names: Vec<String> = sorted.iter().map(|(_, v)| (*v).clone()).collect();
            let binding_ids: Vec<BindingId> = binding_names
                .iter()
                .map(|name| {
                    BindingId::new(chunk_identity, top_level_id(name, chunk_top_level_mark))
                })
                .collect();
            let source_unit_ids = assignment_analysis
                .inputs()
                .source_unit_report_ids_for_bindings(binding_ids.iter())
                .expect("module report bindings belong to the analysis chunk");
            let binding_source_units = sorted
                .iter()
                .map(|(binding_name, _)| {
                    let binding_id = BindingId::new(
                        chunk_identity,
                        top_level_id(binding_name, chunk_top_level_mark),
                    );
                    let source_unit_ids = assignment_analysis
                        .inputs()
                        .source_unit_ids_for_binding(&binding_id)
                        .expect("module report bindings belong to the analysis chunk")
                        .iter()
                        .copied()
                        .map(source_unit_key)
                        .collect();
                    BindingSourceUnitAssociation {
                        binding_name: (*binding_name).clone(),
                        source_unit_ids,
                    }
                })
                .collect();
            FinalModuleContent {
                binding_names,
                binding_source_units,
                file: plan.target_file.clone(),
                member_names,
                path: spec::ModulePath::parse(&plan.target_path, "")
                    .expect("plan target_path is a canonical module path"),
                source_unit_ids,
                residual: plan.role == ModuleRole::Unassigned,
            }
        })
        .collect()
}

fn build_directory_dependency_facts(
    chunk_id: &str,
    entry_destination_report_target_file: &str,
    assignment_analysis: &AssignmentAnalysis,
) -> Vec<DirectoryDependencyFact> {
    let mut facts = Vec::new();
    for edge in assignment_analysis.inputs().dependency_graph().iter_edges() {
        let source_module = assignment_analysis.assignment().of(edge.from);
        let target_module = assignment_analysis.assignment().of(edge.to);
        if source_module == target_module {
            continue;
        }
        let Some(source_file) = module_output_file(
            chunk_id,
            entry_destination_report_target_file,
            assignment_analysis,
            source_module,
        ) else {
            continue;
        };
        let Some(target_file) = module_output_file(
            chunk_id,
            entry_destination_report_target_file,
            assignment_analysis,
            target_module,
        ) else {
            continue;
        };
        let symbol = edge.reason.binding().map(|id| {
            format!(
                "{}#{}",
                target_file,
                assignment_analysis
                    .inputs()
                    .export_name_for(&BindingId::new(
                        assignment_analysis.inputs().chunk_identity(),
                        id.clone(),
                    ))
                    .expect("dependency graph edge bindings belong to the analysis chunk")
            )
        });
        facts.push(DirectoryDependencyFact {
            source_file,
            target_file,
            edge_kind: edge.reason.kind(),
            symbol,
        });
    }
    facts.sort_by(|left, right| {
        left.source_file
            .cmp(&right.source_file)
            .then_with(|| left.target_file.cmp(&right.target_file))
            .then_with(|| left.edge_kind.cmp(&right.edge_kind))
            .then_with(|| left.symbol.cmp(&right.symbol))
    });
    facts
}

fn module_output_file(
    chunk_id: &str,
    entry_destination_report_target_file: &str,
    assignment_analysis: &AssignmentAnalysis,
    module: ModuleId,
) -> Option<String> {
    if module
        == assignment_analysis
            .assignment()
            .entry_destination()
            .module_id()
    {
        return Some(join_module_path(&[
            chunk_id,
            entry_destination_report_target_file,
        ]));
    }
    assignment_analysis
        .inputs()
        .planned_module(module)
        .map(|planned| join_module_path(&[chunk_id, &planned.target_file]))
}
