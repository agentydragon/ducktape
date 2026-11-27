//! Program-wide summaries consumed by per-chunk source analysis.
//!
//! This stage resolves each chunk's imports against the artifact, gathers
//! source-resolved inputs, and calls the cross-module purity analysis
//! (`cross_module_purity`). Its results feed each chunk's
//! `AnalysisInputs`, including `imported_purities` for imported calls.

use crate::asserted_contracts::{project_export_set, project_onto_importers, scoped};
use analysis::diagnostics::{Diagnostics, ExportContract};
use analysis_projection::AnalysisProjection;
use analysis_vocabulary::inputs::{
    CallableSummary, ImportedPrimitiveRead, NativeIntegrity, ResolvedArgumentInput,
    ResolvedBindingValueInput, ResolvedInitializerCallEffectInput, ResolvedNativeOperationInput,
};
use analysis_vocabulary::purity::Purity;
use analysis_vocabulary::value::Value;
use anyhow::Result;
use artifact::{ArtifactIndexes, ChunkBundle};
use cross_module_purity::callable_summaries::{
    CallableSummaryInputs, resolve_imported_callable_summaries,
};
use cross_module_purity::export_primitives::resolve_export_primitives;
use cross_module_purity::imported_purities::resolve_imported_purities;
use cross_module_purity::stable_imports::resolve_stable_imports;
use graph_provenance::GraphProvenance;
use imported_admission::{
    admitted_imported_values, check_provider_closures, closed_factory_imports,
    imported_primitive_reads, imported_pure_new_constructors,
};
use input_resolution::resolve_initializer_call_effect_inputs;
use program_modules::{ProgramModules, program_modules};
use selected_inputs::{SelectedInputs, resolve_selected_inputs, validate_analysis_source_sites};
use source_identity::ChunkId;
use static_dependencies::resolved_static_dependency_graph;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use value_flow::program_bindings::ProgramBindings;
use vendor::VendorResolutionPlan;

mod imported_admission;
mod program_modules;
mod selected_inputs;
mod static_dependencies;

mod input_resolution;

/// Program-wide analysis summaries, keyed by source chunk name.
pub struct ProgramSummary {
    /// Fresh source analysis only; saved summaries restore derived chunk facts.
    pub structural_flows: value_flow::ProgramValueFlows,
    /// Per-chunk imported-binding verdicts (`AnalysisInputs::imported_purities`).
    pub bindings: BTreeMap<String, BTreeMap<String, Arc<Purity>>>,
    /// Per-chunk imports of never-rebound provider cells
    /// (`AnalysisInputs::stable_imports`).
    pub stable_imports: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk imported values admitted to ordinary candidate execution.
    /// Each value has a matching `primitive_reads` proof for the same local
    /// binding and primitive kinds; raw exported primitive facts remain in
    /// the source analysis and `export_primitives` closure above.
    pub values: BTreeMap<String, BTreeMap<String, Value>>,
    /// Per-chunk imported primitive read capabilities with source-cell
    /// provenance and a resolved provider closure excluding the importer.
    pub primitive_reads: BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>>,
    /// Per-chunk fluent-trusted import binding names (deep-purity roots),
    /// merged into `AnalysisAssumptions::fluent_bindings`.
    pub fluent: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk local names whose defining bindings assert `purity: pure_new`.
    pub constructors: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk bare imported callable summaries with their defining evidence.
    pub imported_callables: BTreeMap<String, BTreeMap<String, CallableSummary>>,
    /// Source-derived native mutation census shared by every analyzed chunk.
    pub native_integrity: Option<NativeIntegrity>,
    /// The whole-program bundle-owned globals (SPEC.md § Bundle-owned globals).
    pub bundle_globals: analysis_vocabulary::inputs::BundleGlobalNames,
    /// Source-resolved author inputs for the provider's own local analysis.
    pub value_inputs: BTreeMap<String, Vec<ResolvedBindingValueInput>>,
    /// Source-resolved native operation premises for the provider initializer.
    pub native_operation_inputs: BTreeMap<String, Vec<ResolvedNativeOperationInput>>,
    pub initializer_call_effect_inputs: BTreeMap<String, Vec<ResolvedInitializerCallEffectInput>>,
    /// Exact-site imported-array lifetime assumptions and source-resolved
    /// provider primitive slots for each selected spread.
    pub imported_primitive_array_spread_inputs: BTreeMap<
        String,
        Vec<analysis_vocabulary::inputs::ResolvedImportedPrimitiveArraySpreadInput>,
    >,
    pub inferred_set_constructions:
        BTreeMap<String, BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>>,
    pub argument_inputs: BTreeMap<String, Vec<ResolvedArgumentInput>>,
    /// Source-resolved commuting source-unit pairs, keyed by their chunk.
    pub inline_callback_factories: BTreeMap<String, BTreeSet<String>>,
    pub callback_factory_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    pub retained_callback_factories:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub retained_callback_factory_members:
        BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>>,
    pub retained_callback_constructors:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub no_reentry_exports: BTreeMap<String, BTreeSet<String>>,
    pub no_reentry_callback_exports:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub no_reentry_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    pub no_reentry_record_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    pub namespace_independent_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
}

fn chunk_allowed(chunk_id: ChunkId, chunk_filter: Option<&BTreeSet<ChunkId>>) -> bool {
    chunk_filter.is_none_or(|filter| filter.contains(&chunk_id))
}

/// Inputs that affect program-summary construction.
pub struct ProgramSummaryOptions<'a> {
    pub graph_provenance: Option<&'a GraphProvenance>,
    pub vendor_plan: Option<&'a VendorResolutionPlan>,
    /// Export name → replacement package of every vendor-swapped export, by
    /// defining chunk; summaries derived from those bodies name the package.
    pub replacement_packages: &'a BTreeMap<ChunkId, BTreeMap<String, String>>,
}

pub fn collect_program_summary(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    projection: &AnalysisProjection<'_>,
    options: ProgramSummaryOptions<'_>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    diagnostics: &Diagnostics,
) -> Result<ProgramSummary> {
    let ProgramSummaryOptions {
        graph_provenance,
        vendor_plan,
        replacement_packages,
    } = options;
    let chunk_export_purity = projection.chunk_export_purity;
    let chunk_analysis_inputs = projection.chunk_analysis_inputs.as_ref();
    let predefined_globals = projection.host_behavior.predefined_globals;
    let declared_pure_new_bindings = &projection.declared_pure_new();
    let ProgramModules {
        modules,
        export_value_sources,
        lexically_closed_modules,
        static_requests,
    } = program_modules(artifact, indexes, chunk_filter, replacement_packages);
    let bindings = ProgramBindings::new(
        modules.iter().map(|(module, facts)| facts.bindings(module)),
        export_value_sources
            .keys()
            .cloned()
            .chain(export_value_sources.values().flat_map(|source| {
                source
                    .copies
                    .iter()
                    .map(|copy| (copy.module.clone(), copy.export.clone()))
            })),
    );
    let export_primitives = resolve_export_primitives(&bindings, export_value_sources);
    let export_values: BTreeMap<_, _> = export_primitives
        .iter()
        .map(|(export, primitive)| (export.clone(), Value::Prim(primitive.kinds)))
        .collect();
    let static_dependencies = resolved_static_dependency_graph(
        artifact,
        indexes,
        &static_requests,
        &modules.keys().cloned().collect(),
        projection.host_behavior,
    );
    let initializer_call_effect_inputs = resolve_initializer_call_effect_inputs(
        artifact,
        &modules,
        chunk_analysis_inputs,
        &projection.source_sites,
        ProgramSummaryOptions {
            graph_provenance,
            vendor_plan,
            replacement_packages,
        },
        &static_dependencies,
    )?;
    let primitive_reads =
        imported_primitive_reads(&modules, &export_primitives, &static_dependencies);
    let values = admitted_imported_values(&modules, &primitive_reads, &export_values);
    validate_analysis_source_sites(
        artifact,
        &projection.source_sites,
        &projection.native_operation_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let SelectedInputs {
        value_inputs,
        native_operation_inputs,
        argument_inputs,
        no_accessor_install_callables,
    } = resolve_selected_inputs(
        artifact,
        &modules,
        projection,
        graph_provenance,
        chunk_filter,
    )?;
    let constructors = imported_pure_new_constructors(&modules, declared_pure_new_bindings);
    macro_rules! members {
        ($field:ident, $contract:ident) => {
            project_onto_importers(
                &modules,
                &scoped(chunk_export_purity, &modules, |p| &p.$field),
                ExportContract::$contract,
                diagnostics,
            )
        };
    }
    // Project each policy once. Raw namespace privacy needs the same complete
    // potentially aliasing family as normal execution, before source flow.
    let namespace_independent_members =
        members!(namespace_independent_members, NamespaceIndependentMembers);
    let callback_factory_members = members!(callback_factory_members, CallbackFactoryMembers);
    let retained_callback_factory_members = members!(
        retained_callback_factory_members,
        RetainedCallbackFactoryMembers
    );
    let no_reentry_members = members!(no_reentry_members, NoReentryMembers);
    let no_reentry_record_members = members!(no_reentry_record_members, NoReentryRecordMembers);
    let mut namespace_privacy_members = namespace_independent_members.clone();
    for module in modules.keys() {
        let family = namespace_privacy_members.entry(module.clone()).or_default();
        for root in callback_factory_members[module]
            .keys()
            .chain(retained_callback_factory_members[module].keys())
            .chain(no_reentry_members[module].keys())
            .chain(no_reentry_record_members[module].keys())
        {
            family.entry(root.clone()).or_default();
        }
    }
    let closed_factory_imports = closed_factory_imports(
        &modules,
        projection,
        &lexically_closed_modules,
        &static_dependencies,
    );
    let requested_original_provider_exports = initializer_call_effect_inputs
        .values()
        .flatten()
        .map(|input| (input.provider_chunk.clone(), input.provider_export.clone()))
        .collect();
    let mut callable_summaries = resolve_imported_callable_summaries(
        &bindings,
        &modules,
        &scoped(chunk_export_purity, &modules, |p| {
            &p.callback_factory_exports
        }),
        &scoped(chunk_export_purity, &modules, |p| {
            &p.descriptor_copy_factory_exports
        }),
        CallableSummaryInputs {
            closed_factory_imports: &closed_factory_imports,
            namespace_privacy_members: &namespace_privacy_members,
            callback_factory_members: &callback_factory_members,
            requested_original_provider_exports: &requested_original_provider_exports,
            selected_value_inputs: &value_inputs,
            selected_native_operation_inputs: &native_operation_inputs,
            selected_imported_primitive_array_spread_inputs: &projection
                .source_sites
                .imported_primitive_array_spread_inputs,
            argument_inputs: &argument_inputs,
            predefined_globals,
            no_accessor_install_callables: &no_accessor_install_callables,
            intrinsics_never_written: projection
                .intrinsics_never_written
                .map(|premise| {
                    premise
                        .families
                        .families()
                        .into_iter()
                        .map(Into::into)
                        .collect()
                })
                .unwrap_or_default(),
        },
        diagnostics,
    )?;
    let asserted_pure = scoped(chunk_export_purity, &modules, |p| &p.pure_exports);
    let mut stable_imports = resolve_stable_imports(&bindings, &modules, &asserted_pure);
    let imported_purities = resolve_imported_purities(
        &stable_imports,
        &modules,
        &asserted_pure,
        &mut callable_summaries,
        diagnostics,
    )?;
    // A proved imported primitive read names a provider binding declared once
    // and never written (`StableExportValue`).
    for (chunk, reads) in &primitive_reads {
        stable_imports
            .entry(chunk.clone())
            .or_default()
            .extend(reads.keys().cloned());
    }
    check_provider_closures(
        &initializer_call_effect_inputs,
        &modules,
        &callable_summaries,
        &static_dependencies,
    )?;
    macro_rules! exports {
        ($field:ident, $contract:ident) => {
            project_export_set(
                &modules,
                &scoped(chunk_export_purity, &modules, |p| &p.$field),
                ExportContract::$contract,
                diagnostics,
            )
        };
    }
    Ok(ProgramSummary {
        structural_flows: callable_summaries.structural_flows,
        no_reentry_callback_exports: members!(
            no_reentry_callback_exports,
            NoReentryCallbackExports
        ),
        retained_callback_constructors: members!(
            retained_callback_constructor_exports,
            RetainedCallbackConstructorExports
        ),
        retained_callback_factories: members!(
            retained_callback_factory_exports,
            RetainedCallbackFactoryExports
        ),
        retained_callback_factory_members,
        values,
        primitive_reads,
        bindings: imported_purities,
        stable_imports,
        fluent: exports!(fluent_exports, FluentExports),
        constructors,
        imported_callables: callable_summaries.imported_callables,
        native_integrity: callable_summaries.native_integrity,
        bundle_globals: callable_summaries.bundle_globals,
        value_inputs: callable_summaries.value_inputs,
        native_operation_inputs: callable_summaries.native_operation_inputs,
        initializer_call_effect_inputs,
        imported_primitive_array_spread_inputs: callable_summaries
            .imported_primitive_array_spread_inputs,
        inferred_set_constructions: callable_summaries.inferred_set_constructions,
        argument_inputs: callable_summaries.argument_inputs,
        inline_callback_factories: exports!(
            inline_callback_factory_exports,
            InlineCallbackFactoryExports
        ),
        namespace_independent_members,
        callback_factory_members,
        no_reentry_members,
        no_reentry_record_members,
        no_reentry_exports: exports!(no_reentry_exports, NoReentryExports),
    })
}

#[cfg(test)]
mod tests;
