//! Selected-assignment finalization: plan settlement and sealed renames.

use super::gate_reports::write_selector_diagnostics;
use super::plan_builder::{ChunkPlan, ChunkPlanBuilder, ExplicitRequestContext};
use super::project_planned_modules;
use super::residual_scc::{contract_generated_residual_sccs, generated_residual_mutual_sccs};
use super::{ChunkContext, ChunkSpec, LogicalChunkPlan};
use crate::chunk_renames::collect_chunk_renames;
use crate::imports::ArtifactSourceImportResolutionCache;
use crate::naturalize::collect_plan_export_rename_intents;
use crate::plans::ModulePlan;
use analysis::{BindingCatalogue, top_level_id};
use analysis_projection::{AnalysisProjection, ChunkGraphInputs};
use anyhow::{Context, Result, ensure};
use artifact::JsFile;
use frontend_elaboration::ResolvedMemberSite;
use frontend_elaboration::chunk_ast::{ChunkAstAnalysis, SourceExportIdentity};
use gate::AssignmentAnalysis;
use graph::assignment_assembly::assemble_assignment;
use graph::placement_projection::SourceUnitLayout;
use graph_provenance::GraphProvenance;
use js_ast::ParsedJsModule;
use ownership_subset::SelectedModule;
use rename_ledger::{RenameLedger, SealValidation};
use source_identity::{ChunkId, StatementId};
use spec::{ChunkRenames, UnassignedMode};
use spec_resolution::LogicalRequest;
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_common::Mark;
use swc_ecma_ast::*;

/// What resolving a chunk's `commuting_statements` reads besides the chunk:
/// the spec path its evidence names and the resolved logical-module members a
/// `resolved_member` endpoint may name.
pub(crate) struct GraphConstructionContext<'a> {
    pub(crate) chunk_inputs: &'a BTreeMap<String, ChunkGraphInputs>,
    pub(crate) graph_provenance: Option<&'a GraphProvenance>,
    pub(crate) resolved_member_sites: &'a BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
}

pub(super) struct SelectedAssignmentBuildInput<'a, 'p, 's> {
    pub(super) context: ChunkContext<'a>,
    pub(super) spec: ChunkSpec<'a>,
    pub(super) chunk_unassigned_mode: UnassignedMode,
    pub(super) chunk_id_interned: ChunkId,
    pub(super) runtime_file: &'a JsFile,
    pub(super) runtime_ast: &'a ParsedJsModule,
    pub(super) ast_analysis: ChunkAstAnalysis,
    pub(super) requests: Vec<LogicalRequest>,
    pub(super) builder: ChunkPlanBuilder,
    pub(super) placement_anchors: Option<selector_resolve::PlacementAnchors>,
    pub(super) placement_source: Option<selector_resolve::Chunk<'p>>,
    pub(super) placement_modules: BTreeMap<String, spec::LogicalModule>,
    pub(super) precomputed: graph::atomic_groups::DependencyGraphAndAtomicGroups,
    pub(super) projection: &'p AnalysisProjection<'s>,
}

pub(crate) struct FinalizedLogicalAssignment<'a> {
    pub(super) chunk_unassigned_mode: UnassignedMode,
    pub(crate) chunk_id_interned: ChunkId,
    pub(super) runtime_file: &'a JsFile,
    pub(crate) runtime_ast: &'a ParsedJsModule,
    pub(crate) ast_analysis: ChunkAstAnalysis,
    pub(super) requests: Vec<LogicalRequest>,
    pub(super) selected_ownership: Option<BTreeSet<SelectedModule>>,
    pub(crate) source_unit_layout: SourceUnitLayout,
    pub(super) module_plans: Vec<ModulePlan>,
    pub(crate) module_count: usize,
    pub(crate) whole_statement_assignment: BTreeMap<StatementId, usize>,
    pub(super) atomic_group_owner_completions: Vec<super::plan_builder::AtomicGroupOwnerCompletion>,
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
    pub(crate) assignment_analysis: AssignmentAnalysis,
    pub(super) sealed_renames: rename_ledger::SealedRenames,
    pub(super) chunk_renames_map: HashMap<String, String>,
}

pub(super) fn finalize_selected_assignment<'a, 'p, 's>(
    input: SelectedAssignmentBuildInput<'a, 'p, 's>,
) -> Result<FinalizedLogicalAssignment<'a>> {
    let SelectedAssignmentBuildInput {
        context,
        spec,
        chunk_unassigned_mode,
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        mut builder,
        placement_anchors,
        placement_source,
        placement_modules,
        precomputed,
        projection,
    } = input;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        diagnostics,
        target_dir,
        ownership_subset: ownership_subset_view,
        report_emission,
        ..
    } = context;
    let ChunkSpec {
        chunk_renames,
        import_order,
        ..
    } = spec;
    ensure!(
        placement_modules.is_empty()
            || matches!(chunk_unassigned_mode, UnassignedMode::InlineInEntry),
        "fresh selected placement modules require inline_in_entry residual routing"
    );
    let import_priority = import_order.get(chunk_id).with_context(|| {
        format!(
            "materialized chunk {chunk_id} requires import_order (use [] for an all-entry chunk)"
        )
    })?;
    let ChunkAstAnalysis {
        runtime_import_facts,
        declarations,
        declaration_by_name,
        source_public_export_identities_by_statement,
        ..
    } = &ast_analysis;
    let chunk_top_level_mark = runtime_ast.top_level_mark;
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
    let ownership_subset = ownership_subset_view.map(|subset| subset.modules_for_chunk(chunk_id));
    if let Some(selected) = &ownership_subset {
        builder.retain_modules(selected)?;
        // Recreate only the derived owners required by retained claims.
        builder.apply_destructuring_group_placement(&ast_analysis.destructuring_groups);
        builder.adopt_bindings_of_claimed_anonymous_statements(declarations);
    }
    if let Some(source) = placement_source {
        let anchors = placement_anchors
            .as_ref()
            .context("selected placement is missing the full-base selector anchor snapshot")?;
        let (module_offset, occupied) =
            builder.begin_placement_layer(&precomputed, &source_unit_layout)?;
        let mut placement_requests = spec_resolution::logical_requests_for_chunk(
            Some(&placement_modules),
            &UnassignedMode::InlineInEntry,
            chunk_id,
        )?;
        let member_indices = frontend_elaboration::eligible_member_indices(
            chunk_id,
            &placement_requests,
            &ast_analysis,
            chunk_top_level_mark,
            &runtime_ast.module.body,
            !context.keep_going,
        )?;
        let modules =
            spec_resolution::selector_modules_for_requests(&placement_requests, &member_indices)?;
        let resolution = source.resolve_placement(&modules, anchors, &occupied)?;
        let claims =
            frontend_elaboration::project_member_claims(frontend_elaboration::MemberClaimInput {
                chunk_id,
                requests: &placement_requests,
                resolution: &resolution,
                resolved_members: &member_indices,
                ast: &ast_analysis,
                top_level_mark: chunk_top_level_mark,
                body: &runtime_ast.module.body,
                fail_fast: !context.keep_going,
                diagnostics,
            })?;
        let explicit_context = ExplicitRequestContext {
            body: &runtime_ast.module.body,
            declaration_by_name,
            chunk_top_level_mark,
            target_dir,
            chunk_id,
            target_file: &runtime_file.path,
            runtime_import_facts,
        };
        let mut imported_binding_resolver =
            ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);
        let mut imported_source_by_src = BTreeMap::new();
        for (index, request) in placement_requests.iter_mut().enumerate() {
            builder.add_explicit_request(
                module_offset + index,
                request,
                &explicit_context,
                &mut imported_binding_resolver,
                &mut imported_source_by_src,
            )?;
        }
        builder.drop_explicit_request_scratch();
        builder.claim_resolution_at_offset(
            module_offset,
            &placement_requests,
            resolution,
            member_indices,
            &claims,
            chunk_top_level_mark,
            chunk_id,
            &runtime_ast.module.body,
            declaration_by_name,
        )?;
        builder.apply_destructuring_group_placement(&ast_analysis.destructuring_groups);
        builder.adopt_bindings_of_claimed_anonymous_statements(declarations);
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
    builder.add_residual_sweep(catchall_request, declarations, target_dir)?;
    if matches!(&chunk_unassigned_mode, UnassignedMode::PerAtomicGroup) {
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
            source_public_export_identities_by_statement,
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
    let (sealed_renames, chunk_renames_map, assignment_analysis) = settle_assignment(
        &AssignmentSettlement {
            chunk_id,
            chunk_id_interned,
            chunk_renames: chunk_renames.get(chunk_id),
            chunk_top_level_mark,
            source_unit_layout: &source_unit_layout,
            source_public_export_identities_by_statement,
            import_priority,
            coarse_residual_mode,
        },
        precomputed,
        bindings_catalogue,
        &mut module_plans,
        &mut whole_statement_assignment,
        &mut generated_residual_plans,
    )?;
    chunk_analysis::check_transformed_chunk_admission(
        chunk_id,
        &runtime_ast.module,
        projection
            .chunk_analysis_inputs
            .get(chunk_id)
            .map(|inputs| inputs.admission_overrides)
            .unwrap_or_default(),
        assignment_analysis.splits_or_reorders() || sealed_renames.renames_any(),
    )?;
    let module_count = module_plans.len();
    Ok(FinalizedLogicalAssignment {
        chunk_unassigned_mode,
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        selected_ownership: ownership_subset,
        source_unit_layout,
        module_plans,
        module_count,
        whole_statement_assignment,
        atomic_group_owner_completions,
        unmatched_spec_claims,
        assignment_analysis,
        sealed_renames,
        chunk_renames_map,
    })
}

/// Per-chunk facts fixed across coarse residual contraction rounds.
struct AssignmentSettlement<'a> {
    chunk_id: &'a str,
    chunk_id_interned: ChunkId,
    chunk_renames: Option<&'a ChunkRenames>,
    chunk_top_level_mark: Mark,
    source_unit_layout: &'a SourceUnitLayout,
    source_public_export_identities_by_statement:
        &'a HashMap<StatementId, Vec<SourceExportIdentity>>,
    import_priority: &'a [spec::ModulePath],
    coarse_residual_mode: bool,
}

/// Seals the chunk's renames for the current plans and builds the final
/// assignment analysis, first contracting mutual SCCs among plans created by
/// coarse residual synthesis until none remain.
fn settle_assignment(
    settlement: &AssignmentSettlement<'_>,
    precomputed: graph::atomic_groups::DependencyGraphAndAtomicGroups,
    bindings_catalogue: BindingCatalogue,
    module_plans: &mut Vec<ModulePlan>,
    whole_statement_assignment: &mut BTreeMap<StatementId, usize>,
    generated_residual_plans: &mut BTreeSet<usize>,
) -> Result<(
    rename_ledger::SealedRenames,
    HashMap<String, String>,
    AssignmentAnalysis,
)> {
    let &AssignmentSettlement {
        chunk_id,
        chunk_id_interned,
        chunk_renames,
        chunk_top_level_mark,
        source_unit_layout,
        source_public_export_identities_by_statement,
        import_priority,
        coarse_residual_mode,
    } = settlement;
    let initial_generated_residual_count = generated_residual_plans.len();
    let maximum_coarsening_rounds = initial_generated_residual_count.saturating_sub(1);
    let refine_coarse_residuals = coarse_residual_mode && initial_generated_residual_count > 0;
    let mut graph_for_assignment = Some(precomputed);
    let mut catalogue_for_assignment = Some(bindings_catalogue);
    let mut coarsening_rounds = 0usize;
    // Each pass projects the canonical constraining graph for the current complete
    // layout. Only a whole typed Pass 1 SCC made entirely from plans created by
    // coarse synthesis can be contracted; cuts, rendered reports and the
    // diagnostic-only Pass 2 summary are not inputs here.
    Ok(loop {
        let sealed_renames = {
            let mut ledger = RenameLedger::default();
            if let Some(renames) = chunk_renames {
                collect_chunk_renames(renames, chunk_top_level_mark, &mut ledger)?;
            }
            collect_plan_export_rename_intents(module_plans, chunk_top_level_mark, &mut ledger);
            ledger.seal(&SealValidation::default())
        }?;
        let chunk_renames_map = sealed_renames.chunk_renames_by_name();
        let (planned_modules, entry_destination) =
            project_planned_modules(module_plans, source_unit_layout, chunk_top_level_mark)?;
        let assignment_chunk_renames: HashMap<Id, swc_atoms::Atom> = chunk_renames_map
            .iter()
            .map(|(local, exported)| {
                (
                    top_level_id(local, chunk_top_level_mark),
                    exported.as_str().into(),
                )
            })
            .collect();
        // Generated layout settles before checking the author's final owner
        // universe. Pass-1 SCCs depend only on the ownership quotient, never
        // on an import priority or a schedule-existence query.
        let residual_sccs = if refine_coarse_residuals {
            let precomputed = graph_for_assignment
                .as_ref()
                .expect("coarse assignment graph is retained for contractions");
            let bindings = catalogue_for_assignment
                .as_ref()
                .expect("coarse binding catalogue is retained for contractions");
            let local = bindings.local_view(chunk_id_interned)?;
            let assignment = assemble_assignment(
                &precomputed.dependency_graph,
                &precomputed.atomic_groups,
                &local,
                &planned_modules,
                entry_destination,
            )
            .assignment;
            generated_residual_mutual_sccs(
                &gate::assignment_constraining_cycles(&precomputed.dependency_graph, &assignment),
                generated_residual_plans,
                module_plans.len(),
                module_plans,
                source_public_export_identities_by_statement,
                chunk_top_level_mark,
            )
        } else {
            Vec::new()
        };
        if residual_sccs.is_empty() {
            let assignment_analysis = AssignmentAnalysis::build_with(
                chunk_id.to_string(),
                chunk_id_interned,
                graph_for_assignment
                    .take()
                    .expect("final graph is consumed once"),
                catalogue_for_assignment
                    .take()
                    .expect("final catalogue is consumed once"),
                planned_modules,
                assignment_chunk_renames,
                import_priority,
            )?;
            break (sealed_renames, chunk_renames_map, assignment_analysis);
        }

        ensure!(
            coarsening_rounds < maximum_coarsening_rounds,
            "coarse residual SCC contraction exceeded its finite plan-count bound"
        );
        let old_plan_count = module_plans.len();
        let old_generated_count = generated_residual_plans.len();
        contract_generated_residual_sccs(
            module_plans,
            catalogue_for_assignment
                .as_mut()
                .expect("coarse binding catalogue is retained for contractions"),
            whole_statement_assignment,
            generated_residual_plans,
            &residual_sccs,
        )?;
        ensure!(
            module_plans.len() < old_plan_count
                && generated_residual_plans.len() < old_generated_count,
            "coarse residual SCC contraction must reduce generated plan count"
        );
        coarsening_rounds += 1;
    })
}

/// Reuse the same plan finalization for a selected view whose dependency
/// topology comes from a source-bound saved graph rather than semantic facts.
pub(crate) fn finalize_claimed_selected_assignment<'a, 'p, 's>(
    claimed: LogicalChunkPlan<'a>,
    graph: graph::DependencyGraphInput,
    projection: &'p AnalysisProjection<'s>,
) -> Result<FinalizedLogicalAssignment<'a>> {
    let LogicalChunkPlan {
        structural_analysis,
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
    let chunk_id = context.chunk_id;
    let placement_modules = context
        .ownership_subset
        .map(|subset| subset.placements_for_chunk(chunk_id))
        .transpose()?
        .unwrap_or_default()
        .into_iter()
        .map(|(path, module)| (path.to_string(), module))
        .collect::<BTreeMap<_, _>>();
    let placement_source = (!placement_modules.is_empty()).then(|| {
        let census = Cow::Borrowed(&structural_analysis);
        selector_resolve::Chunk::new(chunk_id, &runtime_ast.module, census)
    });
    let graph = graph.with_source_declarations(&structural_analysis.per_statement)?;
    let dependency_graph = graph.graph().clone();
    let precomputed = graph::atomic_groups::DependencyGraphAndAtomicGroups {
        atomic_groups: graph::atomic_groups::compute_atomic_groups(&dependency_graph),
        dependency_graph,
        unused_commuting_statements: Vec::new(),
    };
    finalize_selected_assignment(SelectedAssignmentBuildInput {
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
    })
}
