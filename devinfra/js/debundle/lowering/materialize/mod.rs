//! Per-chunk materialization: take an `DependencyGraphAndAtomicGroups` + spec plan, run the
//! chunk through `lower_chunk`, and emit a `MaterializedLogicalChunk` whose
//! files/applied/report stay separate from the source bundle until the
//! finalized emit-stage file set is constructed.

mod apply;
mod outcome_sink;
mod plan_builder;

use std::io::Write;

pub(super) use apply::{EmissionChunkOutputs, collect_materialized_logical_chunks};
use plan_builder::{ChunkPlan, ChunkPlanBuilder, ExplicitRequestContext};

use super::io::write_chunk_report_json;
use super::util::{render_atomic_group_cause_guidance, target_file_for_request};
use crate::chunk_ast::{ChunkAstAnalysis, analyze_chunk_ast};
use crate::chunk_renames::collect_chunk_renames;
use crate::imports::{ArtifactSourceImportResolutionCache, VendorReimportOracle};
use crate::lower::{
    LowerChunkAst, LowerChunkContext, LowerChunkInputs, LowerChunkPlan, LowerChunkSpecFacts,
    LoweredChunk, lower_chunk,
};
use crate::naturalize::collect_plan_export_rename_intents;
use crate::placement::EmissionPlacement;
use crate::plans::ModulePlan;
use crate::rebind_fold::compute_rebind_folds;
use crate::rename_ledger::{RenameLedger, SealValidation};
use crate::{
    ChunkModulesCounts, ChunkModulesReport, ExportReentryReport, FinalModuleContent,
    ReportEmission, RequestedLogicalModule,
};
use analysis::facts::{ExportReentryPolicy, StructuralChunkAnalysis};
use analysis::{
    AnalysisAssumptions, AnalysisInputs, BindingId, DependencyGraphAndAtomicGroups,
    DependencyGraphOptions, EntryDestination, LocalEffectPolicy, ModuleId, ModuleRole,
    PlannedModule, top_level_id,
};
use anyhow::{Context, Result, bail};
use artifact::paths::{join_module_path, normalize_module_path};
use artifact::reports::{ChunkValidationSummary, DirectoryDependencyFact, SelectedModuleLowering};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId, FileRole, JsFile, get_chunk_entry_path};
use chunk_analysis::{ChunkAnalysisOutput, DynamicImportTarget, compute_chunk_analysis};
use gate::{
    AssignmentAnalysis, ValidatedAssignment, render_atomic_group_conflict_summary,
    render_cycle_summary,
};
use js_ast::{ParsedJsModule, source_unit_ordinal_for_statement};
use output_layout::{
    ATOMIC_GROUP_CONFLICTS_REPORT, CYCLES_REPORT, DEPENDENCY_GRAPH_REPORT, EXPORT_REENTRY_REPORT,
    SELECTOR_DIAGNOSTICS_REPORT,
};
use spec::{ChunkRenames, InitializerPurity, LogicalModule, MemberPurity, UnassignedMode};
use spec_resolution::{
    LogicalRequest, MemberRequest, ProjectedChunk, ResolvedChunk, known_effect_from_member_effect,
    logical_requests_for_chunk, project_chunk,
};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::*;

const DEBUNDLE_PROGRESS_ENV: &str = "DUCKTAPE_DEBUNDLE_PROGRESS";

fn debundle_progress_enabled() -> bool {
    std::env::var(DEBUNDLE_PROGRESS_ENV)
        .ok()
        .is_some_and(|raw| {
            !matches!(
                raw.to_ascii_lowercase().as_str(),
                "" | "0" | "false" | "off" | "no"
            )
        })
}

fn emit_debundle_progress(chunk_id: &str, phase: &str, state: &str) {
    if !debundle_progress_enabled() {
        return;
    }
    eprintln!("[debundle progress] chunk={chunk_id} phase={phase} state={state}");
    let _ = std::io::stderr().flush();
}

/// Per-chunk inputs that identify the chunk and where its outputs
/// should land. Bundled into `MaterializeLogicalChunkInputs::context`.
pub(super) struct ChunkContext<'a> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) chunk_id: &'a str,
    pub(super) file: Option<&'a str>,
    pub(super) target_dir: &'a str,
    pub(super) keep_going: bool,
    pub(super) list_template_identifiers: bool,
    pub(super) report_emission: &'a ReportEmission,
    pub(super) graph_provenance: Option<&'a graph_provenance::GraphProvenance>,
    pub(super) export_reentry_policies: &'a BTreeMap<ChunkId, ExportReentryPolicy>,
    pub(super) export_reentry_reports: &'a BTreeMap<ChunkId, ExportReentryReport>,
    /// Vendor-plan consultation for runtime re-import construction;
    /// `None` when no vendor mark could
    /// affect construction.
    pub(super) vendor_import_oracle: Option<&'a VendorReimportOracle<'a>>,
}

/// Spec-derived per-chunk inputs: logical-module layout, chunk
/// renames, unassigned-mode, and analysis-options. Bundled into
/// `MaterializeLogicalChunkInputs::spec`.
pub(super) struct ChunkSpec<'a> {
    pub(super) logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    pub(super) chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub(super) unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub(super) chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub(super) host_no_reentry: Option<&'a spec::HostNoReentry>,
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
    builder: ChunkPlanBuilder,
    declared_assumptions: AnalysisAssumptions,
}

/// A prepared chunk after exact selector resolution has claimed its selected
/// members and anonymous statements. The structural facts are retained for the
/// hint-sensitive analysis that follows claim application.
pub(super) struct ClaimedLogicalChunk<'a> {
    prepared: PreparedLogicalChunk<'a>,
    structural_analysis: StructuralChunkAnalysis<'a>,
}

/// Prepare one chunk's lowering state and project its eligible selector
/// requests through the shared spec-resolution service.
pub(super) fn prepare_logical_chunk(
    inputs: MaterializeLogicalChunkInputs<'_>,
) -> Result<(PreparedLogicalChunk<'_>, ProjectedChunk<'_>)> {
    let MaterializeLogicalChunkInputs { context, spec } = inputs;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        file,
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
    let chunk_id_interned = artifact
        .chunk_table
        .get(chunk_id)
        .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
    emit_debundle_progress(chunk_id, "materialize_logical_chunk", "start");
    let target_file = file
        .map(normalize_module_path)
        .transpose()?
        .or_else(|| get_chunk_entry_path(artifact, chunk_id_interned))
        .with_context(|| {
            format!(
                "materialize_logical_modules could not determine entry file for chunk: {chunk_id}"
            )
        })?;
    let runtime_file = artifact
        .js_chunk(chunk_id_interned)?
        .get_file(&target_file)
        .with_context(|| {
            format!("materialize_logical_modules missing entry file for chunk: {chunk_id}")
        })?;
    let runtime_ast = runtime_file.ast().with_context(|| {
        format!("materialize_logical_modules missing entry AST for chunk: {chunk_id}")
    })?;
    // Chunk-wide `top_level_mark` for resolving spec-derived String
    // binding names to hygiene-aware `Id`s via `top_level_id`.
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    let source_path = runtime_file.metadata.source_path.clone();
    let ast_analysis = analyze_chunk_ast(&runtime_ast.module);
    let requests = logical_requests_for_chunk(
        spec.logical_modules.get(chunk_id),
        &chunk_unassigned_mode,
        chunk_id,
    )?;
    let mut explicit_requests = requests
        .iter()
        .filter(|request| !request.residual)
        .cloned()
        .collect::<Vec<_>>();
    let residual_request = requests.iter().find(|request| request.residual).cloned();

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
    builder.pull_destructure_siblings(&ast_analysis.destructure_siblings, chunk_top_level_mark)?;
    builder.add_residual_sweep(
        residual_request.as_ref(),
        chunk_unassigned_mode.catchall_file_target(),
        &ast_analysis.declarations,
        target_dir,
    )?;

    // The structural half is hint-free. Selector resolution runs over this
    // source-level fact layer so semantic annotations can be projected to
    // concrete bindings before the final, hint-sensitive dependency graph is built.
    emit_debundle_progress(chunk_id, "analyze_chunk_structural", "start");
    let line_index = runtime_ast.line_index();
    let structural_analysis = analysis::facts::analyze_chunk_structural(
        &runtime_ast.module,
        Some(&source_path),
        |span| line_index.line_range_and_start_column_for_span(span),
    );
    emit_debundle_progress(chunk_id, "analyze_chunk_structural", "end");
    // `add_explicit_request` left deferred selector members unclaimed; the
    // resolve every chunk shares assigns them and the anonymous statements
    // over the hint-free structural facts.
    let selectors =
        selector_resolve::Chunk::new(chunk_id, &runtime_ast.module, structural_analysis);
    let resolved_members = builder.selector_member_indices(
        &explicit_requests,
        chunk_top_level_mark,
        &ast_analysis.declaration_by_name,
    );
    emit_debundle_progress(chunk_id, "project_selectors", "start");
    let projected = project_chunk(selectors, &explicit_requests, resolved_members)?;
    emit_debundle_progress(chunk_id, "project_selectors", "end");
    Ok((
        PreparedLogicalChunk {
            context,
            spec,
            chunk_unassigned_mode,
            chunk_id_interned,
            target_file,
            runtime_ast,
            header_lines: runtime_file.header_lines.clone(),
            source_path,
            ast_analysis,
            requests,
            explicit_requests,
            builder,
            declared_assumptions: AnalysisAssumptions::default(),
        },
        projected,
    ))
}

/// Resolve source-selected bindings before the cross-chunk purity oracle runs.
/// The same resolved hints are later used by this chunk's source unit analysis.
pub(super) fn claim_prepared_chunk<'a>(
    mut prepared: PreparedLogicalChunk<'a>,
    resolved: ResolvedChunk<'a>,
) -> Result<ClaimedLogicalChunk<'a>> {
    let ResolvedChunk {
        resolution,
        resolved_members,
        structural_analysis,
    } = resolved;
    let chunk_top_level_mark = prepared.runtime_ast.top_level_mark;
    prepared.builder.claim_resolution(
        &prepared.explicit_requests,
        resolution,
        resolved_members,
        chunk_top_level_mark,
        prepared.context.chunk_id,
        &prepared.runtime_ast.module.body,
        &prepared.ast_analysis.declaration_by_name,
    )?;
    prepared.builder.pull_destructure_siblings(
        &prepared.ast_analysis.destructure_siblings,
        chunk_top_level_mark,
    )?;
    prepared
        .builder
        .adopt_bindings_of_claimed_anonymous_statements(&prepared.ast_analysis.declarations);
    prepared.declared_assumptions = collect_analysis_assumptions(
        &prepared.explicit_requests,
        &prepared.builder,
        prepared.spec.chunk_renames.get(prepared.context.chunk_id),
        &prepared.runtime_ast.module,
    )?;
    Ok(ClaimedLogicalChunk {
        prepared,
        structural_analysis,
    })
}

impl ClaimedLogicalChunk<'_> {
    pub(super) fn declared_pure_new(&self) -> (&str, &BTreeSet<String>) {
        (
            self.prepared.context.chunk_id,
            &self.prepared.declared_assumptions.declared_pure_new,
        )
    }
}

/// Analyze, plan and lower a chunk whose selector claims are already resolved.
pub(super) fn finish_logical_chunk(
    claimed: ClaimedLogicalChunk<'_>,
    cross_module_purities: &super::cross_module::CrossModulePurities,
) -> Result<MaterializedLogicalChunk> {
    let ClaimedLogicalChunk {
        prepared,
        structural_analysis,
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
        declared_assumptions,
        ..
    } = prepared;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        target_dir,
        report_emission,
        graph_provenance,
        vendor_import_oracle,
        export_reentry_policies,
        export_reentry_reports,
        ..
    } = context;
    let ChunkSpec {
        chunk_renames,
        chunk_analysis_options,
        host_no_reentry,
        ..
    } = spec;
    let ChunkAstAnalysis {
        runtime_import_facts,
        declarations,
        declaration_by_name,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
        ..
    } = ast_analysis;
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    // Fetched before hints assembly: `local_property_effects` selects
    // the facts pass's local-effect policy, which travels in the hints.
    let mut dependency_graph_options = chunk_analysis_options
        .get(chunk_id)
        .cloned()
        .unwrap_or_default();
    let line_index = runtime_ast.line_index();
    // A3 admission resolver: where does a dynamic-import specifier in
    // this chunk's entry land? Same artifact resolution the specifier
    // rewriter uses; `SameChunk` marks a debundled internal module.
    let resolve_dynamic_import = |specifier: &str| match artifact_indexes
        .resolve_runtime_import_reference(
            specifier,
            chunk_id_interned,
            &target_file,
            &artifact.chunk_table,
        ) {
        Some(resolved) if resolved.target_chunk_id == chunk_id_interned => {
            DynamicImportTarget::SameChunk
        }
        Some(_) => DynamicImportTarget::OtherChunk,
        None => DynamicImportTarget::External,
    };
    let analysis_inputs: AnalysisInputs = {
        let mut inputs = AnalysisInputs {
            assumptions: declared_assumptions,
            native_integrity: cross_module_purities.native_integrity.clone(),
            ..AnalysisInputs::default()
        };
        inputs.assumptions.captured_value_inputs = cross_module_purities
            .value_inputs
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.assumptions.host_no_reentry = host_no_reentry.cloned();
        inputs.imported_purities = cross_module_purities
            .bindings
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.imported_primitives = cross_module_purities
            .primitives
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        // Definition-side `pure_members` assertions projected onto this
        // chunk's local import bindings; merged (not overwritten) so spec
        // member annotations and cross-module assertions coexist.
        if let Some(member_sets) = cross_module_purities.members.get(chunk_id) {
            for (binding, members) in member_sets {
                inputs
                    .assumptions
                    .declared_pure_members
                    .entry(binding.clone())
                    .or_default()
                    .extend(members.iter().cloned());
            }
        }
        // Definition-side `fluent_exports` assertions projected onto this
        // chunk's local import bindings (deep-purity chain roots).
        if let Some(fluent) = cross_module_purities.fluent.get(chunk_id) {
            inputs
                .assumptions
                .fluent_bindings
                .extend(fluent.iter().cloned());
        }
        if let Some(constructors) = cross_module_purities.constructors.get(chunk_id) {
            inputs
                .assumptions
                .declared_pure_new
                .extend(constructors.iter().cloned());
        }
        if let Some(callables) = cross_module_purities.imported_callables.get(chunk_id) {
            inputs.merge_imported_callables(callables.clone());
        }
        if let Some(factories) = cross_module_purities
            .retained_callback_factories
            .get(chunk_id)
        {
            inputs
                .assumptions
                .retained_callback_factories
                .extend(factories.clone());
        }
        if let Some(callbacks) = cross_module_purities
            .no_reentry_callback_exports
            .get(chunk_id)
        {
            inputs
                .assumptions
                .no_reentry_callback_exports
                .extend(callbacks.clone());
        }
        if let Some(constructors) = cross_module_purities
            .retained_callback_constructors
            .get(chunk_id)
        {
            inputs
                .assumptions
                .retained_callback_constructors
                .extend(constructors.clone());
        }
        if let Some(members) = cross_module_purities
            .retained_callback_factory_members
            .get(chunk_id)
        {
            inputs
                .assumptions
                .retained_callback_factory_members
                .extend(members.clone());
        }
        if let Some(factories) = cross_module_purities
            .inline_callback_factories
            .get(chunk_id)
        {
            inputs
                .assumptions
                .inline_callback_factories
                .extend(factories.iter().cloned());
        }
        if let Some(members) = cross_module_purities
            .namespace_independent_members
            .get(chunk_id)
        {
            inputs
                .assumptions
                .namespace_independent_members
                .extend(members.clone());
        }
        if let Some(members) = cross_module_purities.callback_factory_members.get(chunk_id) {
            inputs
                .assumptions
                .callback_factory_members
                .extend(members.clone());
        }
        if let Some(members) = cross_module_purities.no_reentry_members.get(chunk_id) {
            inputs
                .assumptions
                .no_reentry_members
                .extend(members.clone());
        }
        if let Some(members) = cross_module_purities
            .no_reentry_record_members
            .get(chunk_id)
        {
            inputs
                .assumptions
                .no_reentry_record_members
                .extend(members.clone());
        }
        if let Some(bindings) = cross_module_purities.no_reentry_exports.get(chunk_id) {
            inputs
                .assumptions
                .no_reentry_exports
                .extend(bindings.iter().cloned());
        }
        if dependency_graph_options.local_property_effects {
            inputs.local_effect_policy = LocalEffectPolicy::LocalPropertyWrites;
        }
        inputs.assumptions.trusted_dataflow_summaries =
            dependency_graph_options.trusted_dataflow_summaries;
        inputs.assumptions.deferred_global_publications = dependency_graph_options
            .deferred_global_publications
            .clone();
        inputs.assumptions.non_reentrant_global_membership = dependency_graph_options
            .non_reentrant_global_membership
            .clone();
        // Source inputs were already resolved and validated in the whole-
        // program prepared execution index. Pure graph construction has no
        // parser/source context and must not silently consume this spec field.
        dependency_graph_options.value_inputs.clear();
        inputs
    };
    // Chunk analysis: hint-sensitive facts, dependency graph, and structural
    // atomic groups. See `chunk_analysis/mod.rs` for the current implementation.
    emit_debundle_progress(chunk_id, "compute_chunk_analysis", "start");
    let chunk_analysis = compute_chunk_analysis(
        chunk_id,
        &runtime_ast.module,
        structural_analysis,
        &analysis_inputs,
        Some(&source_path),
        |span| line_index.line_range_and_start_column_for_span(span),
        dependency_graph_options,
        export_reentry_policies
            .get(&chunk_id_interned)
            .copied()
            .unwrap_or_default(),
        &resolve_dynamic_import,
    )?;
    emit_debundle_progress(chunk_id, "compute_chunk_analysis", "end");
    let ChunkAnalysisOutput {
        fact_analysis: analysis,
        dependency_graph_and_atomic_groups: precomputed,
    } = chunk_analysis;
    apply_rebind_folds_from_chunk_analysis(&mut builder, &precomputed);
    if matches!(chunk_unassigned_mode, UnassignedMode::PerAtomicGroup) {
        builder.synthesize_per_atomic_group(&precomputed, &runtime_ast.module.body, target_dir)?;
    }
    builder.route_nested_var_statements(&runtime_ast.module.body, &declarations)?;
    if matches!(chunk_unassigned_mode, UnassignedMode::CatchallFile { .. }) {
        builder.route_unclaimed_anonymous_to_catchall(&precomputed, &runtime_ast.module.body);
    }
    if let Some(report) = builder.selector_outcome_report()
        && let Some(report_out_dir) = report_emission.rejection_dir()
    {
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            SELECTOR_DIAGNOSTICS_REPORT,
            &report,
        )?;
    }
    // Plan construction is finished — consume the builder and use
    // owned state from here on.
    let ChunkPlan {
        module_plans,
        bindings_catalogue,
        whole_statement_assignment,
        unmatched_spec_claims,
    } = builder.finalize(chunk_id_interned)?;
    // Plan structure is final — collect the explicit rename contributors
    // into the chunk's rename ledger and seal it. Seal hard-errors when
    // same-priority intents disagree on one binding's target; the sealed
    // output feeds the same application sites the pre-ledger maps fed
    // (Chunk scope → `chunk_renames_map` below, Module scope → the
    // per-plan naturalize pass in `lower_chunk`). No occupancy facts are
    // passed: the post-split bodies these renames must not collide with
    // don't exist yet, so target occupancy is validated downstream — the
    // entry ledger in `lower_chunk` re-collects the Chunk-scope intents
    // and the per-module naturalize ledgers re-collect the Module-scope
    // ones, each sealing against its body's occupancy.
    let sealed_renames = {
        let mut ledger = RenameLedger::default();
        if let Some(renames) = chunk_renames.get(chunk_id) {
            collect_chunk_renames(renames, chunk_top_level_mark, &mut ledger)?;
        }
        collect_plan_export_rename_intents(&module_plans, chunk_top_level_mark, &mut ledger);
        ledger.seal(&SealValidation::default())
    }?;
    let chunk_renames_map = sealed_renames.chunk_renames_by_name();
    let (planned_modules, entry_destination) = project_planned_modules(
        &module_plans,
        &runtime_ast.module.body,
        chunk_top_level_mark,
    );
    let redundant_purity_hints = analysis.redundant_purity_hints;
    let assignment_chunk_renames: HashMap<Id, swc_atoms::Atom> = chunk_renames_map
        .iter()
        .map(|(local, exported)| {
            (
                top_level_id(local, chunk_top_level_mark),
                exported.as_str().into(),
            )
        })
        .collect();
    let assignment_analysis = AssignmentAnalysis::build_with(
        chunk_id.to_string(),
        chunk_id_interned,
        precomputed,
        bindings_catalogue,
        planned_modules,
        assignment_chunk_renames,
        entry_destination,
    )?;
    let validated_assignment = validate_and_emit_reports(
        chunk_id,
        report_emission,
        graph_provenance,
        &assignment_analysis,
        &export_reentry_reports[&chunk_id_interned],
    )?;

    let placement = EmissionPlacement::from_validated(
        &validated_assignment,
        chunk_id_interned,
        &runtime_ast.module.body,
        module_plans.len(),
        &whole_statement_assignment,
    )?;

    let lowered = lower_chunk(LowerChunkInputs {
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
    })?;
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
    let validation = ChunkValidationSummary {
        linker_order: validated_assignment.report().linker_order.clone(),
    };
    let report = ChunkModulesReport {
        chunk_id: chunk_id.to_string(),
        counts: ChunkModulesCounts {
            applied: applied.len(),
            selected_bindings: placement.bindings().len(),
        },
        final_module_contents: final_modules,
        requested_logical_modules: requests
            .iter()
            .map(|request| RequestedLogicalModule {
                target_path: spec::ModulePath::parse(&request.target_path, "")
                    .expect("request target_path is a canonical module path"),
                residual: request.residual,
            })
            .collect(),
        imported_callables: analysis_inputs.imported_callables.clone(),
        redundant_purity_hints,
    };
    Ok(MaterializedLogicalChunk {
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
        vendor_reference_rewrites,
    })
}

/// Rebind-fold composer: fold rebind-only atomic groups into their
/// explicit destination. Bridges the pure
/// `compute_rebind_folds` decision in `lowering/rebind_fold.rs` over the chunk's
/// post-seed assignment (managed by `ChunkPlanBuilder`) into the
/// builder's plan-list/catalogue state.
///
/// This runs after the seed phases of assignment construction
/// (explicit requests, destructure pull, residual sweep) and before
/// `synthesize_per_atomic_group`. The decision step is spec-independent —
/// it only inspects the chunk-analysis dependency graph + atomic groups, the
/// builder's binding→plan assignment, and the residual landing-site
/// index — so it lives in the `analysis` crate; this thin composer
/// owns the application of those decisions to the builder's
/// `ModulePlan` list.
fn apply_rebind_folds_from_chunk_analysis(
    builder: &mut ChunkPlanBuilder,
    precomputed: &DependencyGraphAndAtomicGroups,
) {
    let folds = compute_rebind_folds(
        precomputed,
        &builder.owned_binding_destinations(),
        builder.residual_plan_index(),
    );
    builder.apply_rebind_folds(folds);
}

/// Project the materializer's real output plans into the analysis view. The
/// entry is a typed destination after the planned outputs, not a fabricated
/// `PlannedModule`; its wire label is chosen only when constructing reports.
fn project_planned_modules(
    module_plans: &[ModulePlan],
    body: &[ModuleItem],
    chunk_top_level_mark: swc_common::Mark,
) -> (Vec<PlannedModule>, EntryDestination) {
    let planned_modules: Vec<PlannedModule> = module_plans
        .iter()
        .map(|plan| PlannedModule {
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
            anonymous_source_unit_ordinals: plan
                .anonymous_statements
                .iter()
                .map(|body_idx| source_unit_ordinal_for_statement(body, *body_idx))
                .collect(),
        })
        .collect();
    let entry_destination = EntryDestination::after_planned_modules(planned_modules.len());
    (planned_modules, entry_destination)
}

/// Collect `AnalysisAssumptions` from binding annotations. These are semantic
/// trust assertions, not ownership claims.
fn collect_analysis_assumptions(
    explicit_requests: &[LogicalRequest],
    builder: &ChunkPlanBuilder,
    chunk_renames: Option<&ChunkRenames>,
    module: &Module,
) -> Result<AnalysisAssumptions> {
    let mut assumptions = AnalysisAssumptions::default();
    for (request_index, req) in explicit_requests.iter().enumerate() {
        for m in &req.members {
            collect_member_analysis_assumptions(
                request_index,
                req,
                m,
                builder,
                module,
                &mut assumptions,
            )?;
        }
    }
    collect_chunk_rename_analysis_assumptions(chunk_renames, module, &mut assumptions)?;
    Ok(assumptions)
}

fn collect_chunk_rename_analysis_assumptions(
    chunk_renames: Option<&ChunkRenames>,
    module: &Module,
    assumptions: &mut AnalysisAssumptions,
) -> Result<()> {
    let Some(chunk_renames) = chunk_renames else {
        return Ok(());
    };
    let mut by_export_name = BTreeMap::<String, Vec<String>>::new();
    for member in &chunk_renames.members {
        let binding = &member.selector.binding.name;
        let export_name = member.name.as_deref().unwrap_or(binding);
        by_export_name
            .entry(export_name.to_string())
            .or_default()
            .push(binding.clone());
    }

    for (export_name, annotation) in &chunk_renames.annotations {
        let Some(bindings) = by_export_name.get(export_name) else {
            bail!(
                "chunk_renames.annotations key `{export_name}` does not match any \
                 chunk_renames member name"
            );
        };
        if bindings.len() != 1 {
            bail!(
                "chunk_renames.annotations key `{export_name}` is ambiguous because that \
                 readable binding name is claimed {} times",
                bindings.len()
            );
        }
        collect_binding_annotation_hints(assumptions, &bindings[0], annotation, module)?;
    }
    Ok(())
}

fn collect_binding_annotation_hints(
    assumptions: &mut AnalysisAssumptions,
    binding: &str,
    annotation: &spec::BindingAnnotation,
    module: &Module,
) -> Result<()> {
    if annotation.initializer_purity == InitializerPurity::Pure {
        validate_initializer_purity_target(module, binding)?;
        assumptions
            .declared_pure_initializers
            .insert(binding.to_string());
    }
    if annotation.purity == MemberPurity::Pure {
        assumptions.declared_pure.insert(binding.to_string());
    }
    if annotation.purity == MemberPurity::PureNew {
        assumptions.declared_pure_new.insert(binding.to_string());
    }
    if !annotation.pure_members.is_empty() {
        assumptions
            .declared_pure_members
            .entry(binding.to_string())
            .or_default()
            .extend(annotation.pure_members.iter().cloned());
    }
    if !annotation.no_sync_callback_members.is_empty() {
        assumptions
            .no_sync_callback_members
            .entry(binding.to_string())
            .or_default()
            .extend(annotation.no_sync_callback_members.iter().cloned());
    }
    if let Some(effect) = known_effect_from_member_effect(annotation.effect) {
        assumptions
            .known_effects
            .insert(binding.to_string(), effect);
    }
    Ok(())
}

fn collect_member_analysis_assumptions(
    request_index: usize,
    request: &LogicalRequest,
    member: &MemberRequest,
    builder: &ChunkPlanBuilder,
    module: &Module,
    assumptions: &mut AnalysisAssumptions,
) -> Result<()> {
    if !member.has_analysis_assumptions() {
        return Ok(());
    }
    if let Some(binding) = builder.resolved_binding_for_member(request_index, member) {
        if member.initializer_purity == InitializerPurity::Pure {
            validate_initializer_purity_target(module, &binding)?;
        }
        member.collect_assumptions_for_binding(assumptions, &binding);
        return Ok(());
    }
    if !builder.fail_fast() {
        return Ok(());
    }
    bail!(
        "logical_module {}: semantic annotations on selector member `{}` could not be applied \
         because selector resolution did not produce a concrete binding",
        request.id,
        member.export_name,
    )
}

fn validate_initializer_purity_target(module: &Module, binding: &str) -> Result<()> {
    let mut initialized_sites = 0usize;
    let mut unsupported_sites = 0usize;
    let mut bare_var_sites = 0usize;

    for item in &module.body {
        let var = match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var.as_ref()),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                Decl::Var(var) => Some(var.as_ref()),
                _ => None,
            },
            _ => None,
        };
        if let Some(var) = var {
            for declarator in &var.decls {
                if !pattern_binds_name(&declarator.name, binding) {
                    continue;
                }
                match &declarator.name {
                    Pat::Ident(ident) if ident.id.sym.as_ref() == binding => {
                        if declarator.init.is_some() {
                            initialized_sites += 1;
                        } else if var.kind == VarDeclKind::Var {
                            bare_var_sites += 1;
                        } else {
                            unsupported_sites += 1;
                        }
                    }
                    _ => unsupported_sites += 1,
                }
            }
            continue;
        }

        let is_import_of_binding = match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
                .specifiers
                .iter()
                .any(|specifier| specifier.local().sym.as_ref() == binding),
            _ => false,
        };
        if is_import_of_binding || item_declares_nonvariable_name(item, binding) {
            unsupported_sites += 1;
        }
    }

    if initialized_sites == 1 && unsupported_sites == 0 {
        return Ok(());
    }
    let detail = if initialized_sites > 1 {
        "multiple initialized declarations"
    } else if unsupported_sites > 0 {
        "a non-simple, imported, or non-variable declaration"
    } else if bare_var_sites > 0 && initialized_sites == 0 {
        "no initialized declaration"
    } else {
        "no simple initialized variable declaration"
    };
    bail!(
        "initializer_purity for binding `{binding}` requires exactly one direct top-level simple variable initializer; found {detail}"
    )
}

fn item_declares_nonvariable_name(item: &ModuleItem, binding: &str) -> bool {
    let declaration_matches = |declaration: &Decl| match declaration {
        Decl::Fn(function) => function.ident.sym.as_ref() == binding,
        Decl::Class(class) => class.ident.sym.as_ref() == binding,
        _ => false,
    };
    match item {
        ModuleItem::Stmt(Stmt::Decl(declaration)) => declaration_matches(declaration),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => declaration_matches(&export.decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export)) => match &export.decl {
            DefaultDecl::Fn(function) => function
                .ident
                .as_ref()
                .is_some_and(|ident| ident.sym.as_ref() == binding),
            DefaultDecl::Class(class) => class
                .ident
                .as_ref()
                .is_some_and(|ident| ident.sym.as_ref() == binding),
            _ => false,
        },
        _ => false,
    }
}

fn pattern_binds_name(pattern: &Pat, binding: &str) -> bool {
    match pattern {
        Pat::Ident(ident) => ident.id.sym.as_ref() == binding,
        Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .any(|element| pattern_binds_name(element, binding)),
        Pat::Rest(rest) => pattern_binds_name(&rest.arg, binding),
        Pat::Object(object) => object.props.iter().any(|property| match property {
            ObjectPatProp::KeyValue(property) => pattern_binds_name(&property.value, binding),
            ObjectPatProp::Assign(property) => property.key.id.sym.as_ref() == binding,
            ObjectPatProp::Rest(property) => pattern_binds_name(&property.arg, binding),
        }),
        Pat::Assign(assign) => pattern_binds_name(&assign.left, binding),
        Pat::Expr(_) | Pat::Invalid(_) => false,
    }
}

/// Write per-chunk validation reports (dependency graph, atomic-group
/// conflicts, cycles) and bail with a human-readable summary when
/// the spec is unrealizable.
///
/// Under [`ReportEmission::OnRejection`] (the `debundle run
/// --dry-run` mode) the accept path writes nothing, but a rejection
/// still writes `dependency_graph.json` plus the rejection evidence so
/// the documented `debundle gate list/describe` follow-up works.
fn validate_and_emit_reports<'a>(
    chunk_id: &str,
    report_emission: &ReportEmission,
    graph_provenance: Option<&graph_provenance::GraphProvenance>,
    assignment_analysis: &'a AssignmentAnalysis,
    export_reentry_report: &ExportReentryReport,
) -> Result<ValidatedAssignment<'a>> {
    let validation = assignment_analysis.validate_for_emission();
    let assignment_report = match &validation {
        Ok(assignment) => assignment.report(),
        Err(report) => report,
    };
    let rejected = !assignment_report.atomic_group_conflicts.is_empty()
        || !assignment_report.cycles.is_empty();
    // Full mode writes the dependency graph unconditionally; OnRejection
    // writes it only alongside rejection evidence (`gate describe`
    // recomputes per-edge blame from it).
    let report_out_dir = if rejected {
        report_emission.rejection_dir()
    } else {
        report_emission.full_dir()
    };
    if let Some(report_out_dir) = report_out_dir {
        let mut dependency_graph_report = assignment_analysis.dependency_graph_report();
        dependency_graph_report.provenance = graph_provenance.cloned();
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            DEPENDENCY_GRAPH_REPORT,
            &dependency_graph_report,
        )?;
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            EXPORT_REENTRY_REPORT,
            export_reentry_report,
        )?;
    }
    if !assignment_report.atomic_group_conflicts.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            // Project onto the wire shape: source units as `"unit:N"`,
            // modules as canonical `ModulePath` — the same entity
            // keys `dependency_graph.json` uses, so the file joins
            // against the dependency graph without string surgery.
            let wire = ::analysis::AtomicGroupConflictReport::from_conflicts(
                &assignment_report.atomic_group_conflicts,
                &|id| assignment_analysis.module_path(id),
                assignment_analysis.inputs().dependency_graph(),
            );
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                ATOMIC_GROUP_CONFLICTS_REPORT,
                &wire,
            )?;
        }
        let summary = render_atomic_group_conflict_summary(
            &assignment_report.atomic_group_conflicts,
            &|id| assignment_analysis.module_path(id),
            assignment_analysis.inputs().dependency_graph(),
        );
        let causes = render_atomic_group_cause_guidance(&assignment_report.atomic_group_conflicts);
        let evidence = report_out_dir.map_or_else(String::new, |dir| {
            format!(
                " Full evidence written to {}; dependency graph written to {}.",
                dir.join(chunk_id)
                    .join(ATOMIC_GROUP_CONFLICTS_REPORT)
                    .display(),
                dir.join(chunk_id).join(DEPENDENCY_GRAPH_REPORT).display(),
            )
        });
        bail!(
            "materialize_logical_modules: chunk {chunk_id} has {n} atomic-group conflict(s) — the spec assigns members of one atomic group to different destination modules, forming a cycle in the module dependency graph that the constraining-edge SCC analysis says is unrealizable. Atomic groups come from `G_atomic` SCCs over the dependency graph (docs/design.md §\"Two classes of atom\"); every member must co-locate. {causes}Resolve by reconciling each group's claims into one destination.{evidence} Summary:\n{summary}",
            n = assignment_report.atomic_group_conflicts.len(),
        );
    }
    if !assignment_report.cycles.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            // Trim the wire shape before writing: keep `id` (array
            // index), `modules`, `cut`. Per-edge evidence is
            // recomputable from `dependency_graph.json` + this entry's
            // `modules` set via `debundle gate describe <id>`. See
            // `validation.rs` `BlockingSccEntry` for the schema and
            // `docs/cli.md` § "Gate queries" for the CLI surface.
            let wire = ::gate::BlockingSccEntry::from_cycle_reports(&assignment_report.cycles);
            write_chunk_report_json(report_out_dir, chunk_id, CYCLES_REPORT, &wire)?;
        }
        let summary = render_cycle_summary(&assignment_report.cycles);
        let evidence = report_out_dir.map_or_else(String::new, |dir| {
            format!(
                " Full per-cycle evidence at {}; dependency graph at {}.",
                dir.join(chunk_id).join(CYCLES_REPORT).display(),
                dir.join(chunk_id).join(DEPENDENCY_GRAPH_REPORT).display(),
            )
        });
        bail!(
            "materialize_logical_modules: chunk {chunk_id} — spec is unrealizable: {n} module-quotient SCC(s) with at-init / side-effect edges between members. Each SCC names the binding pairs whose split forced the cycle; co-locate them or break a back-edge.{evidence} Summary:\n{summary}",
            n = assignment_report.cycles.len(),
        );
    }
    // Defense-in-depth on the accept path: a cross-destination
    // rebinding write (clause 2) always co-locates with its target in
    // one atomic factor group, so a spec splitting them must have
    // produced an `atomic_group_conflicts` bail above. Reaching this
    // point with a non-empty clause-2 verdict means the atomic-group
    // glue and the realizability gate disagree — refuse to emit
    // rather than materialize a bundle that reassigns an ESM import.
    if !assignment_report.cross_rebinds.is_empty() {
        bail!(
            "materialize_logical_modules: chunk {chunk_id} — realizability verdict carries {n} cross-destination rebind(s) but no atomic-group conflict or blocking SCC was reported; this should be unreachable (rebinding writes co-locate with their target via atomic factor groups). Rebinds:\n  {rebinds}",
            n = assignment_report.cross_rebinds.len(),
            rebinds = assignment_report.cross_rebinds.join("\n  "),
        );
    }
    validation.map_err(|_| {
        anyhow::anyhow!(
            "chunk {chunk_id}: assignment validation rejected without a rendered diagnostic"
        )
    })
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
            FinalModuleContent {
                binding_names,
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
