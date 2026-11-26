//! Program-wide summaries consumed by per-chunk source analysis.
//!
//! This stage resolves each chunk's imports against the artifact, gathers
//! source-resolved inputs, and calls the cross-module purity analysis
//! (`cross_module_purity`). Its results feed each chunk's
//! `AnalysisInputs`, including `imported_purities` for imported calls.

use analysis::diagnostics::{Diagnostics, ExportContract};
use analysis_model::{PrimKinds, StableExportValue};
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis_vocabulary::inputs::{
    CallableSummary, ImportedPrimitiveRead, NativeIntegrity, PrimitiveExportCell,
    ResolvedArgumentInput, ResolvedBindingValueInput, ResolvedInitializerCallEffectInput,
    ResolvedNativeOperationInput, ResolvedNoAccessorInstall, SelectedBindingValueInput,
    SelectedNativeOperationInput,
};
use analysis_vocabulary::purity::Purity;
use analysis_vocabulary::value::Value;
use anyhow::{Context, Result, ensure};
use artifact::reports::{ImportSpecifierKind, StaticModuleRequest, StaticModuleRequestMode};
use artifact::{ArtifactIndexes, ChunkBundle};
use cross_module_purity::callable_summaries::{
    CallableSummaryInputs, ResolvedCallableSummaries, resolve_imported_callable_summaries,
};
use cross_module_purity::export_primitives::{
    ExportPrimitiveValue, ExportValueSource, resolve_export_primitives,
};
use cross_module_purity::imported_purities::resolve_imported_purities;
use cross_module_purity::module_facts::ModulePurityFacts;
use cross_module_purity::stable_imports::resolve_stable_imports;
use graph_provenance::GraphProvenance;
use swc_ecma_ast::{Decl, ModuleDecl, ModuleItem, Pat};
use value_flow::program_bindings::{ProgramBindings, ResolvedImport};
use vendor::VendorResolutionPlan;

use crate::asserted_contracts::{project_export_set, project_onto_importers, scoped};
use analysis_projection::{AnalysisNativeOperationInput, AnalysisProjection, AnalysisSourceSites};

mod input_resolution;
use input_resolution::{
    resolve_argument_inputs, resolve_initializer_call_effect_inputs,
    resolve_native_operation_inputs, resolve_no_accessor_install_callables, resolve_value_inputs,
};

/// Binding names introduced by an exported declaration (`export function`/
/// `export const`/`export class`), whose export name equals the local name.
/// Destructuring exports are skipped — conservative (the binding stays
/// unresolved, i.e. `unknown_call`).
fn exported_decl_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(function) => vec![function.ident.sym.to_string()],
        Decl::Class(class) => vec![class.ident.sym.to_string()],
        Decl::Var(var) => var
            .decls
            .iter()
            .filter_map(|declarator| match &declarator.name {
                Pat::Ident(binding) => Some(binding.id.sym.to_string()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

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

fn validate_analysis_source_sites(
    artifact: &ChunkBundle,
    source_sites: &AnalysisSourceSites,
    native_operation_inputs: &BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<()> {
    let mut site_chunks = BTreeSet::new();
    site_chunks.extend(source_sites.value_inputs.keys());
    site_chunks.extend(source_sites.native_call_identity_inputs.keys());
    site_chunks.extend(source_sites.initializer_call_effect_inputs.keys());
    site_chunks.extend(source_sites.argument_inputs.keys());
    site_chunks.extend(source_sites.no_accessor_install_callables.keys());
    site_chunks.extend(source_sites.imported_primitive_array_spread_inputs.keys());
    site_chunks.extend(native_operation_inputs.keys());
    for input in source_sites
        .imported_primitive_array_spread_inputs
        .values()
        .flatten()
    {
        site_chunks.extend(input.operands.iter().map(|operand| &operand.provider_chunk));
    }
    ensure!(
        site_chunks.iter().copied().collect::<BTreeSet<_>>()
            == source_sites.source_sha256.keys().collect::<BTreeSet<_>>(),
        "analysis projection source-site chunks do not match their source digests"
    );
    if site_chunks.is_empty() {
        return Ok(());
    }
    let provenance =
        graph_provenance.context("analysis source sites require parser-input provenance")?;
    for chunk_name in site_chunks {
        let chunk = artifact
            .chunks
            .iter()
            .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == chunk_name)
            .with_context(|| {
                format!("analysis source sites name no loaded provider chunk {chunk_name}")
            })?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "analysis source-site provider {chunk_name} is outside the analyzed import closure"
        );
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| {
                format!("analysis source-site provider {chunk_name} has no parser-input provenance")
            })?;
        let expected = &source_sites.source_sha256[chunk_name];
        ensure!(
            &source.sha256 == expected,
            "analysis source-site provider {chunk_name} has stale parser input: expected {}, found {}",
            expected,
            source.sha256
        );
    }
    Ok(())
}

fn resolved_target_is_entry(
    artifact: &ChunkBundle,
    target: &artifact::ResolvedImportReference,
) -> bool {
    artifact
        .find_chunk(target.target_chunk_id)
        .is_some_and(|chunk| chunk.analysis.entry_file == target.target_file)
}

struct ResolvedStaticDependencies {
    targets: BTreeMap<String, BTreeSet<String>>,
    ordinary_requests: BTreeMap<String, BTreeSet<(String, String)>>,
    complete_modules: BTreeSet<String>,
}

fn resolved_static_dependency_graph(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    requests: &BTreeMap<String, (ChunkId, String, Vec<StaticModuleRequest>)>,
    analyzed_modules: &BTreeSet<String>,
    host_behavior: &spec::HostBehavior,
) -> ResolvedStaticDependencies {
    let mut dependencies = BTreeMap::new();
    let mut ordinary_dependencies = BTreeMap::new();
    let mut complete_modules = BTreeSet::new();
    for (module, (chunk, entry_file, module_requests)) in requests {
        let mut module_dependencies = BTreeSet::new();
        let mut module_ordinary_dependencies = BTreeSet::new();
        let mut is_complete = true;
        for request in module_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                is_complete = false;
            }
            let Some(target) = indexes.resolve_runtime_import_reference(
                &request.specifier,
                *chunk,
                entry_file,
                &artifact.chunk_table,
            ) else {
                if request.mode == StaticModuleRequestMode::OrdinaryEsm
                    && host_behavior
                        .reentry_free_module(&request.specifier)
                        .is_some()
                {
                    continue;
                }
                is_complete = false;
                continue;
            };
            if !resolved_target_is_entry(artifact, &target) {
                is_complete = false;
                continue;
            }
            let target_module = artifact
                .chunk_table
                .name(target.target_chunk_id)
                .to_string();
            module_dependencies.insert(target_module.clone());
            if request.mode == StaticModuleRequestMode::OrdinaryEsm {
                module_ordinary_dependencies
                    .insert((request.specifier.clone(), target_module.clone()));
            }
            if artifact.find_chunk(target.target_chunk_id).is_none()
                || !analyzed_modules.contains(&target_module)
            {
                is_complete = false;
            }
        }
        dependencies.insert(module.clone(), module_dependencies);
        ordinary_dependencies.insert(module.clone(), module_ordinary_dependencies);
        if is_complete {
            complete_modules.insert(module.clone());
        }
    }
    ResolvedStaticDependencies {
        targets: dependencies,
        ordinary_requests: ordinary_dependencies,
        complete_modules,
    }
}

fn resolved_closure_precedes_importer(
    importer: &str,
    provider: &str,
    source_specifier: &str,
    dependencies: &ResolvedStaticDependencies,
) -> Option<BTreeSet<String>> {
    if !dependencies
        .ordinary_requests
        .get(importer)
        .is_some_and(|requests| {
            requests.contains(&(source_specifier.to_string(), provider.to_string()))
        })
    {
        return None;
    }
    let mut pending = vec![provider.to_string()];
    let mut visited = BTreeSet::new();
    while let Some(module) = pending.pop() {
        if module == importer {
            return None;
        }
        if !visited.insert(module.clone()) {
            continue;
        }
        if !dependencies.complete_modules.contains(&module) {
            return None;
        }
        pending.extend(dependencies.targets.get(&module)?.iter().cloned());
    }
    Some(visited)
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

/// Oracle inputs assembled from the prepared syntax and matching manifests.
struct ProgramModules<'a> {
    modules: BTreeMap<String, ModulePurityFacts<'a>>,
    export_value_sources: BTreeMap<(String, String), ExportValueSource>,
    lexically_closed_modules: BTreeSet<String>,
    static_requests: BTreeMap<String, (ChunkId, String, Vec<StaticModuleRequest>)>,
}

fn program_modules<'a>(
    artifact: &'a ChunkBundle,
    indexes: &ArtifactIndexes,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    replacement_packages: &BTreeMap<ChunkId, BTreeMap<String, String>>,
) -> ProgramModules<'a> {
    let mut modules = BTreeMap::new();
    let mut local_export_values =
        BTreeMap::<(String, String), (PrimKinds, BTreeSet<String>)>::new();
    let mut export_value_sources = BTreeMap::<(String, String), ExportValueSource>::new();
    let mut lexically_closed_modules = BTreeSet::new();
    let mut static_requests =
        BTreeMap::<String, (ChunkId, String, Vec<StaticModuleRequest>)>::new();
    for chunk_artifact in &artifact.chunks {
        if !chunk_allowed(chunk_artifact.chunk_id, chunk_filter) {
            continue;
        }
        let chunk_name = artifact.chunk_table.name(chunk_artifact.chunk_id);
        let entry_file = &chunk_artifact.analysis.entry_file;
        let Some(parsed) = chunk_artifact
            .js
            .get_file(entry_file)
            .and_then(|file| file.ast())
        else {
            continue;
        };
        if chunk_artifact
            .analysis
            .runtime_facts
            .global_uses
            .as_ref()
            .is_some_and(|uses| !uses.dynamic_scope)
        {
            lexically_closed_modules.insert(chunk_name.to_string());
        }
        let import_records = &chunk_artifact.analysis.imports;
        let export_alias_records = &chunk_artifact.analysis.export_aliases;
        let stable_export_values = &chunk_artifact.analysis.runtime_facts.stable_export_values;
        let module_static_requests = &chunk_artifact.analysis.runtime_facts.static_requests;
        static_requests.insert(
            chunk_name.to_string(),
            (
                chunk_artifact.chunk_id,
                entry_file.clone(),
                module_static_requests.clone(),
            ),
        );
        for (export, value) in stable_export_values {
            let key = (chunk_name.to_string(), export.clone());
            let (source, imported_export) = match value {
                StableExportValue::Local { kinds, imports } => {
                    local_export_values.insert(key.clone(), (*kinds, imports.clone()));
                    continue;
                }
                StableExportValue::ReExport { source, export } => (source, export),
            };
            let Some(target) = indexes.resolve_runtime_import_reference(
                source,
                chunk_artifact.chunk_id,
                entry_file,
                &artifact.chunk_table,
            ) else {
                continue;
            };
            if !chunk_allowed(target.target_chunk_id, chunk_filter)
                || !resolved_target_is_entry(artifact, &target)
            {
                continue;
            }
            export_value_sources.insert(
                key,
                ExportValueSource {
                    kinds: PrimKinds::NONE,
                    copies: vec![ResolvedImport {
                        module: artifact
                            .chunk_table
                            .name(target.target_chunk_id)
                            .to_string(),
                        export: imported_export.clone(),
                        source_specifier: source.clone(),
                    }],
                    cell: None,
                },
            );
        }
        let mut imports = BTreeMap::new();
        for import in import_records {
            // Same artifact-output resolution the specifier rewriter uses;
            // imports of paths outside the artifact (vendor packages) don't
            // resolve and are skipped — conservative.
            let Some(target) = indexes.resolve_runtime_import_reference(
                &import.source,
                chunk_artifact.chunk_id,
                entry_file,
                &artifact.chunk_table,
            ) else {
                continue;
            };
            if !chunk_allowed(target.target_chunk_id, chunk_filter)
                || !resolved_target_is_entry(artifact, &target)
            {
                continue;
            }
            let target_chunk_name = artifact.chunk_table.name(target.target_chunk_id);
            for specifier in &import.specifiers {
                let export = match specifier.kind {
                    ImportSpecifierKind::Named => specifier
                        .imported
                        .clone()
                        .unwrap_or_else(|| specifier.local.clone()),
                    ImportSpecifierKind::Default => "default".to_string(),
                    // Namespace imports are member-call sites (`ns.foo()`),
                    // outside the bare-Ident callee arm the verdicts feed.
                    ImportSpecifierKind::Namespace => continue,
                };
                imports.insert(
                    specifier.local.clone(),
                    ResolvedImport {
                        module: target_chunk_name.to_string(),
                        export,
                        source_specifier: import.source.clone(),
                    },
                );
            }
        }
        // `export_aliases` only carries `export { x as y }` specifiers and
        // `export default`. Exported declarations (`export function foo`,
        // `export const foo`, `export class Foo`) are not aliases, so add them
        // directly from the body — their export name is the local name.
        let mut exports: BTreeMap<String, String> = export_alias_records
            .iter()
            .filter_map(|alias| {
                alias
                    .local
                    .clone()
                    .map(|local| (alias.exported.clone(), local))
            })
            .collect();
        for item in &parsed.module.body {
            if let ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) = item {
                for name in exported_decl_names(&export_decl.decl) {
                    exports.entry(name.clone()).or_insert(name);
                }
            }
        }
        modules.insert(
            chunk_name.to_string(),
            ModulePurityFacts {
                body: &parsed.module.body,
                imports,
                exports,
                source: Some((
                    chunk_artifact.js.get_file(entry_file).map_or_else(
                        || chunk_artifact.analysis.source_path.clone(),
                        |file| file.metadata.source_path.clone(),
                    ),
                    parsed.line_index(),
                )),
                replacement_packages: replacement_packages
                    .get(&chunk_artifact.chunk_id)
                    .cloned()
                    .unwrap_or_default(),
            },
        );
    }

    for ((chunk, export), (kinds, local_imports)) in local_export_values {
        let Some(module) = modules.get(&chunk) else {
            continue;
        };
        let copies: Option<Vec<_>> = local_imports
            .iter()
            .map(|local| module.imports.get(local).cloned())
            .collect();
        if let Some(copies) = copies {
            export_value_sources.insert(
                (chunk.clone(), export.clone()),
                ExportValueSource {
                    kinds,
                    copies,
                    cell: Some(PrimitiveExportCell {
                        module: chunk,
                        export,
                    }),
                },
            );
        }
    }
    ProgramModules {
        modules,
        export_value_sources,
        lexically_closed_modules,
        static_requests,
    }
}

fn imported_primitive_reads(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    export_primitives: &BTreeMap<(String, String), ExportPrimitiveValue>,
    static_dependencies: &ResolvedStaticDependencies,
) -> BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>> {
    modules
        .iter()
        .map(|(importer, module)| {
            let reads = module
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let primitive =
                        export_primitives.get(&(target.module.clone(), target.export.clone()))?;
                    if primitive.kinds.is_empty() || primitive.source_cells.is_empty() {
                        return None;
                    }
                    let dependency_closure = resolved_closure_precedes_importer(
                        importer,
                        &target.module,
                        &target.source_specifier,
                        static_dependencies,
                    )?;
                    if !primitive.source_cells.iter().all(|cell| {
                        dependency_closure.contains(&cell.module)
                            && export_primitives
                                .get(&(cell.module.clone(), cell.export.clone()))
                                .is_some_and(|source| !source.kinds.is_empty())
                    }) {
                        return None;
                    }
                    Some((
                        local.clone(),
                        ImportedPrimitiveRead {
                            kinds: primitive.kinds,
                            provider_module: target.module.clone(),
                            dependency_closure,
                            source_cells: primitive.source_cells.clone(),
                        },
                    ))
                })
                .collect();
            (importer.clone(), reads)
        })
        .collect()
}

/// Raw stable export kinds are useful while resolving copy and re-export
/// chains, but they do not prove that an imported binding's GetValue is
/// available at its consumer. Keep the existing dependency-closure read
/// capability as the single join at the candidate-input boundary.
fn admitted_imported_values(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    primitive_reads: &BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>>,
    export_values: &BTreeMap<(String, String), Value>,
) -> BTreeMap<String, BTreeMap<String, Value>> {
    modules
        .iter()
        .map(|(name, module)| {
            let proved_reads = primitive_reads.get(name);
            let values = module
                .imported_values(export_values)
                .into_iter()
                .filter(|(local, value)| {
                    proved_reads
                        .and_then(|reads| reads.get(local))
                        .is_some_and(|read| *value == Value::Prim(read.kinds))
                })
                .collect();
            (name.clone(), values)
        })
        .collect()
}

/// Source-resolved author inputs that callable summaries consume.
struct SelectedInputs {
    value_inputs: BTreeMap<String, Vec<SelectedBindingValueInput>>,
    native_operation_inputs: BTreeMap<String, Vec<SelectedNativeOperationInput>>,
    argument_inputs: BTreeMap<String, Vec<ResolvedArgumentInput>>,
    no_accessor_install_callables: BTreeMap<String, Vec<ResolvedNoAccessorInstall>>,
}

fn resolve_selected_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    projection: &AnalysisProjection<'_>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<SelectedInputs> {
    let chunk_analysis_inputs = projection.chunk_analysis_inputs.as_ref();
    let value_inputs = resolve_value_inputs(
        artifact,
        modules,
        chunk_analysis_inputs,
        &projection.source_sites.value_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let native_operation_inputs = resolve_native_operation_inputs(
        artifact,
        modules,
        chunk_analysis_inputs,
        graph_provenance,
        chunk_filter,
        &projection.native_operation_inputs,
        &projection.source_sites,
    )?;
    let argument_inputs = resolve_argument_inputs(
        artifact,
        modules,
        chunk_analysis_inputs,
        &projection.source_sites.argument_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let no_accessor_install_callables = resolve_no_accessor_install_callables(
        artifact,
        modules,
        chunk_analysis_inputs,
        &projection.source_sites.no_accessor_install_callables,
        chunk_filter,
    )?;
    Ok(SelectedInputs {
        value_inputs,
        native_operation_inputs,
        argument_inputs,
        no_accessor_install_callables,
    })
}

/// A structurally selected member's trusted constructor contract follows
/// every export of that resolved binding. The importer sees its local
/// binding name; neither spec nor analysis needs the minified export name.
fn imported_pure_new_constructors(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    declared_pure_new_bindings: &BTreeMap<String, BTreeSet<String>>,
) -> BTreeMap<String, BTreeSet<String>> {
    modules
        .iter()
        .map(|(importer, facts)| {
            let locals = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let binding = modules.get(&target.module)?.exports.get(&target.export)?;
                    declared_pure_new_bindings
                        .get(&target.module)?
                        .contains(binding)
                        .then(|| local.clone())
                })
                .collect();
            (importer.clone(), locals)
        })
        .collect()
}

/// A fresh result cannot be allocated at an importer AST site when all
/// provider code is in this complete static closure, which excludes that
/// importer. Dynamic scope cannot supply the missing identity proof.
fn closed_factory_imports(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    projection: &AnalysisProjection<'_>,
    lexically_closed_modules: &BTreeSet<String>,
    static_dependencies: &ResolvedStaticDependencies,
) -> BTreeMap<String, BTreeSet<String>> {
    let chunk_export_purity = projection.chunk_export_purity;
    modules
        .iter()
        .map(|(module, facts)| {
            let mut closures = BTreeMap::new();
            let imports = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    if !lexically_closed_modules.contains(module)
                        || !chunk_export_purity
                            .get(&target.module)
                            .is_some_and(|policy| {
                                policy.callback_factory_exports.contains(&target.export)
                                    || policy.callback_factory_members.contains_key(&target.export)
                            })
                    {
                        return None;
                    }
                    let closed = *closures
                        .entry((&target.module, &target.source_specifier))
                        .or_insert_with(|| {
                            resolved_closure_precedes_importer(
                                module,
                                &target.module,
                                &target.source_specifier,
                                static_dependencies,
                            )
                            .is_some_and(|closure| {
                                closure
                                    .iter()
                                    .all(|provider| lexically_closed_modules.contains(provider))
                            })
                        });
                    closed.then(|| local.clone())
                })
                .collect();
            (module.clone(), imports)
        })
        .collect()
}

/// Rejects initializer-call-effect inputs that a source-known original
/// provider write contradicts, and imported primitive array spreads whose
/// providers lack a complete static initialization closure before the
/// consumer.
fn check_provider_closures(
    initializer_call_effect_inputs: &BTreeMap<String, Vec<ResolvedInitializerCallEffectInput>>,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    callable_summaries: &ResolvedCallableSummaries,
    static_dependencies: &ResolvedStaticDependencies,
) -> Result<()> {
    for inputs in initializer_call_effect_inputs.values() {
        for input in inputs {
            if let Some((module, export, reason)) = known_original_provider_write(
                &input.provider_chunk,
                &input.provider_export,
                modules,
                callable_summaries,
                &mut BTreeSet::new(),
            ) {
                anyhow::bail!(
                    "initializer_call_effect_inputs {}:{} is contradicted by source-known original provider write {module}:{export}: {reason}",
                    input.reference.defining_chunk,
                    input.reference.input_index,
                );
            }
        }
    }
    for (consumer, inputs) in &callable_summaries.imported_primitive_array_spread_inputs {
        for input in inputs {
            for operand in &input.evidence.operands {
                ensure!(
                    resolved_closure_precedes_importer(
                        consumer,
                        &operand.provider_module,
                        &operand.source_specifier,
                        static_dependencies,
                    )
                    .is_some(),
                    "{} requires direct provider {} to have a complete acyclic ordinary static initialization closure before consumer {}",
                    input.evidence.spec_location,
                    operand.provider_module,
                    consumer
                );
            }
        }
    }
    Ok(())
}

fn known_original_provider_write(
    module: &str,
    export: &str,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    known: &ResolvedCallableSummaries,
    visited: &mut BTreeSet<(String, String)>,
) -> Option<(String, String, String)> {
    if !visited.insert((module.to_owned(), export.to_owned())) {
        return None;
    }
    if let Some(reason) = known.known_original_external_write(module, export) {
        return Some((module.to_owned(), export.to_owned(), reason.to_owned()));
    }
    let local = modules.get(module)?.exports.get(export)?;
    let target = modules.get(module)?.imports.get(local)?;
    known_original_provider_write(&target.module, &target.export, modules, known, visited)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{collect_test_program_summary, test_bundle};
    use artifact::reports::ChunkFileRecord;
    use artifact::{ChunkBundle, FileMetadata, FileRole, JsFile, JsFileBody};
    use program_analysis::analyze_program_shallow;
    use spec::ChunkExportPurity;

    fn add_non_entry_file(artifact: &mut ChunkBundle, chunk_name: &str, path: &str, source: &str) {
        let chunk_id = artifact.chunk_table.get(chunk_name).expect("test chunk");
        let chunk = artifact
            .chunks
            .iter_mut()
            .find(|chunk| chunk.chunk_id == chunk_id)
            .expect("test artifact chunk");
        chunk.js.files.push(JsFile {
            path: path.to_string(),
            body: JsFileBody::Source(source.to_string()),
            header_lines: Vec::new(),
            binding_comments: BTreeMap::new(),
            leading_item_comments: BTreeMap::new(),
            metadata: FileMetadata {
                chunk_id: chunk_name.to_string(),
                chunk_file: path.to_string(),
                role: FileRole::Module,
                source_path: format!("{chunk_name}/{path}"),
            },
        });
        chunk.analysis.files.push(ChunkFileRecord {
            file: path.to_string(),
            role: FileRole::Module,
        });
    }

    #[test]
    fn request_only_host_loading_preserves_direct_esm_primitive_facts() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "request-only.js",
                "if (false) import.meta.require('./deferred.js');\nexport const label = `tool_${2}`;",
            )
            .expect("request-only ESM source parses");
            let facts = analyze_program_shallow(&parsed).runtime_facts;

            assert!(facts.has_cjs_interop, "the full runtime guard remains set");
            assert!(
                !facts.has_own_cjs_export_surface,
                "request-only loading does not create a CommonJS export surface"
            );
            assert!(facts.stable_export_values.contains_key("label"));

            let parsed = js_ast::parse_js_module(
                "cjs-surface.js",
                "if (false) import.meta.require('./deferred.js');\ntypeof module;\nexport const label = 'tool';",
            )
            .expect("hybrid module source parses");
            let facts = analyze_program_shallow(&parsed).runtime_facts;
            assert!(facts.has_cjs_interop);
            assert!(facts.has_own_cjs_export_surface);
            assert!(!facts.stable_export_values.contains_key("label"));
        });
    }

    #[test]
    fn imported_read_capability_requires_a_resolved_provider_closure_before_the_importer() {
        js_ast::with_swc_globals(|| {
            for (provider, expected_read) in [
                ("export const limit = 2;", true),
                ("import '../app/app.js'; export const limit = 2;", false),
                ("import './missing.js'; export const limit = 2;", false),
            ] {
                let artifact = test_bundle(&[
                    (
                        "static/app",
                        "import { limit as bytes } from '../peer/peer.js'; export const copied = bytes;",
                    ),
                    ("static/peer", provider),
                ]);
                let facts = collect_test_program_summary(&artifact, &BTreeMap::new());
                let provider_chunk = artifact
                    .chunks
                    .iter()
                    .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == "static/peer")
                    .expect("provider source facts");
                assert!(matches!(
                    provider_chunk.analysis.runtime_facts.stable_export_values.get("limit"),
                    Some(StableExportValue::Local { kinds, .. }) if *kinds == PrimKinds::NUMBER
                ));
                let imported_value = facts.values["static/app"].get("bytes");
                assert_eq!(imported_value.is_some(), expected_read, "{provider}");
                if let Some(imported_value) = imported_value {
                    assert_eq!(*imported_value, Value::Prim(PrimKinds::NUMBER));
                }
                let read = facts.primitive_reads["static/app"].get("bytes");
                assert_eq!(read.is_some(), expected_read, "{provider}");
                if !expected_read {
                    assert!(facts.values["static/app"].is_empty(), "{provider}");
                    assert!(facts.primitive_reads["static/app"].is_empty(), "{provider}");
                }
                if let Some(read) = read {
                    assert_eq!(
                        read.dependency_closure,
                        BTreeSet::from(["static/peer".to_string()])
                    );
                    assert_eq!(
                        read.source_cells,
                        BTreeSet::from([PrimitiveExportCell {
                            module: "static/peer".to_string(),
                            export: "limit".to_string(),
                        }])
                    );
                }
            }
        });
    }

    #[test]
    fn primitive_import_facts_do_not_project_through_non_entry_files() {
        js_ast::with_swc_globals(|| {
            let mut artifact = test_bundle(&[
                (
                    "static/app",
                    "import { value as alias } from '../peer/internal.js';\n\
                     export const copied = alias;\n\
                     export { value as forwarded } from '../peer/internal.js';",
                ),
                ("static/peer", "export const value = 4;"),
            ]);
            add_non_entry_file(
                &mut artifact,
                "static/peer",
                "internal.js",
                "export const value = {};",
            );
            let facts = collect_test_program_summary(&artifact, &BTreeMap::new());

            let app_values = facts
                .values
                .get("static/app")
                .expect("app primitive fact set");
            assert!(!app_values.contains_key("copied"));
            assert!(!app_values.contains_key("forwarded"));
        });
    }

    #[test]
    fn non_entry_synchronous_callback_export_does_not_inherit_entry_summaries() {
        js_ast::with_swc_globals(|| {
            let mut artifact = test_bundle(&[
                (
                    "static/app",
                    "import { factory } from '../peer/unsafe.js';\n\
                     let later = 0;\n\
                     const callback = () => later;\n\
                     const result = factory(callback);\n\
                     later = 42;\n\
                     console.log(result(), later);",
                ),
                (
                    "static/peer",
                    "export function factory(callback) { return () => callback(); }",
                ),
            ]);
            add_non_entry_file(
                &mut artifact,
                "static/peer",
                "unsafe.js",
                "export function factory(callback) { callback(); return () => undefined; }",
            );
            let mut assertion = ChunkExportPurity::default();
            assertion.pure_exports.insert("factory".to_string());
            assertion
                .callback_factory_exports
                .insert("factory".to_string());
            let assertions = BTreeMap::from([("static/peer".to_string(), assertion)]);
            let facts = collect_test_program_summary(&artifact, &assertions);

            assert!(
                facts
                    .bindings
                    .get("static/app")
                    .is_none_or(|bindings| !bindings.contains_key("factory"))
            );
            assert!(
                facts
                    .imported_callables
                    .get("static/app")
                    .is_none_or(|bindings| !bindings.contains_key("factory"))
            );
        });
    }
}
