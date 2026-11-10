//! Shared fact computation and loaded-fact validation, independent of lowering.
use super::progress::emit_debundle_progress;
use analysis::AnalysisInputs;
use analysis::analysis_inputs::CallableSummary;
use analysis::execution_evidence::SourceLocator;
use analysis::facts::{ChunkFactAnalysis, ExportReentryPolicy, StructuralChunkSourceFacts};
use analysis_projection::AnalysisProjection;
use anyhow::{Context, Result};
use artifact::{ArtifactIndexes, ChunkBundle, chunk_runtime_entry};
use chunk_analysis::{DynamicImportTarget, check_chunk_facts, compute_chunk_facts};
use js_ast::ParsedJsModule;
use source_identity::ChunkId;
use spec::AdmissionCheck;
use std::collections::BTreeMap;

/// One chunk's facts for `debundle analyze`: what `finish_logical_chunk`
/// computes, from the chunk's source, the projection and the cross-module
/// summaries, with no plan.
pub fn analyze_logical_chunk(
    artifact: &ChunkBundle,
    artifact_indexes: &ArtifactIndexes,
    chunk_id: &str,
    file: Option<&str>,
    export_reentry_policies: &BTreeMap<ChunkId, ExportReentryPolicy>,
    projection: &AnalysisProjection<'_>,
    program_summary: &super::program_summary::ProgramSummary,
) -> Result<ChunkFactAnalysis> {
    let chunk_id_interned = artifact
        .chunk_table
        .get(chunk_id)
        .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
    let (target_file, runtime_file, runtime_ast) =
        chunk_runtime_entry(artifact, chunk_id, chunk_id_interned, file)?;
    let source_path = runtime_file.metadata.source_path.clone();
    let line_index = runtime_ast.line_index();
    let structural_analysis = analysis::facts::collect_chunk_source_facts(
        &runtime_ast.module,
        Some(SourceLocator {
            source_path: &source_path,
            lines: &line_index,
        }),
    );
    emit_debundle_progress(chunk_id, "compute_chunk_analysis", "start");
    let LogicalChunkFacts { facts, .. } = compute_logical_chunk_facts(
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
        ChunkFacts::Compute(structural_analysis),
        projection,
        program_summary,
    )?;
    emit_debundle_progress(chunk_id, "compute_chunk_analysis", "end");
    Ok(facts)
}

/// What a chunk's fact analysis reads besides the projection and the
/// program summary: the chunk's source and where its dynamic imports
/// land. It never sees the chunk's plan builder or assignment.
pub struct ChunkFactsContext<'a> {
    pub artifact: &'a ChunkBundle,
    pub artifact_indexes: &'a ArtifactIndexes,
    pub chunk_id: &'a str,
    pub chunk_id_interned: ChunkId,
    pub target_file: &'a str,
    pub runtime_ast: &'a ParsedJsModule,
    pub source_path: &'a str,
    pub export_reentry_policy: &'a ExportReentryPolicy,
}

/// Where a chunk's facts come from.
pub enum ChunkFacts<'a> {
    /// Analyze the chunk from its structural facts.
    Compute(StructuralChunkSourceFacts<'a>),
    /// Facts an analysis artifact holds for this chunk.
    Loaded(ChunkFactAnalysis),
}

/// One chunk's checked facts and what the chunk report records beside them.
pub struct LogicalChunkFacts {
    pub facts: ChunkFactAnalysis,
    /// Imported callable summaries the analysis consumed.
    pub imported_callables: BTreeMap<String, CallableSummary>,
    /// Configured admission overrides that suppressed no violation.
    pub redundant_admission_overrides: Vec<AdmissionCheck>,
}

/// Analyze one chunk's facts from its source, its spec projection and the
/// program summary, or check facts loaded for it.
pub fn compute_logical_chunk_facts(
    context: ChunkFactsContext<'_>,
    facts: ChunkFacts<'_>,
    projection: &AnalysisProjection<'_>,
    program_summary: &super::program_summary::ProgramSummary,
) -> Result<LogicalChunkFacts> {
    let ChunkFactsContext {
        artifact,
        artifact_indexes,
        chunk_id,
        chunk_id_interned,
        target_file,
        runtime_ast,
        source_path,
        export_reentry_policy,
    } = context;
    // `local_property_effects` selects the facts pass's local-effect policy,
    // which travels in the hints.
    let chunk_analysis_inputs = projection
        .chunk_analysis_inputs
        .get(chunk_id)
        .cloned()
        .unwrap_or_default();
    let host_behavior = projection.host_behavior;
    let line_index = runtime_ast.line_index();
    // A3 admission resolver: where does a dynamic-import specifier in
    // this chunk's entry land? Same artifact resolution the specifier
    // rewriter uses; `SameChunk` marks a debundled internal module.
    let resolve_dynamic_import = |specifier: &str| match artifact_indexes
        .resolve_runtime_import_reference(
            specifier,
            chunk_id_interned,
            target_file,
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
            assumptions: projection
                .declared_assumptions
                .get(chunk_id)
                .cloned()
                .unwrap_or_default(),
            native_integrity: program_summary.native_integrity.clone(),
            current_chunk_id: Some(chunk_id.to_string()),
            ..AnalysisInputs::default()
        };
        inputs.assumptions.captured_value_inputs = program_summary
            .value_inputs
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.assumptions.native_operation_inputs = program_summary
            .native_operation_inputs
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.assumptions.initializer_call_effect_inputs = program_summary
            .initializer_call_effect_inputs
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.assumptions.imported_primitive_array_spread_inputs = program_summary
            .imported_primitive_array_spread_inputs
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.inferred_set_constructions = program_summary
            .inferred_set_constructions
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.stable_set_spreads = program_summary
            .stable_set_spreads
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.assumptions.argument_inputs = program_summary
            .argument_inputs
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.assumptions.reentry_free_operations = host_behavior.reentry_free_operations.clone();
        inputs.assumptions.predefined_globals = host_behavior.predefined_globals;
        inputs.assumptions.bundle_globals = program_summary.bundle_globals.clone();
        inputs.imported_purities = program_summary
            .bindings
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.stable_imports = program_summary
            .stable_imports
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.imported_values = program_summary
            .values
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        inputs.imported_primitive_reads = program_summary
            .primitive_reads
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        // Definition-side `pure_members` assertions projected onto this
        // chunk's local import bindings; merged (not overwritten) so spec
        // member annotations and cross-module assertions coexist.
        if let Some(member_sets) = program_summary.members.get(chunk_id) {
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
        if let Some(fluent) = program_summary.fluent.get(chunk_id) {
            inputs
                .assumptions
                .fluent_bindings
                .extend(fluent.iter().cloned());
        }
        if let Some(constructors) = program_summary.constructors.get(chunk_id) {
            inputs
                .assumptions
                .declared_pure_new
                .extend(constructors.iter().cloned());
        }
        if let Some(callables) = program_summary.imported_callables.get(chunk_id) {
            inputs.merge_imported_callables(callables.clone());
        }
        if let Some(factories) = program_summary.retained_callback_factories.get(chunk_id) {
            inputs
                .assumptions
                .retained_callback_factories
                .extend(factories.clone());
        }
        if let Some(callbacks) = program_summary.no_reentry_callback_exports.get(chunk_id) {
            inputs
                .assumptions
                .no_reentry_callback_exports
                .extend(callbacks.clone());
        }
        if let Some(constructors) = program_summary.retained_callback_constructors.get(chunk_id) {
            inputs
                .assumptions
                .retained_callback_constructors
                .extend(constructors.clone());
        }
        if let Some(members) = program_summary
            .retained_callback_factory_members
            .get(chunk_id)
        {
            inputs
                .assumptions
                .retained_callback_factory_members
                .extend(members.clone());
        }
        if let Some(factories) = program_summary.inline_callback_factories.get(chunk_id) {
            inputs
                .assumptions
                .inline_callback_factories
                .extend(factories.iter().cloned());
        }
        if let Some(members) = program_summary.namespace_independent_members.get(chunk_id) {
            inputs
                .assumptions
                .namespace_independent_members
                .extend(members.clone());
        }
        if let Some(members) = program_summary.callback_factory_members.get(chunk_id) {
            inputs
                .assumptions
                .callback_factory_members
                .extend(members.clone());
        }
        if let Some(members) = program_summary.no_reentry_members.get(chunk_id) {
            inputs
                .assumptions
                .no_reentry_members
                .extend(members.clone());
        }
        if let Some(members) = program_summary.no_reentry_record_members.get(chunk_id) {
            inputs
                .assumptions
                .no_reentry_record_members
                .extend(members.clone());
        }
        if let Some(bindings) = program_summary.no_reentry_exports.get(chunk_id) {
            inputs
                .assumptions
                .no_reentry_exports
                .extend(bindings.iter().cloned());
        }
        inputs.local_effect_policy = chunk_analysis_inputs.local_effect_policy;
        inputs.assumptions.trusted_dataflow_summaries =
            chunk_analysis_inputs.trusted_dataflow_summaries;
        inputs.assumptions.deferred_global_publications =
            chunk_analysis_inputs.deferred_global_publications.clone();
        inputs.assumptions.non_reentrant_global_membership = chunk_analysis_inputs
            .non_reentrant_global_membership
            .clone();
        inputs
    };
    let (facts, redundant_admission_overrides) = match facts {
        ChunkFacts::Loaded(facts) => {
            let redundant = check_chunk_facts(
                chunk_id,
                &runtime_ast.module,
                &facts,
                chunk_analysis_inputs.admission_overrides,
                &resolve_dynamic_import,
            )?;
            (facts, redundant)
        }
        ChunkFacts::Compute(structural_analysis) => {
            let structural_analysis = analysis::facts::complete_chunk_structural_with_inputs(
                structural_analysis,
                program_summary
                    .native_operation_inputs
                    .get(chunk_id)
                    .map(Vec::as_slice)
                    .unwrap_or_default(),
                program_summary
                    .stable_set_spreads
                    .get(chunk_id)
                    .unwrap_or(&BTreeMap::new()),
                program_summary
                    .inferred_set_constructions
                    .get(chunk_id)
                    .unwrap_or(&BTreeMap::new()),
                &analysis_inputs.assumptions.argument_inputs,
            );
            compute_chunk_facts(
                chunk_id,
                &runtime_ast.module,
                structural_analysis,
                &analysis_inputs,
                Some(SourceLocator {
                    source_path,
                    lines: &line_index,
                }),
                chunk_analysis_inputs.admission_overrides,
                export_reentry_policy,
                &resolve_dynamic_import,
            )?
        }
    };
    Ok(LogicalChunkFacts {
        facts,
        imported_callables: analysis_inputs.imported_callables,
        redundant_admission_overrides,
    })
}
