//! Program-wide summaries consumed by per-chunk source analysis.
//!
//! This stage resolves each chunk's imports against the artifact, gathers
//! source-resolved inputs, and calls the cross-module purity analysis
//! (`analysis::cross_module_purity`). Its results feed each chunk's
//! `AnalysisInputs`, including `imported_purities` for imported calls.

use analysis_model::{PrimKinds, StableExportValue};
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis::analysis_inputs::{
    ArgumentInputEvidence, ArgumentInputRef, CallableSummary, ImportedPrimitiveRead,
    NativeInputKind, NativeIntegrity, NativeOperationInputEvidence, NativeOperationInputRef,
    NativeOperationSelectorProvenance, NoAccessorInstallRef, PrimitiveExportCell,
    ResolvedArgumentInput, ResolvedBindingValueInput, ResolvedNativeOperationInput,
    ResolvedNoAccessorInstall, SelectedBindingValueInput, SelectedNativeOperationInput,
    ValueInputEvidence, ValueInputRef,
};
use analysis::cross_module_purity::{
    CallableSummaryInputs, ExportValueSource, ModulePurityFacts, ResolvedImport,
    resolve_asserted_callback_factory_members, resolve_asserted_fluent_bindings,
    resolve_asserted_inline_callback_factories, resolve_asserted_member_purities,
    resolve_asserted_namespace_independent_members, resolve_asserted_no_reentry_bindings,
    resolve_asserted_no_reentry_callbacks, resolve_asserted_no_reentry_members,
    resolve_asserted_no_reentry_record_members, resolve_asserted_pure_new_bindings,
    resolve_asserted_retained_callback_constructors, resolve_asserted_retained_callback_factories,
    resolve_asserted_retained_callback_factory_members, resolve_export_primitives,
    resolve_imported_callable_summaries, resolve_imported_purities,
};
use analysis::facts::abstract_domain::value::Value;
use analysis::purity::Purity;
use anyhow::{Context, Result, bail, ensure};
use artifact::reports::{
    ExportAliasRecord, ImportRecord, ImportSpecifierKind, StaticModuleRequest,
    StaticModuleRequestMode,
};
use artifact::{ArtifactIndexes, ChunkBundle};
use graph_provenance::GraphProvenance;
use js_ast::ParsedJsModule;
use program_analysis::analyze_program_shallow;
use swc_common::{Span, Spanned};
use swc_ecma_ast::{
    ArrowExpr, CallExpr, Callee, Constructor, Decl, Function, GetterProp, MemberProp, Module,
    ModuleDecl, ModuleItem, Pat, SetterProp,
};
use swc_ecma_visit::{Visit, VisitWith};

use analysis_projection::{
    AnalysisNativeOperationInput, AnalysisProjection, AnalysisSourceSites, ChunkAnalysisInputs,
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

/// Parsed entry + import/export records for a chunk whose entry the artifact
/// kept as raw source (non-selected chunks). The program-summary pass parses
/// these itself so every listed chunk participates in analysis, not just the
/// selected ones.
pub struct ReparsedEntry {
    pub parsed: ParsedJsModule,
    imports: Vec<ImportRecord>,
    export_aliases: Vec<ExportAliasRecord>,
    stable_export_values: BTreeMap<String, StableExportValue>,
    static_requests: Vec<StaticModuleRequest>,
}

/// Program-wide analysis summaries, keyed by source chunk name.
pub struct ProgramSummary {
    /// Per-chunk imported-binding verdicts (`AnalysisInputs::imported_purities`).
    pub bindings: BTreeMap<String, BTreeMap<String, Arc<Purity>>>,
    /// Per-chunk imported-binding values (`AnalysisInputs::imported_values`).
    pub values: BTreeMap<String, BTreeMap<String, Value>>,
    /// Per-chunk imported primitive read capabilities with source-cell
    /// provenance and an acyclic, fully resolved provider dependency closure.
    pub primitive_reads: BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>>,
    /// Per-chunk pure-member sets for imported namespace-like bindings,
    /// merged into `AnalysisAssumptions::declared_pure_members`.
    pub members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    /// Per-chunk fluent-trusted import binding names (deep-purity roots),
    /// merged into `AnalysisAssumptions::fluent_bindings`.
    pub fluent: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk local names whose imported exports have an asserted pure
    /// construction contract (`chunk_export_purity.pure_new_exports`).
    pub constructors: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk bare imported callable summaries with their defining evidence.
    pub imported_callables: BTreeMap<String, BTreeMap<String, CallableSummary>>,
    /// Source-derived native mutation census shared by every analyzed chunk.
    pub native_integrity: Option<NativeIntegrity>,
    /// The whole-program bundle-owned globals (SPEC.md § Bundle-owned globals).
    pub bundle_globals: analysis::analysis_inputs::BundleGlobalNames,
    /// Source-resolved author inputs for the provider's own local analysis.
    pub value_inputs: BTreeMap<String, Vec<ResolvedBindingValueInput>>,
    /// Source-resolved native operation premises for the provider initializer.
    pub native_operation_inputs: BTreeMap<String, Vec<ResolvedNativeOperationInput>>,
    pub stable_set_spreads:
        BTreeMap<String, BTreeMap<u32, analysis::analysis_inputs::StableSetIterableUse>>,
    pub inferred_set_constructions:
        BTreeMap<String, BTreeMap<u32, analysis::analysis_inputs::InferredSetConstruction>>,
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
    site_chunks.extend(source_sites.argument_inputs.keys());
    site_chunks.extend(source_sites.no_accessor_install_callables.keys());
    site_chunks.extend(native_operation_inputs.keys());
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
    /// Export name → replacement package of every vendor-swapped export, by
    /// defining chunk; summaries derived from those bodies name the package.
    pub replacement_packages: &'a BTreeMap<ChunkId, BTreeMap<String, String>>,
}

/// Re-parse the entries stored as raw source so every chunk's body is
/// analyzable by the program-summary pass. Parse failures only warn: an
/// unparseable chunk degrades to conservative behavior rather than failing the
/// build. The caller keeps the parses until it has numbered the hygiene
/// contexts they introduce in an analysis artifact.
pub fn reparse_unretained_entries(
    artifact: &ChunkBundle,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> BTreeMap<ChunkId, ReparsedEntry> {
    let mut reparsed: BTreeMap<ChunkId, ReparsedEntry> = BTreeMap::new();
    for chunk_artifact in &artifact.chunks {
        if !chunk_allowed(chunk_artifact.chunk_id, chunk_filter) {
            continue;
        }
        let entry_file = &chunk_artifact.analysis.entry_file;
        let Some(file) = chunk_artifact.js.get_file(entry_file) else {
            continue;
        };
        if file.ast().is_some() {
            continue;
        }
        let Some(source) = file.source() else {
            continue;
        };
        let chunk_name = artifact.chunk_table.name(chunk_artifact.chunk_id);
        match js_ast::parse_js_module(chunk_name, source) {
            Ok(parsed) => {
                let analysis = analyze_program_shallow(&parsed);
                reparsed.insert(
                    chunk_artifact.chunk_id,
                    ReparsedEntry {
                        parsed,
                        imports: analysis.imports,
                        export_aliases: analysis.export_aliases,
                        stable_export_values: analysis.runtime_facts.stable_export_values,
                        static_requests: analysis.runtime_facts.static_requests,
                    },
                );
            }
            Err(error) => {
                eprintln!(
                    "cross_module_purity: chunk {chunk_name} entry failed to parse; \
                     leaving it opaque to the purity oracle: {error:#}"
                );
            }
        }
    }

    reparsed
}

pub fn collect_program_summary(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    projection: &AnalysisProjection<'_>,
    options: ProgramSummaryOptions<'_>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    reparsed: &BTreeMap<ChunkId, ReparsedEntry>,
) -> Result<ProgramSummary> {
    let ProgramSummaryOptions {
        graph_provenance,
        replacement_packages,
    } = options;
    let chunk_export_purity = projection.chunk_export_purity;
    let chunk_analysis_inputs = projection.chunk_analysis_inputs.as_ref();
    let predefined_globals = projection.host_behavior.predefined_globals;
    let declared_pure_new_bindings = &projection.declared_pure_new();
    // Pass 2: assemble each chunk's oracle inputs from whichever body is
    // available (retained AST or re-parsed entry).
    let mut modules = BTreeMap::new();
    let mut local_export_values =
        BTreeMap::<(String, String), (PrimKinds, BTreeSet<String>)>::new();
    let mut export_value_sources = BTreeMap::<(String, String), ExportValueSource>::new();
    let mut static_requests =
        BTreeMap::<String, (ChunkId, String, Vec<StaticModuleRequest>)>::new();
    for chunk_artifact in &artifact.chunks {
        if !chunk_allowed(chunk_artifact.chunk_id, chunk_filter) {
            continue;
        }
        let chunk_name = artifact.chunk_table.name(chunk_artifact.chunk_id);
        let entry_file = &chunk_artifact.analysis.entry_file;
        let retained_ast = chunk_artifact
            .js
            .get_file(entry_file)
            .and_then(|file| file.ast());
        let (parsed, import_records, export_alias_records) =
            match (retained_ast, reparsed.get(&chunk_artifact.chunk_id)) {
                (Some(ast), _) => (
                    ast,
                    chunk_artifact.analysis.imports.as_slice(),
                    chunk_artifact.analysis.export_aliases.as_slice(),
                ),
                (None, Some(entry)) => (
                    &entry.parsed,
                    entry.imports.as_slice(),
                    entry.export_aliases.as_slice(),
                ),
                (None, None) => continue,
            };
        let stable_export_values = match reparsed.get(&chunk_artifact.chunk_id) {
            Some(entry) => &entry.stable_export_values,
            None => &chunk_artifact.analysis.runtime_facts.stable_export_values,
        };
        let module_static_requests = match reparsed.get(&chunk_artifact.chunk_id) {
            Some(entry) => &entry.static_requests,
            None => &chunk_artifact.analysis.runtime_facts.static_requests,
        };
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
    let export_primitives = resolve_export_primitives(export_value_sources);
    let export_values: BTreeMap<_, _> = export_primitives
        .iter()
        .map(|(export, primitive)| (export.clone(), Value::Prim(primitive.kinds)))
        .collect();
    let static_dependencies = resolved_static_dependency_graph(
        artifact,
        indexes,
        &static_requests,
        &modules.keys().cloned().collect(),
    );
    let primitive_reads = modules
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
                        &static_dependencies,
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
        .collect();
    validate_analysis_source_sites(
        artifact,
        &projection.source_sites,
        &projection.native_operation_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let value_inputs = resolve_value_inputs(
        artifact,
        &modules,
        chunk_analysis_inputs,
        &projection.source_sites.value_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let native_operation_inputs = resolve_native_operation_inputs(
        artifact,
        &modules,
        chunk_analysis_inputs,
        graph_provenance,
        chunk_filter,
        &projection.native_operation_inputs,
        &projection.source_sites,
    )?;
    let argument_inputs = resolve_argument_inputs(
        artifact,
        &modules,
        chunk_analysis_inputs,
        &projection.source_sites.argument_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let no_accessor_install_callables = resolve_no_accessor_install_callables(
        artifact,
        &modules,
        chunk_analysis_inputs,
        &projection.source_sites.no_accessor_install_callables,
        chunk_filter,
    )?;
    let asserted_pure: BTreeMap<String, BTreeSet<String>> = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.pure_exports.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.pure_exports.clone()))
        .collect();
    let asserted_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>> =
        chunk_export_purity
            .iter()
            .filter(|(_, assertion)| !assertion.pure_members.is_empty())
            .filter(|(chunk, _)| modules.contains_key(*chunk))
            .map(|(chunk, assertion)| (chunk.clone(), assertion.pure_members.clone()))
            .collect();
    let asserted_fluent: BTreeMap<String, BTreeSet<String>> = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.fluent_exports.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.fluent_exports.clone()))
        .collect();
    let asserted_callback_factories: BTreeMap<String, BTreeSet<String>> = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.callback_factory_exports.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.callback_factory_exports.clone()))
        .collect();
    let asserted_inline_callback_factories: BTreeMap<String, BTreeSet<String>> =
        chunk_export_purity
            .iter()
            .filter(|(_, assertion)| !assertion.inline_callback_factory_exports.is_empty())
            .filter(|(chunk, _)| modules.contains_key(*chunk))
            .map(|(chunk, assertion)| {
                (
                    chunk.clone(),
                    assertion.inline_callback_factory_exports.clone(),
                )
            })
            .collect();
    let asserted_callback_factory_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>> =
        chunk_export_purity
            .iter()
            .filter(|(_, assertion)| !assertion.callback_factory_members.is_empty())
            .filter(|(chunk, _)| modules.contains_key(*chunk))
            .map(|(chunk, assertion)| (chunk.clone(), assertion.callback_factory_members.clone()))
            .collect();
    let asserted_no_reentry_members = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.no_reentry_members.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.no_reentry_members.clone()))
        .collect();
    let asserted_no_reentry_record_members = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.no_reentry_record_members.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.no_reentry_record_members.clone()))
        .collect();
    let asserted_no_reentry_exports = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.no_reentry_exports.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.no_reentry_exports.clone()))
        .collect();
    let asserted_namespace_independent_members = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.namespace_independent_members.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| {
            (
                chunk.clone(),
                assertion.namespace_independent_members.clone(),
            )
        })
        .collect();
    let mut asserted_pure_new: BTreeMap<String, BTreeSet<String>> = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.pure_new_exports.is_empty())
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), assertion.pure_new_exports.clone()))
        .collect();
    // A structurally selected member's trusted constructor contract follows
    // every export of that resolved binding. The importer sees its local
    // binding name; neither spec nor analysis needs the minified export name.
    for (chunk, bindings) in declared_pure_new_bindings {
        let Some(facts) = modules.get(chunk) else {
            continue;
        };
        for (export, local) in &facts.exports {
            if bindings.contains(local) {
                asserted_pure_new
                    .entry(chunk.clone())
                    .or_default()
                    .insert(export.clone());
            }
        }
    }
    let mut callable_summaries = resolve_imported_callable_summaries(
        &modules,
        &asserted_callback_factories,
        &chunk_export_purity
            .iter()
            .map(|(chunk, assertion)| {
                (
                    chunk.clone(),
                    assertion.descriptor_copy_factory_exports.clone(),
                )
            })
            .collect(),
        CallableSummaryInputs {
            selected_value_inputs: &value_inputs,
            selected_native_operation_inputs: &native_operation_inputs,
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
    )?;
    Ok(ProgramSummary {
        no_reentry_callback_exports: resolve_asserted_no_reentry_callbacks(
            &modules,
            &chunk_export_purity
                .iter()
                .map(|(chunk, assertion)| {
                    (chunk.clone(), assertion.no_reentry_callback_exports.clone())
                })
                .collect(),
        ),
        retained_callback_constructors: resolve_asserted_retained_callback_constructors(
            &modules,
            &chunk_export_purity
                .iter()
                .map(|(chunk, assertion)| {
                    (
                        chunk.clone(),
                        assertion.retained_callback_constructor_exports.clone(),
                    )
                })
                .collect(),
        ),
        retained_callback_factories: resolve_asserted_retained_callback_factories(
            &modules,
            &chunk_export_purity
                .iter()
                .map(|(chunk, assertion)| {
                    (
                        chunk.clone(),
                        assertion.retained_callback_factory_exports.clone(),
                    )
                })
                .collect(),
        ),
        retained_callback_factory_members: resolve_asserted_retained_callback_factory_members(
            &modules,
            &chunk_export_purity
                .iter()
                .map(|(chunk, assertion)| {
                    (
                        chunk.clone(),
                        assertion.retained_callback_factory_members.clone(),
                    )
                })
                .collect(),
        ),
        values: modules
            .iter()
            .map(|(name, module)| (name.clone(), module.imported_values(&export_values)))
            .collect(),
        primitive_reads,
        bindings: resolve_imported_purities(&modules, &asserted_pure, &mut callable_summaries),
        members: resolve_asserted_member_purities(&modules, &asserted_members),
        fluent: resolve_asserted_fluent_bindings(&modules, &asserted_fluent),
        constructors: resolve_asserted_pure_new_bindings(&modules, &asserted_pure_new),
        imported_callables: callable_summaries.imported_callables,
        native_integrity: callable_summaries.native_integrity,
        bundle_globals: callable_summaries.bundle_globals,
        value_inputs: callable_summaries.value_inputs,
        native_operation_inputs: callable_summaries.native_operation_inputs,
        stable_set_spreads: callable_summaries.stable_set_spreads,
        inferred_set_constructions: callable_summaries.inferred_set_constructions,
        argument_inputs: callable_summaries.argument_inputs,
        inline_callback_factories: resolve_asserted_inline_callback_factories(
            &modules,
            &asserted_inline_callback_factories,
        ),
        namespace_independent_members: resolve_asserted_namespace_independent_members(
            &modules,
            &asserted_namespace_independent_members,
        ),
        callback_factory_members: resolve_asserted_callback_factory_members(
            &modules,
            &asserted_callback_factory_members,
        ),
        no_reentry_members: resolve_asserted_no_reentry_members(
            &modules,
            &asserted_no_reentry_members,
        ),
        no_reentry_record_members: resolve_asserted_no_reentry_record_members(
            &modules,
            &asserted_no_reentry_record_members,
        ),
        no_reentry_exports: resolve_asserted_no_reentry_bindings(
            &modules,
            &asserted_no_reentry_exports,
        ),
    })
}

fn resolve_value_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    selected_sites: &BTreeMap<String, Vec<analysis::analysis_inputs::SourceDeclaration>>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<SelectedBindingValueInput>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.value_inputs.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.value_inputs");
        let chunk = artifact
            .chunks
            .iter()
            .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == chunk_name)
            .with_context(|| {
                format!(
                    "chunk_analysis_options.{chunk_name}.value_inputs names no loaded provider chunk"
                )
            })?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "chunk_analysis_options.{chunk_name}.value_inputs provider is outside the existing analyzed import closure"
        );
        let facts = modules.get(chunk_name).with_context(|| {
            format!(
                "chunk_analysis_options.{chunk_name}.value_inputs provider entry is unavailable to whole-program analysis"
            )
        })?;
        let entry_file = &chunk.analysis.entry_file;
        ensure!(
            chunk.js.get_file(entry_file).is_some(),
            "provider {chunk_name} is missing its analyzed entry file {entry_file}"
        );
        let provenance = graph_provenance
            .context("captured value inputs require explicit graph parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| {
                format!(
                    "chunk_analysis_options.{chunk_name}.value_inputs requires explicit graph parser-input provenance"
                )
            })?;
        let declarations = selected_sites
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no frontend-resolved source sites"))?;
        ensure!(
            declarations.len() == options.value_inputs.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let mut chunk_inputs = Vec::with_capacity(options.value_inputs.len());
        for (input_index, (contract, declaration)) in
            options.value_inputs.iter().zip(declarations).enumerate()
        {
            ensure!(
                contract.audited_source_sha256.len() == 64
                    && contract
                        .audited_source_sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit()),
                "chunk_analysis_options.{chunk_name}.value_inputs[{input_index}].audited_source_sha256 must be a 64-character SHA-256 digest"
            );
            ensure!(
                contract.audited_source_sha256 == source.sha256,
                "chunk_analysis_options.{chunk_name}.value_inputs[{input_index}] has stale audited_source_sha256: expected parser input {}, found {}",
                source.sha256,
                contract.audited_source_sha256
            );
            let request_id =
                format!("chunk_analysis_options.{chunk_name}.value_inputs[{input_index}]");
            let body_index = declaration.body_index;
            let selected_binding = declaration.binding.clone();
            if let Some(export_name) = &contract.expected_export {
                ensure!(
                    facts
                        .exports
                        .get(export_name)
                        .is_some_and(|resolved_local| resolved_local == &selected_binding)
                        && !facts.imports.contains_key(&selected_binding),
                    "{request_id}.expected_export `{export_name}` does not resolve directly to selected local `{selected_binding}` in provider {chunk_name}"
                );
            }
            let selected_statement_span = facts
                .body
                .get(body_index)
                .with_context(|| {
                    format!("{request_id}.source_match selected a missing module item")
                })?
                .span();
            let reference = ValueInputRef {
                defining_chunk: chunk_name.clone(),
                input_index,
            };
            let evidence = ValueInputEvidence {
                reference,
                spec_location: request_id,
                source_match: contract.source_match.clone(),
                value_kind: contract.value.kind,
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: None,
                audited_source_sha256: contract.audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                declaration_location: None,
                initializer_location: None,
                expected_export: contract.expected_export.clone(),
                note: None,
            };
            chunk_inputs.push(SelectedBindingValueInput {
                evidence,
                selected_binding,
                selected_statement_span,
            });
        }
        resolved.insert(chunk_name.clone(), chunk_inputs);
    }
    Ok(resolved)
}

fn resolve_native_operation_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    projected_inputs: &BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
    source_sites: &analysis_projection::AnalysisSourceSites,
) -> Result<BTreeMap<String, Vec<SelectedNativeOperationInput>>> {
    let mut resolved = BTreeMap::new();
    // Native operation inputs have their own projection, so their chunks may
    // not have a source-analysis entry.
    let chunk_names: BTreeSet<&String> = options.keys().chain(projected_inputs.keys()).collect();
    for chunk_name in chunk_names {
        let analysis_options = options.get(chunk_name);
        let native_call_identity_inputs = analysis_options.map_or(&[][..], |options| {
            options.native_call_identity_inputs.as_slice()
        });
        let operation_inputs = projected_inputs
            .get(chunk_name)
            .map_or(&[][..], Vec::as_slice);
        if operation_inputs.is_empty() && native_call_identity_inputs.is_empty() {
            continue;
        }
        let chunk = artifact
            .chunks
            .iter()
            .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == chunk_name)
            .with_context(|| {
                format!(
                    "chunk_analysis_options.{chunk_name}.native_operation_inputs names no loaded provider chunk"
                )
            })?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "chunk_analysis_options.{chunk_name}.native_operation_inputs provider is outside the existing analyzed import closure"
        );
        let facts = modules.get(chunk_name).with_context(|| {
            format!(
                "chunk_analysis_options.{chunk_name}.native_operation_inputs provider entry is unavailable to whole-program analysis"
            )
        })?;
        let entry_file = &chunk.analysis.entry_file;
        ensure!(
            chunk.js.get_file(entry_file).is_some(),
            "provider {chunk_name} is missing its analyzed entry file {entry_file}"
        );
        let provenance = graph_provenance
            .context("native operation inputs require explicit graph parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| {
                format!(
                    "chunk_analysis_options.{chunk_name}.native_operation_inputs requires explicit graph parser-input provenance"
                )
            })?;
        let call_identity_sites = source_sites
            .native_call_identity_inputs
            .get(chunk_name)
            .map_or(&[][..], Vec::as_slice);
        ensure!(
            call_identity_sites.len() == native_call_identity_inputs.len(),
            "chunk_analysis_options.{chunk_name}.native_call_identity_inputs source-site count does not match authored inputs"
        );
        let contracts = operation_inputs
            .iter()
            .enumerate()
            .map(|(index, contract)| {
                (
                    index,
                    format!("chunk_analysis_options.{chunk_name}.native_operation_inputs[{index}]"),
                    contract.declaration.clone(),
                    contract.selector.clone(),
                    &contract.audited_source_sha256,
                    NativeInputKind::Operation(contract.operation),
                )
            })
            .chain(native_call_identity_inputs.iter().enumerate().map(
                |(index, contract)| {
                    (
                        operation_inputs.len() + index,
                        format!("chunk_analysis_options.{chunk_name}.native_call_identity_inputs[{index}]"),
                        call_identity_sites[index].clone(),
                        NativeOperationSelectorProvenance::SourceMatch(contract.source_match.clone()),
                        &contract.audited_source_sha256,
                        NativeInputKind::CallIdentity(contract.callee),
                    )
                },
            ));
        let mut chunk_inputs =
            Vec::with_capacity(operation_inputs.len() + native_call_identity_inputs.len());
        for (
            input_index,
            request_id,
            declaration,
            selector_provenance,
            audited_source_sha256,
            operation,
        ) in contracts
        {
            ensure!(
                audited_source_sha256.len() == 64
                    && audited_source_sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit()),
                "{request_id}.audited_source_sha256 must be a 64-character SHA-256 digest"
            );
            ensure!(
                audited_source_sha256 == &source.sha256,
                "{request_id} has stale audited_source_sha256: expected parser input {}, found {}",
                source.sha256,
                audited_source_sha256
            );
            let body_index = declaration.body_index;
            let selected_binding = declaration.binding;
            let selected_statement_span = facts
                .body
                .get(body_index)
                .with_context(|| {
                    format!("{request_id}.source_match selected a missing module item")
                })?
                .span();
            let reference = NativeOperationInputRef {
                defining_chunk: chunk_name.clone(),
                input_index,
            };
            let evidence = NativeOperationInputEvidence {
                reference,
                spec_location: request_id,
                selector: selector_provenance,
                operation,
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: None,
                audited_source_sha256: audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                declaration_location: None,
                initializer_location: None,
                note: None,
            };
            chunk_inputs.push(SelectedNativeOperationInput {
                evidence,
                selected_binding,
                selected_statement_span,
            });
        }
        resolved.insert(chunk_name.clone(), chunk_inputs);
    }
    Ok(resolved)
}

fn resolve_argument_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    selected_sites: &BTreeMap<String, Vec<analysis::analysis_inputs::SourceDeclaration>>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<ResolvedArgumentInput>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.argument_inputs.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.argument_inputs");
        let chunk = artifact
            .chunks
            .iter()
            .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == chunk_name)
            .with_context(|| format!("{prefix} names no loaded provider chunk"))?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "{prefix} cannot target an outside-closure provider"
        );
        let facts = modules
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no analyzed provider"))?;
        ensure!(
            chunk.js.get_file(&chunk.analysis.entry_file).is_some(),
            "{prefix} has no parser entry file"
        );
        let provenance =
            graph_provenance.context("argument inputs require graph parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|entry| entry.chunk_id == *chunk_name)
            .with_context(|| format!("{prefix} has no parser-input provenance"))?;
        let declarations = selected_sites
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no frontend-resolved source sites"))?;
        ensure!(
            declarations.len() == options.argument_inputs.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let module = Module {
            span: Default::default(),
            body: facts.body.to_vec(),
            shebang: None,
        };
        let source_facts = analysis::facts::collect_chunk_source_facts(&module, None, |_| None);
        let source_contradictions = source_facts.per_statement.clone();
        let mut selected = Vec::new();
        for (input_index, (contract, declaration)) in
            options.argument_inputs.iter().zip(declarations).enumerate()
        {
            let request_id = format!("{prefix}[{input_index}]");
            ensure!(
                contract.audited_source_sha256.len() == 64
                    && contract
                        .audited_source_sha256
                        .bytes()
                        .all(|b| b.is_ascii_hexdigit()),
                "{request_id}.audited_source_sha256 must be a 64-character SHA-256 digest"
            );
            ensure!(
                contract.audited_source_sha256 == source.sha256,
                "{request_id} has stale audited_source_sha256: expected parser input {}, found {}",
                source.sha256,
                contract.audited_source_sha256
            );
            ensure!(
                contract.parameter_index < contract.exact_arity,
                "{request_id} parameter_index exceeds exact_arity"
            );
            let body_index = declaration.body_index;
            let selected_binding = declaration.binding.clone();
            let item = facts
                .body
                .get(body_index)
                .context("selected source item is missing")?;
            let function = match item {
                ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(Decl::Fn(function))) => function,
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    let Decl::Fn(function) = &export.decl else {
                        bail!("{request_id} requires a direct function declaration");
                    };
                    function
                }
                _ => bail!("{request_id} requires a direct function declaration"),
            };
            ensure!(
                function.ident.sym.as_ref() == selected_binding
                    && function.function.params.len() == contract.exact_arity
                    && function
                        .function
                        .params
                        .iter()
                        .all(|parameter| matches!(parameter.pat, Pat::Ident(_)))
                    && !function.function.is_async
                    && !function.function.is_generator
                    && function.function.body.is_some(),
                "{request_id} requires the selected stable synchronous function with exact arity"
            );
            let Pat::Ident(parameter) = &function.function.params[contract.parameter_index].pat
            else {
                bail!("{request_id} requires a simple positional parameter");
            };
            let source_facts = source_contradictions
                .get(body_index)
                .context("selected source statement facts are missing")?;
            let contradictions = source_facts.contradicts_argument_borrow(
                &function.ident.to_id(),
                &parameter.id.to_id(),
                contract.parameter_index,
            );
            ensure!(
                !contradictions,
                "{request_id} contradicts a visible return, throw, capture, store, or mutation of its parameter"
            );
            let evidence = ArgumentInputEvidence {
                reference: ArgumentInputRef {
                    defining_chunk: chunk_name.clone(),
                    input_index,
                },
                spec_location: request_id.clone(),
                source_match: contract.source_match.clone(),
                source_path: source.source_path.to_string_lossy().into_owned(),
                audited_source_sha256: contract.audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                parameter_index: contract.parameter_index,
                exact_arity: contract.exact_arity,
                declaration_location: None,
            };
            selected.push(ResolvedArgumentInput {
                evidence,
                binding: function.ident.to_id(),
                function_start: function.function.span.lo.0,
            });
        }
        resolved.insert(chunk_name.clone(), selected);
    }
    Ok(resolved)
}

fn resolve_no_accessor_install_callables(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    selected_sites: &BTreeMap<String, Vec<analysis::analysis_inputs::SourceDeclaration>>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<ResolvedNoAccessorInstall>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.no_accessor_install_callables.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.no_accessor_install_callables");
        let chunk = artifact
            .chunks
            .iter()
            .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == chunk_name)
            .with_context(|| format!("{prefix} names no loaded provider chunk"))?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "{prefix} cannot target an outside-closure provider"
        );
        let facts = modules
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no analyzed provider"))?;
        let declarations = selected_sites
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no frontend-resolved source sites"))?;
        ensure!(
            declarations.len() == options.no_accessor_install_callables.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let mut selected = Vec::new();
        for (input_index, declaration) in declarations.iter().enumerate() {
            let request_id = format!("{prefix}[{input_index}]");
            let mut callables = SelectedCallables::default();
            facts
                .body
                .get(declaration.body_index)
                .context("selected source item is missing")?
                .visit_with(&mut callables);
            let [function_span] = callables.outermost[..] else {
                bail!(
                    "{request_id} must select a statement containing exactly one outermost function, found {}",
                    callables.outermost.len()
                );
            };
            ensure!(
                callables.computed_method_call,
                "{request_id} selects a function with no computed-key method call"
            );
            selected.push(ResolvedNoAccessorInstall {
                reference: NoAccessorInstallRef {
                    defining_chunk: chunk_name.clone(),
                    input_index,
                },
                function_span,
            });
        }
        resolved.insert(chunk_name.clone(), selected);
    }
    Ok(resolved)
}

/// The outermost functions of one selected statement, accessors included;
/// a nested function is part of the one enclosing it.
#[derive(Default)]
struct SelectedCallables {
    outermost: Vec<Span>,
    depth: usize,
    computed_method_call: bool,
}

impl SelectedCallables {
    fn enclose(&mut self, span: Span, visit: impl FnOnce(&mut Self)) {
        if self.depth == 0 {
            self.outermost.push(span);
        }
        self.depth += 1;
        visit(self);
        self.depth -= 1;
    }
}

impl Visit for SelectedCallables {
    fn visit_function(&mut self, function: &Function) {
        self.enclose(function.span, |this| function.visit_children_with(this));
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.enclose(arrow.span, |this| arrow.visit_children_with(this));
    }

    fn visit_constructor(&mut self, constructor: &Constructor) {
        self.enclose(constructor.span, |this| {
            constructor.visit_children_with(this)
        });
    }

    fn visit_getter_prop(&mut self, getter: &GetterProp) {
        self.enclose(getter.span, |this| getter.visit_children_with(this));
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        self.enclose(setter.span, |this| setter.visit_children_with(this));
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Some(member) = callee.as_member()
            && matches!(member.prop, MemberProp::Computed(_))
        {
            self.computed_method_call = true;
        }
        call.visit_children_with(self);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::reports::{ChunkAnalysisReport, ChunkFileRecord};
    use artifact::{
        ChunkArtifact, ChunkBundle, ChunkMetadata, FileMetadata, FileRole, JsChunk, JsFile,
        JsFileBody,
    };
    use source_identity::ChunkTable;
    use spec::ChunkExportPurity;

    fn test_bundle(chunks: &[(&str, &str)]) -> ChunkBundle {
        js_ast::with_swc_globals(|| {
            let mut chunk_table = ChunkTable::default();
            let mut chunk_artifacts = Vec::new();
            for (chunk_name, source) in chunks {
                let chunk_id = chunk_table.intern((*chunk_name).to_string());
                let entry_file = format!(
                    "{}.js",
                    chunk_name
                        .rsplit('/')
                        .next()
                        .expect("test chunk names are non-empty")
                );
                let source_path = format!("{chunk_name}.js");
                let parsed = js_ast::parse_js_module(&format!("{chunk_name}/{entry_file}"), source)
                    .expect("test source parses");
                let analysis = analyze_program_shallow(&parsed);
                chunk_artifacts.push(ChunkArtifact {
                    chunk_id,
                    js: JsChunk {
                        entry_file: entry_file.clone(),
                        files: vec![JsFile {
                            path: entry_file.clone(),
                            body: JsFileBody::Source((*source).to_string()),
                            header_lines: Vec::new(),
                            binding_comments: BTreeMap::new(),
                            leading_item_comments: BTreeMap::new(),
                            metadata: FileMetadata {
                                chunk_id: (*chunk_name).to_string(),
                                chunk_file: entry_file.clone(),
                                role: FileRole::Entry,
                                source_path: source_path.clone(),
                            },
                        }],
                        metadata: ChunkMetadata {
                            source_path: source_path.clone(),
                        },
                    },
                    analysis: ChunkAnalysisReport {
                        chunk_id: (*chunk_name).to_string(),
                        source_path,
                        entry_file: entry_file.clone(),
                        counts: Default::default(),
                        files: vec![ChunkFileRecord {
                            file: entry_file,
                            role: FileRole::Entry,
                        }],
                        imports: analysis.imports,
                        export_aliases: analysis.export_aliases,
                        unresolved_exports: Vec::new(),
                        kept_top_level_declarations: Vec::new(),
                        runtime_facts: analysis.runtime_facts,
                    },
                });
            }
            ChunkBundle {
                chunks: chunk_artifacts,
                chunk_table,
            }
        })
    }

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
            let indexes = ArtifactIndexes::build(&artifact).expect("build indexes");
            let no_options = BTreeMap::new();
            let no_export_purity = BTreeMap::new();
            let host_behavior = spec::HostBehavior::default();
            let facts = collect_program_summary(
                &artifact,
                &indexes,
                &AnalysisProjection::new(
                    &no_options,
                    &no_export_purity,
                    &host_behavior,
                    None,
                    BTreeMap::new(),
                    analysis_projection::AnalysisSourceSites::default(),
                    BTreeMap::new(),
                )
                .expect("empty projection is valid"),
                ProgramSummaryOptions {
                    graph_provenance: None,
                    replacement_packages: &BTreeMap::new(),
                },
                None,
                &reparse_unretained_entries(&artifact, None),
            )
            .expect("program summary analysis succeeds");

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
            let indexes = ArtifactIndexes::build(&artifact).expect("build indexes");
            let no_options = BTreeMap::new();
            let host_behavior = spec::HostBehavior::default();
            let facts = collect_program_summary(
                &artifact,
                &indexes,
                &AnalysisProjection::new(
                    &no_options,
                    &assertions,
                    &host_behavior,
                    None,
                    BTreeMap::new(),
                    analysis_projection::AnalysisSourceSites::default(),
                    BTreeMap::new(),
                )
                .expect("empty projection is valid"),
                ProgramSummaryOptions {
                    graph_provenance: None,
                    replacement_packages: &BTreeMap::new(),
                },
                None,
                &reparse_unretained_entries(&artifact, None),
            )
            .expect("program summary analysis succeeds");

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
