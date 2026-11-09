//! Program-wide summaries consumed by per-chunk source analysis.
//!
//! This stage resolves each chunk's imports against the artifact, gathers
//! source-resolved inputs, and calls the cross-module purity analysis
//! (`analysis::cross_module_purity`). Its results feed each chunk's
//! `AnalysisInputs`, including `imported_purities` for imported calls.

use analysis_model::{PrimKinds, StableExportValue};
use source_identity::{ChunkId, ModuleCellId};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis::analysis_inputs::{
    AdmittedHostModuleDependency, ArgumentInputEvidence, ArgumentInputRef, CallableSummary,
    EffectiveInitializerCallReplacement, ImportedPrimitiveRead, InitializerCallEffectInputRef,
    NativeInputKind, NativeIntegrity, NativeOperationInputEvidence, NativeOperationInputRef,
    NativeOperationSelectorProvenance, NoAccessorInstallRef, PrimitiveCellInvariant,
    PrimitiveExportCell, ResolvedArgumentInput, ResolvedBindingValueInput,
    ResolvedInitializerCallEffectInput, ResolvedNativeOperationInput, ResolvedNoAccessorInstall,
    ResolvedProviderDependencyClosure, SelectedBindingValueInput, SelectedNativeOperationInput,
    ValueInputEvidence, ValueInputRef,
};
use analysis::cross_module_purity::{
    CallableSummaryInputs, ExportValueSource, ModulePurityFacts, ResolvedCallableSummaries,
    ResolvedImport, resolve_asserted_callback_factory_members, resolve_asserted_fluent_bindings,
    resolve_asserted_inline_callback_factories, resolve_asserted_member_purities,
    resolve_asserted_namespace_independent_members, resolve_asserted_no_reentry_bindings,
    resolve_asserted_no_reentry_callbacks, resolve_asserted_no_reentry_members,
    resolve_asserted_no_reentry_record_members, resolve_asserted_pure_new_bindings,
    resolve_asserted_retained_callback_constructors, resolve_asserted_retained_callback_factories,
    resolve_asserted_retained_callback_factory_members, resolve_export_primitives,
    resolve_imported_callable_summaries, resolve_imported_purities,
};
use analysis::facts::abstract_domain::value::HeapModuleScope;
use analysis::facts::abstract_domain::value::Value;
use analysis::purity::Purity;
use anyhow::{Context, Result, bail, ensure};
use artifact::reports::{
    ExportAliasRecord, ImportRecord, ImportSpecifierKind, StaticModuleRequest,
    StaticModuleRequestMode,
};
use artifact::{ArtifactIndexes, ChunkBundle};
use graph_provenance::{CurrentRoots, GraphProvenance, InputRoot};
use js_ast::{ParsedGlobalUses, ParsedJsModule};
use program_analysis::analyze_program_shallow;
use swc_common::{Span, Spanned};
use swc_ecma_ast::{
    ArrowExpr, CallExpr, Callee, Constructor, Decl, Function, GetterProp, Id, MemberProp, Module,
    ModuleDecl, ModuleItem, Pat, SetterProp,
};
use swc_ecma_visit::{Visit, VisitWith};
use vendor::VendorResolutionPlan;

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

pub(super) fn local_exports(
    body: &[ModuleItem],
    aliases: &[ExportAliasRecord],
) -> BTreeMap<String, String> {
    let mut exports: BTreeMap<_, _> = aliases
        .iter()
        .filter(|alias| alias.source.is_none())
        .filter_map(|alias| {
            alias
                .local
                .clone()
                .map(|local| (alias.exported.clone(), local))
        })
        .collect();
    // The shallow alias inventory excludes exported declarations.
    for item in body {
        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(declaration)) = item {
            for name in exported_decl_names(&declaration.decl) {
                exports.entry(name.clone()).or_insert(name);
            }
        }
    }
    exports
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
    pub provider_programs: analysis::analysis_inputs::FrozenProviderPrograms,
    /// Complete resolver-unbound source inventories from prepared parses for
    /// every source chunk in this summary. Entries are never reconstructed
    /// from the legacy reparse used for other summary facts.
    pub global_uses: BTreeMap<String, Arc<ParsedGlobalUses>>,
    /// Per-chunk imported-binding verdicts (`AnalysisInputs::imported_purities`).
    pub bindings: BTreeMap<String, BTreeMap<String, Arc<Purity>>>,
    /// Per-chunk imported values admitted to ordinary candidate execution.
    /// Each value has a matching `primitive_reads` proof for the same local
    /// binding and primitive kinds; raw exported primitive facts remain in
    /// the source analysis and `export_primitives` closure above.
    pub values: BTreeMap<String, BTreeMap<String, Value>>,
    /// Per-chunk imported primitive read capabilities with source-cell
    /// provenance and a resolved provider closure excluding the importer.
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
    /// A conditional provider transfer remains one record within each callable
    /// summary, so its source, layout, result, effects, and cell obligations
    /// arrive together in the importing `AnalysisInputs`.
    pub imported_callables: BTreeMap<String, BTreeMap<String, CallableSummary>>,
    /// Per-chunk local and imported binding addresses. These identify the defining cell
    /// but do not grant value, initialization, or body authority.
    pub module_cells: BTreeMap<String, BTreeMap<Id, ModuleCellId>>,
    /// Canonical source-only invariant proofs for module-cell post-initialization kinds.
    pub module_cell_invariants: BTreeMap<ModuleCellId, PrimitiveCellInvariant>,
    /// Module cells source-proven initialized before each importer's execution.
    pub source_initialized_module_cells: BTreeMap<String, BTreeSet<ModuleCellId>>,
    /// Host premises used by the source-initialized cell closures. `None`
    /// means a legacy saved summary did not retain this proof; an explicit
    /// empty map is complete evidence with no host leaves.
    pub source_initialized_host_dependencies:
        Option<BTreeMap<String, BTreeSet<AdmittedHostModuleDependency>>>,
    /// Source-derived native mutation census shared by every analyzed chunk.
    pub native_integrity: Option<NativeIntegrity>,
    /// The whole-program bundle-owned globals (SPEC.md § Bundle-owned globals).
    pub bundle_globals: analysis::analysis_inputs::BundleGlobalNames,
    /// Source-resolved author inputs for the provider's own local analysis.
    pub value_inputs: BTreeMap<String, Vec<ResolvedBindingValueInput>>,
    /// Source-resolved native operation premises for the provider initializer.
    pub native_operation_inputs: BTreeMap<String, Vec<ResolvedNativeOperationInput>>,
    pub initializer_call_effect_inputs: BTreeMap<String, Vec<ResolvedInitializerCallEffectInput>>,
    /// Exact-site imported-array lifetime assumptions and source-resolved
    /// provider primitive slots for each selected spread.
    pub imported_primitive_array_spread_inputs:
        BTreeMap<String, Vec<analysis::analysis_inputs::ResolvedImportedPrimitiveArraySpreadInput>>,
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
    admitted_host_modules: BTreeMap<String, BTreeSet<AdmittedHostModuleDependency>>,
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
    let mut admitted_host_modules = BTreeMap::new();
    let mut complete_modules = BTreeSet::new();
    for (module, (chunk, entry_file, module_requests)) in requests {
        let mut module_dependencies = BTreeSet::new();
        let mut module_ordinary_dependencies = BTreeSet::new();
        let mut module_host_dependencies = BTreeSet::new();
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
                    && let Some(host_module) = host_behavior.reentry_free_module(&request.specifier)
                {
                    module_host_dependencies.insert(AdmittedHostModuleDependency {
                        source_module: module.clone(),
                        specifier: request.specifier.clone(),
                        module: host_module,
                        consumed_premise:
                            analysis::analysis_inputs::HostBehaviorKey::reentry_free_module(
                                host_module,
                            ),
                    });
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
        admitted_host_modules.insert(module.clone(), module_host_dependencies);
        if is_complete {
            complete_modules.insert(module.clone());
        }
    }
    ResolvedStaticDependencies {
        targets: dependencies,
        ordinary_requests: ordinary_dependencies,
        admitted_host_modules,
        complete_modules,
    }
}

fn resolved_closure_precedes_importer(
    importer: &str,
    provider: &str,
    source_specifier: &str,
    dependencies: &ResolvedStaticDependencies,
) -> Option<ResolvedProviderDependencyClosure> {
    if !dependencies
        .ordinary_requests
        .get(importer)
        .is_some_and(|requests| {
            requests.contains(&(source_specifier.to_string(), provider.to_string()))
        })
    {
        return None;
    }
    #[derive(Clone, Copy, Eq, PartialEq)]
    enum VisitState {
        Visiting,
        Complete,
    }

    let mut pending = vec![(provider.to_string(), false)];
    let mut states = BTreeMap::<String, VisitState>::new();
    let mut modules = BTreeSet::new();
    let mut admitted_host_modules = BTreeSet::new();
    while let Some((module, leaving)) = pending.pop() {
        if leaving {
            states.insert(module, VisitState::Complete);
            continue;
        }
        if module == importer {
            return None;
        }
        match states.get(&module) {
            Some(VisitState::Visiting) => return None,
            Some(VisitState::Complete) => continue,
            None => {}
        }
        if !dependencies.complete_modules.contains(&module) {
            return None;
        }
        states.insert(module.clone(), VisitState::Visiting);
        modules.insert(module.clone());
        admitted_host_modules.extend(
            dependencies
                .admitted_host_modules
                .get(&module)?
                .iter()
                .cloned(),
        );
        let targets = dependencies.targets.get(&module)?;
        pending.push((module, true));
        pending.extend(targets.iter().rev().cloned().map(|target| (target, false)));
    }
    Some(ResolvedProviderDependencyClosure {
        modules,
        admitted_host_modules,
    })
}

/// Close noninvoking source initialization from dependency leaves. Cycles and
/// dependencies outside the complete ordinary ESM graph never become ready.
/// This proves a source context only; relocation must check the actual output
/// graph independently before consuming the same body effects.
fn source_initialized_module_cells(
    dependencies: &ResolvedStaticDependencies,
    noninvoking: &BTreeSet<String>,
    invariants: &BTreeMap<ModuleCellId, PrimitiveCellInvariant>,
    chunks: &source_identity::ChunkTable,
) -> (
    BTreeMap<String, BTreeSet<ModuleCellId>>,
    BTreeMap<String, BTreeSet<AdmittedHostModuleDependency>>,
) {
    let mut pending = BTreeMap::<&str, usize>::new();
    let mut dependents = BTreeMap::<&str, Vec<&str>>::new();
    for module in noninvoking.intersection(&dependencies.complete_modules) {
        let Some(targets) = dependencies.targets.get(module) else {
            continue;
        };
        pending.insert(module, targets.len());
        for target in targets {
            dependents.entry(target).or_default().push(module);
        }
    }
    let mut ready: Vec<_> = pending
        .iter()
        .filter_map(|(module, count)| (*count == 0).then_some(*module))
        .collect();
    let mut completed = BTreeSet::new();
    while let Some(module) = ready.pop() {
        completed.insert(module);
        for dependent in dependents.get(module).into_iter().flatten() {
            let Some(count) = pending.get_mut(*dependent) else {
                continue;
            };
            *count -= 1;
            if *count == 0 {
                ready.push(*dependent);
            }
        }
    }
    let mut cells_by_module = BTreeMap::<&str, BTreeSet<ModuleCellId>>::new();
    for cell in invariants.keys() {
        if let Some(module) = chunks.try_name(cell.chunk) {
            cells_by_module.entry(module).or_default().insert(*cell);
        }
    }
    let summaries: Vec<_> = dependencies
        .ordinary_requests
        .iter()
        .filter(|(importer, _)| dependencies.complete_modules.contains(*importer))
        .map(|(importer, requests)| {
            let mut cells = BTreeSet::new();
            let mut host_dependencies = BTreeSet::new();
            for (specifier, provider) in requests {
                let Some(closure) =
                    resolved_closure_precedes_importer(importer, provider, specifier, dependencies)
                else {
                    continue;
                };
                if !closure
                    .modules
                    .iter()
                    .all(|module| completed.contains(module.as_str()))
                {
                    continue;
                }
                let closure_cells: BTreeSet<_> = closure
                    .modules
                    .iter()
                    .flat_map(|module| {
                        cells_by_module
                            .get(module.as_str())
                            .into_iter()
                            .flatten()
                            .copied()
                    })
                    .collect();
                if !closure_cells.is_empty() {
                    cells.extend(closure_cells);
                    host_dependencies.extend(closure.admitted_host_modules);
                }
            }
            (importer.clone(), cells, host_dependencies)
        })
        .collect();
    let cells = summaries
        .iter()
        .map(|(importer, cells, _)| (importer.clone(), cells.clone()))
        .collect();
    let host_dependencies = summaries
        .into_iter()
        .map(|(importer, _, dependencies)| (importer, dependencies))
        .collect();
    (cells, host_dependencies)
}

/// Inputs that affect program-summary construction.
pub struct ProgramSummaryOptions<'a> {
    pub graph_provenance: Option<&'a GraphProvenance>,
    pub vendor_plan: Option<&'a VendorResolutionPlan>,
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
        vendor_plan,
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
    let mut global_uses = BTreeMap::new();
    let mut provider_global_uses = BTreeMap::new();
    let mut provider_sources = BTreeMap::new();
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
        // The prepared manifest belongs to the canonical preparation parse.
        // A legacy reparse may provide a body for other summary work, but its
        // freshly allocated resolver contexts are not interchangeable with
        // the canonical manifest's Id keys.
        if retained_ast.is_some() {
            let files = parsed.cm.files();
            let file = &files[0];
            provider_sources.insert(
                chunk_name.to_owned(),
                analysis::analysis_inputs::ProviderProgramSource {
                    module: chunk_name.to_owned(),
                    scope: HeapModuleScope::Chunk(chunk_artifact.chunk_id),
                    sha256: graph_provenance::sha256(file.src.as_bytes()),
                    source_start: file.start_pos.0,
                    source_end: file.end_pos.0,
                },
            );
        }
        let parsed_global_uses = chunk_artifact
            .analysis
            .runtime_facts
            .global_uses
            .clone()
            .with_context(|| {
                format!("parsed source chunk {chunk_name} has no canonical resolver-use inventory")
            })?;
        if retained_ast.is_some() {
            ensure!(
                parsed_global_uses.source_file == parsed.source_file_name(),
                "retained source chunk {chunk_name} resolver-use inventory belongs to a different source-map file"
            );
            provider_global_uses.insert(chunk_name.to_string(), Arc::clone(&parsed_global_uses));
        }
        global_uses.insert(chunk_name.to_string(), parsed_global_uses);
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
        let exports = local_exports(&parsed.module.body, export_alias_records);
        let reexports = export_alias_records
            .iter()
            .filter_map(|alias| {
                let source = alias.source.as_ref()?;
                if !module_static_requests.iter().any(|request| {
                    &request.specifier == source
                        && request.mode == StaticModuleRequestMode::OrdinaryEsm
                }) || module_static_requests.iter().any(|request| {
                    &request.specifier == source
                        && request.mode != StaticModuleRequestMode::OrdinaryEsm
                }) {
                    return None;
                }
                let target = indexes.resolve_runtime_import_reference(
                    source,
                    chunk_artifact.chunk_id,
                    entry_file,
                    &artifact.chunk_table,
                )?;
                if !chunk_allowed(target.target_chunk_id, chunk_filter)
                    || !resolved_target_is_entry(artifact, &target)
                {
                    return None;
                }
                Some((
                    alias.exported.clone(),
                    ResolvedImport {
                        module: artifact.chunk_table.name(target.target_chunk_id).to_owned(),
                        export: alias.local.clone()?,
                        source_specifier: source.clone(),
                    },
                ))
            })
            .collect();
        modules.insert(
            chunk_name.to_string(),
            ModulePurityFacts {
                heap_scope: HeapModuleScope::Chunk(chunk_artifact.chunk_id),
                body: &parsed.module.body,
                imports,
                exports,
                reexports,
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
    ensure!(
        global_uses.len() == modules.len()
            && global_uses
                .keys()
                .all(|chunk_name| modules.contains_key(chunk_name)),
        "program summary is missing resolver-use inventory for a parsed source chunk"
    );

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

    let primitive_reads: BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>> = modules
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
                    let provider_closure = resolved_closure_precedes_importer(
                        importer,
                        &target.module,
                        &target.source_specifier,
                        &static_dependencies,
                    )?;
                    if !primitive.source_cells.iter().all(|cell| {
                        provider_closure.modules.contains(&cell.module)
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
                            source_specifier: target.source_specifier.clone(),
                            imported_name: target.export.clone(),
                            provider_module: target.module.clone(),
                            dependency_closure: provider_closure.modules.clone(),
                            provider_closure: Some(provider_closure),
                            source_cells: primitive.source_cells.clone(),
                        },
                    ))
                })
                .collect();
            (importer.clone(), reads)
        })
        .collect();
    // Raw stable export kinds are useful while resolving copy and re-export
    // chains, but they do not prove that an imported binding's GetValue is
    // available at its consumer. Keep the existing dependency-closure read
    // capability as the single join at the candidate-input boundary.
    let values = modules
        .iter()
        .map(|(name, module)| {
            let proved_reads = primitive_reads.get(name);
            let values = module
                .imported_values(&export_values)
                .into_iter()
                .filter(|(local, value)| {
                    proved_reads
                        .and_then(|reads| reads.get(local))
                        .is_some_and(|read| *value == Value::Prim(read.kinds))
                })
                .collect();
            (name.clone(), values)
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
    let source_callable_members = chunk_export_purity.iter()
        .filter(|(_, assertion)| !assertion.source_callable_members.is_empty())
        .map(|(chunk, assertion)| {
            for (export, members) in &assertion.source_callable_members {
                ensure!(assertion.namespace_independent_members.get(export).is_some_and(|audited| members.is_subset(audited)),
                    "source_callable_members {chunk}:{export} requires the same namespace_independent_members entries");
            }
            Ok((chunk.clone(), assertion.source_callable_members.clone()))
        }).collect::<Result<BTreeMap<_, _>>>()?;
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
    let requested_original_provider_exports = initializer_call_effect_inputs
        .values()
        .flatten()
        .map(|input| (input.provider_chunk.clone(), input.provider_export.clone()))
        .collect();
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
            provider_sources: &provider_sources,
            chunk_table: &artifact.chunk_table,
            global_uses: &provider_global_uses,
            source_callable_members: &source_callable_members,
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
    )?;
    // Bind conditional callable summaries to the direct source import route
    // of each consumer before passing them to per-chunk execution. The same
    // closure later proves the imported provider's module-cell initialization;
    // a missing or incomplete route must not leave a usable body summary.
    for (consumer, bindings) in &mut callable_summaries.imported_callables {
        let Some(consumer_facts) = modules.get(consumer) else {
            continue;
        };
        for (local, summary) in bindings {
            let closure = consumer_facts.imports.get(local).and_then(|target| {
                resolved_closure_precedes_importer(
                    consumer,
                    &target.module,
                    &target.source_specifier,
                    &static_dependencies,
                )
            });
            summary.bind_provider_closure_from_direct_import(closure, &artifact.chunk_table);
        }
    }
    let (source_initialized_module_cells, source_initialized_host_dependencies) =
        source_initialized_module_cells(
            &static_dependencies,
            &callable_summaries.noninvoking_provider_modules,
            &callable_summaries.module_cell_invariants,
            &artifact.chunk_table,
        );
    let bindings = resolve_imported_purities(
        &modules,
        &asserted_pure,
        &provider_global_uses,
        &mut callable_summaries,
    )?;
    for inputs in initializer_call_effect_inputs.values() {
        for input in inputs {
            if let Some((module, export, reason)) = known_original_provider_write(
                &input.provider_chunk,
                &input.provider_export,
                &modules,
                &callable_summaries,
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
    for (consumer, inputs) in &mut callable_summaries.imported_primitive_array_spread_inputs {
        for input in inputs {
            for operand in &mut input.evidence.operands {
                let closure = resolved_closure_precedes_importer(
                    consumer,
                    &operand.provider_module,
                    &operand.source_specifier,
                    &static_dependencies,
                )
                .with_context(|| {
                    format!(
                        "{} requires direct provider {} to have a complete acyclic ordinary static initialization closure before consumer {}",
                        input.evidence.spec_location, operand.provider_module, consumer
                    )
                })?;
                operand.provider_closure = Some(closure);
            }
        }
    }
    Ok(ProgramSummary {
        global_uses,
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
        values,
        primitive_reads,
        bindings,
        members: resolve_asserted_member_purities(&modules, &asserted_members),
        fluent: resolve_asserted_fluent_bindings(&modules, &asserted_fluent),
        constructors: resolve_asserted_pure_new_bindings(&modules, &asserted_pure_new),
        provider_programs: callable_summaries.provider_programs,
        module_cells: callable_summaries.module_cells,
        module_cell_invariants: callable_summaries.module_cell_invariants,
        source_initialized_module_cells,
        source_initialized_host_dependencies: Some(source_initialized_host_dependencies),
        imported_callables: callable_summaries.imported_callables,
        native_integrity: callable_summaries.native_integrity,
        bundle_globals: callable_summaries.bundle_globals,
        value_inputs: callable_summaries.value_inputs,
        native_operation_inputs: callable_summaries.native_operation_inputs,
        initializer_call_effect_inputs,
        imported_primitive_array_spread_inputs: callable_summaries
            .imported_primitive_array_spread_inputs,
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

fn resolve_initializer_call_effect_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    sites: &analysis_projection::AnalysisSourceSites,
    summary_options: ProgramSummaryOptions<'_>,
    dependencies: &ResolvedStaticDependencies,
) -> Result<BTreeMap<String, Vec<ResolvedInitializerCallEffectInput>>> {
    let ProgramSummaryOptions {
        graph_provenance: provenance,
        vendor_plan,
        replacement_packages,
    } = summary_options;
    let mut resolved = BTreeMap::new();
    for (chunk, options) in options {
        if options.initializer_call_effect_inputs.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk}.initializer_call_effect_inputs");
        let module = modules
            .get(chunk)
            .with_context(|| format!("{prefix} importer has no analyzed entry"))?;
        let selected = sites
            .initializer_call_effect_inputs
            .get(chunk)
            .with_context(|| format!("{prefix} has no resolved source sites"))?;
        ensure!(
            selected.len() == options.initializer_call_effect_inputs.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let source_sha = sites
            .source_sha256
            .get(chunk)
            .with_context(|| format!("{prefix} has no parser-input digest"))?;
        let provenance =
            provenance.context("initializer call effect inputs require parser-input provenance")?;
        let mut seen = BTreeSet::new();
        let mut entries = Vec::new();
        for (index, (contract, selected)) in options
            .initializer_call_effect_inputs
            .iter()
            .zip(selected)
            .enumerate()
        {
            let request = format!("{prefix}[{index}]");
            for (field, digest) in [
                ("audited_source_sha256", &contract.audited_source_sha256),
                ("audited_provider_sha256", &contract.audited_provider_sha256),
            ] {
                ensure!(
                    digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_hexdigit()),
                    "{request}.{field} must be a 64-character SHA-256 digest"
                );
            }
            ensure!(
                &contract.audited_source_sha256 == source_sha,
                "{request} has stale audited_source_sha256"
            );
            let provider_source = provenance
                .chunk_sources
                .iter()
                .find(|source| source.chunk_id == contract.provider_chunk)
                .with_context(|| {
                    format!("{request}.provider_chunk has no parser-input provenance")
                })?;
            ensure!(
                contract.audited_provider_sha256 == provider_source.sha256,
                "{request} has stale audited_provider_sha256"
            );
            let provider = modules
                .get(&contract.provider_chunk)
                .with_context(|| format!("{request}.provider_chunk has no analyzed entry"))?;
            ensure!(
                provider.exports.contains_key(&contract.provider_export),
                "{request}.provider_export has no analyzed export"
            );
            let provider_id = artifact
                .chunk_table
                .get(&contract.provider_chunk)
                .with_context(|| {
                    format!("{request}.provider_chunk has no resolved chunk identity")
                })?;
            let swapped_package = replacement_packages
                .get(&provider_id)
                .and_then(|exports| exports.get(&contract.provider_export));
            let effective_replacement = if let Some(swapped_package) = swapped_package {
                let plan = vendor_plan.with_context(|| {
                    format!("{request} has no effective vendor resolution plan")
                })?;
                let authored = contract.effective_replacement.as_ref().with_context(|| {
                    format!("{request}.effective_replacement is required for a swapped export")
                })?;
                let resolved = plan.bundled_replacement_export(provider_id, &contract.provider_export)
                    .with_context(|| format!("{request} requires an exact bundled_partial_swap export; other swap levels are unsupported"))?;
                ensure!(
                    !plan.replacement_bundle_is_open(provider_id),
                    "{request} replacement bundle has an open evaluation closure"
                );
                ensure!(
                    authored.package == *swapped_package
                        && authored.package == resolved.package
                        && authored.bundle_export == resolved.bundle_export
                        && authored.symbol_kind == resolved.symbol_kind
                        && authored.upstream_export == resolved.upstream_export,
                    "{request}.effective_replacement disagrees with the selected vendor mapping"
                );
                let bundle_path = graph_provenance::absolute(&resolved.bundle_path)?;
                let recorded_files = provenance
                    .files
                    .iter()
                    .filter(|file| {
                        matches!(&file.root, InputRoot::Source | InputRoot::Absolute)
                            && provenance
                                .resolve(&file.root, &file.path, &CurrentRoots::default())
                                .and_then(|path| graph_provenance::absolute(&path))
                                .is_ok_and(|path| path == bundle_path)
                    })
                    .collect::<Vec<_>>();
                let [recorded] = recorded_files.as_slice() else {
                    bail!("{request} selected vendor bundle has no unique captured source input");
                };
                ensure!(
                    authored.audited_bundle_sha256.len() == 64
                        && authored
                            .audited_bundle_sha256
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit()),
                    "{request}.effective_replacement.audited_bundle_sha256 must be a 64-character SHA-256 digest"
                );
                ensure!(
                    authored.audited_bundle_sha256 == recorded.sha256,
                    "{request} has stale effective_replacement.audited_bundle_sha256"
                );
                Some(EffectiveInitializerCallReplacement {
                    package: resolved.package,
                    bundle_path,
                    bundle_export: resolved.bundle_export,
                    symbol_kind: resolved.symbol_kind,
                    upstream_export: resolved.upstream_export,
                    bundle_sha256: recorded.sha256.clone(),
                })
            } else {
                ensure!(
                    contract.effective_replacement.is_none(),
                    "{request}.effective_replacement names an unswapped provider export"
                );
                None
            };
            let item = module
                .body
                .get(selected.body_index)
                .with_context(|| format!("{request} selected source item is missing"))?;
            let ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(Decl::Var(declaration))) = item else {
                anyhow::bail!("{request} requires one ordinary variable declaration");
            };
            let mut declarators = declaration.decls.iter().filter(|declarator| {
                matches!(&declarator.name, Pat::Ident(binding)
                    if binding.id.sym.as_ref() == selected.binding)
            });
            let declarator = declarators.next().with_context(|| {
                format!("{request} selected binding has no variable declarator")
            })?;
            ensure!(
                declarators.next().is_none(),
                "{request} selected binding has multiple variable declarators"
            );
            let Pat::Ident(binding) = &declarator.name else {
                anyhow::bail!("{request} requires one simple binding");
            };
            ensure!(
                binding.id.sym.as_ref() == selected.binding,
                "{request} source selector binding differs from initializer binding"
            );
            let Some(initializer) = &declarator.init else {
                anyhow::bail!("{request} requires an initializer");
            };
            let swc_ecma_ast::Expr::Call(call) = initializer.as_ref().unwrap_parens() else {
                anyhow::bail!("{request} requires a direct root call initializer");
            };
            let swc_ecma_ast::Callee::Expr(callee_expression) = &call.callee else {
                anyhow::bail!("{request} requires a direct imported callee");
            };
            let swc_ecma_ast::Expr::Ident(callee) = callee_expression.as_ref().unwrap_parens()
            else {
                anyhow::bail!("{request} requires a bare imported callee");
            };
            let [argument] = call.args.as_slice() else {
                anyhow::bail!("{request} requires exactly one function argument");
            };
            let swc_ecma_ast::Expr::Fn(callback) = argument.expr.as_ref().unwrap_parens() else {
                anyhow::bail!("{request} requires a fresh anonymous ordinary function argument");
            };
            ensure!(
                argument.spread.is_none()
                    && call.type_args.is_none()
                    && callback.ident.is_none()
                    && !callback.function.is_async
                    && !callback.function.is_generator
                    && callback.function.body.is_some()
                    && callback.function.decorators.is_empty()
                    && callback.function.type_params.is_none()
                    && callback.function.return_type.is_none(),
                "{request} requires a fresh anonymous ordinary function argument"
            );
            // The callee is a resolved direct ESM import binding. Its immutable
            // module environment cell supplies the selected GetBindingValue;
            // attempted assignment throws and remains an actual source effect.
            let imported = module.imports.get(callee.sym.as_ref()).with_context(|| {
                format!("{request} callee is not a resolved direct named import")
            })?;
            ensure!(
                imported.module == contract.provider_chunk
                    && imported.export == contract.provider_export,
                "{request} provider identity disagrees with the direct import"
            );
            let provider_closure = resolved_closure_precedes_importer(
                chunk,
                &imported.module,
                &imported.source_specifier,
                dependencies,
            )
            .with_context(|| {
                format!("{request} provider static closure does not precede importer")
            })?;
            ensure!(
                seen.insert((call.span.lo.0, call.span.hi.0)),
                "{request} duplicates an asserted initializer call"
            );
            entries.push(ResolvedInitializerCallEffectInput {
                reference: InitializerCallEffectInputRef {
                    defining_chunk: chunk.clone(),
                    input_index: index,
                },
                binding: binding.id.to_id(),
                callee: callee.to_id(),
                call_span: call.span,
                source_sha256: source_sha.clone(),
                provider_chunk: imported.module.clone(),
                provider_export: imported.export.clone(),
                provider_sha256: provider_source.sha256.clone(),
                effective_replacement,
                provider_dependency_closure: provider_closure.modules.clone(),
                provider_closure: Some(provider_closure),
            });
        }
        resolved.insert(chunk.clone(), entries);
    }
    Ok(resolved)
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

    // Original parse identity and analysis-only storage are internal invariants.
    // Exercise the real loader/preparation boundary, including a forwarding edge
    // and a callable-free static leaf, without author premises.
    #[test]
    fn inferred_provider_uses_canonical_preparation_and_preserves_passthrough() {
        js_ast::with_swc_globals(|| {
            let root = tempfile::TempDir::new().unwrap();
            let provider_source = "// exact pass-through bytes\nexport function reader() { return 7; }\nexport function pick(record) { return record.value; }\nfunction globalReader() { return undefined; }\n";
            for (name, source) in [
                (
                    "app.js",
                    "import { reader, pick, limit } from './forward.js'; pick({ value: 1 }); export const copied = reader(); export const count = limit;",
                ),
                (
                    "forward.js",
                    "export { reader, pick } from './provider.js'; export { limit } from './leaf.js';",
                ),
                ("provider.js", provider_source),
                ("leaf.js", "export const limit = 7;"),
                ("unrelated.js", "export const ignored = 9;"),
            ] {
                std::fs::write(root.path().join(name), source).unwrap();
            }
            let list = root.path().join("js-files.txt");
            std::fs::write(
                &list,
                "app.js\nforward.js\nprovider.js\nleaf.js\nunrelated.js\n",
            )
            .unwrap();
            for retain_closure in [false, true] {
                let selected = if retain_closure {
                    BTreeSet::from(["app".to_owned()])
                } else {
                    BTreeSet::new()
                };
                let prepared = prepare_chunks::prepare_js_chunks(
                    &selected,
                    &BTreeSet::new(),
                    &BTreeMap::new(),
                    artifact::load_js_chunks(root.path(), &list).unwrap(),
                )
                .unwrap();
                let artifact = prepared.program.artifact();
                let provider_id = artifact.chunk_table.get("provider").unwrap();
                let provider = artifact.chunk(provider_id).unwrap();
                let file = provider
                    .js
                    .get_file(artifact::CANONICAL_CHUNK_ENTRY_FILE)
                    .unwrap();
                assert_eq!(file.ast().is_some(), retain_closure);
                assert_eq!(file.render_source().unwrap(), provider_source);
                if let Some(parsed) = file.ast() {
                    let inventory = provider
                        .analysis
                        .runtime_facts
                        .global_uses
                        .as_ref()
                        .unwrap();
                    assert_eq!(inventory.source_file, parsed.source_file_name());
                    let (binding, _) = inventory
                        .uses
                        .iter()
                        .find(|(id, _)| id.0 == "undefined")
                        .unwrap();
                    assert_eq!(
                        binding.1,
                        swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark)
                    );
                }
                for chunk in &artifact.chunks {
                    let name = artifact.chunk_table.name(chunk.chunk_id);
                    let entry = chunk
                        .js
                        .get_file(artifact::CANONICAL_CHUNK_ENTRY_FILE)
                        .unwrap();
                    let retained = matches!(name, "app" | "forward")
                        || (retain_closure && name != "unrelated");
                    assert_eq!(entry.ast().is_some(), retained, "{name}");
                }
                let closure = prepared
                    .program
                    .indexes()
                    .selected_static_import_closure(artifact, &["app".to_owned()])
                    .unwrap();
                let reparsed = reparse_unretained_entries(artifact, Some(&closure));
                assert_eq!(reparsed.is_empty(), retain_closure);
                let host_behavior = spec::HostBehavior::default();
                let facts = collect_program_summary(
                    artifact,
                    prepared.program.indexes(),
                    &AnalysisProjection::new(
                        &BTreeMap::new(),
                        &BTreeMap::new(),
                        &host_behavior,
                        None,
                        BTreeMap::new(),
                        AnalysisSourceSites::default(),
                        BTreeMap::new(),
                    )
                    .unwrap(),
                    ProgramSummaryOptions {
                        graph_provenance: None,
                        vendor_plan: None,
                        replacement_packages: &BTreeMap::new(),
                    },
                    Some(&closure),
                    &reparsed,
                )
                .unwrap();
                let inventories = facts.provider_programs.source_inventories();
                assert_eq!(inventories.contains_key("provider"), retain_closure);
                assert_eq!(inventories.contains_key("leaf"), retain_closure);
                let ordinals = crate::context_ordinals(
                    artifact,
                    &[],
                    None,
                    &reparsed,
                    facts.global_uses.values().map(AsRef::as_ref),
                )
                .unwrap();
                let snapshot = |summary| {
                    serde_json::to_value(
                        analysis::analysis_inputs::saved::SavedCallableSummary::snapshot(
                            summary,
                            &ordinals,
                            &inventories,
                        )
                        .unwrap(),
                    )
                    .unwrap()
                };
                let reader = &facts.imported_callables["app"]["reader"];
                assert!(reader.foreign_call.is_some());
                let saved_reader = snapshot(reader);
                assert_eq!(
                    saved_reader["foreign_call_body_effects"].is_object(),
                    retain_closure
                );
                let pick = facts.imported_callables["app"]
                    .get("pick")
                    .cloned()
                    .unwrap_or_default();
                let saved_pick = snapshot(&pick);
                let transfers = saved_pick["conditional_bodies"].as_array().unwrap();
                assert_eq!(!transfers.is_empty(), retain_closure);
                for transfer in transfers {
                    assert_eq!(transfer["identity"]["defining_module"], "provider");
                    assert_eq!(
                        transfer["source"]["sha256"],
                        graph_provenance::sha256(provider_source.as_bytes())
                    );
                }
                facts
                    .provider_programs
                    .validate_transfers(&facts.imported_callables)
                    .unwrap();
                assert!(facts.module_cell_invariants.keys().all(|cell| {
                    inventories.contains_key(artifact.chunk_table.name(cell.chunk))
                }));
                let emission = artifact::EmissionFiles::from_prepared(prepared.program);
                let emitted_provider = emission.files().chunk(provider_id).unwrap();
                let emitted = emitted_provider
                    .js
                    .get_file(artifact::CANONICAL_CHUNK_ENTRY_FILE)
                    .unwrap();
                assert!(emitted.ast().is_none());
                assert_eq!(emitted.render_source().unwrap(), provider_source);
            }
        });
    }

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

    fn provider_dependencies(
        importer: &str,
        provider: &str,
        edges: &[(&str, &[&str])],
        complete_modules: &[&str],
        host_dependencies: &[AdmittedHostModuleDependency],
    ) -> ResolvedStaticDependencies {
        let mut modules = BTreeSet::from([importer.to_string(), provider.to_string()]);
        let mut targets = BTreeMap::new();
        for (source, dependencies) in edges {
            modules.insert((*source).to_string());
            let dependencies: BTreeSet<_> = dependencies
                .iter()
                .map(|dependency| {
                    modules.insert((*dependency).to_string());
                    (*dependency).to_string()
                })
                .collect();
            targets.insert((*source).to_string(), dependencies);
        }
        modules.extend(complete_modules.iter().map(|module| (*module).to_string()));
        modules.extend(
            host_dependencies
                .iter()
                .map(|dependency| dependency.source_module.clone()),
        );
        for module in &modules {
            targets.entry(module.clone()).or_default();
        }

        let mut ordinary_requests = BTreeMap::new();
        ordinary_requests.insert(
            importer.to_string(),
            BTreeSet::from([("./provider.js".to_string(), provider.to_string())]),
        );
        let mut admitted_host_modules: BTreeMap<_, BTreeSet<_>> = modules
            .iter()
            .map(|module| (module.clone(), BTreeSet::new()))
            .collect();
        for dependency in host_dependencies {
            admitted_host_modules
                .entry(dependency.source_module.clone())
                .or_default()
                .insert(dependency.clone());
        }
        ResolvedStaticDependencies {
            targets,
            ordinary_requests,
            admitted_host_modules,
            complete_modules: complete_modules
                .iter()
                .map(|module| (*module).to_string())
                .collect(),
        }
    }

    fn provider_closure(
        provider: &str,
        dependencies: &ResolvedStaticDependencies,
    ) -> Option<ResolvedProviderDependencyClosure> {
        resolved_closure_precedes_importer("static/app", provider, "./provider.js", dependencies)
    }

    #[test]
    fn provider_closure_rejects_internal_cycles() {
        let dependencies = provider_dependencies(
            "static/app",
            "static/provider",
            &[
                ("static/provider", &["static/helper"]),
                ("static/helper", &["static/provider"]),
            ],
            &["static/app", "static/provider", "static/helper"],
            &[],
        );

        assert!(provider_closure("static/provider", &dependencies).is_none());
    }

    #[test]
    fn provider_closure_accepts_dag_diamonds_once() {
        let dependencies = provider_dependencies(
            "static/app",
            "static/provider",
            &[
                ("static/provider", &["static/left", "static/right"]),
                ("static/left", &["static/leaf"]),
                ("static/right", &["static/leaf"]),
            ],
            &[
                "static/app",
                "static/provider",
                "static/left",
                "static/right",
                "static/leaf",
            ],
            &[],
        );

        let closure = provider_closure("static/provider", &dependencies)
            .expect("a complete DAG diamond is a valid provider closure");
        assert_eq!(
            closure.modules,
            BTreeSet::from([
                "static/provider".to_string(),
                "static/left".to_string(),
                "static/right".to_string(),
                "static/leaf".to_string(),
            ])
        );
        assert!(closure.admitted_host_modules.is_empty());
    }

    #[test]
    fn provider_closure_rejects_importer_backedges() {
        let dependencies = provider_dependencies(
            "static/app",
            "static/provider",
            &[("static/provider", &["static/app"])],
            &["static/app", "static/provider"],
            &[],
        );

        assert!(provider_closure("static/provider", &dependencies).is_none());
    }

    #[test]
    fn provider_closure_keeps_admitted_host_leaves_and_rejects_unknown_ones() {
        let host_module = spec::HostModule::Node(spec::NodeBuiltinModule::Path);
        let host_dependency = AdmittedHostModuleDependency {
            source_module: "static/provider".to_string(),
            specifier: "path".to_string(),
            module: host_module,
            consumed_premise: analysis::analysis_inputs::HostBehaviorKey::reentry_free_module(
                host_module,
            ),
        };
        let admitted = provider_dependencies(
            "static/app",
            "static/provider",
            &[],
            &["static/app", "static/provider"],
            &[host_dependency.clone()],
        );
        let closure = provider_closure("static/provider", &admitted)
            .expect("an explicitly admitted host leaf completes the provider closure");
        assert_eq!(
            closure.modules,
            BTreeSet::from(["static/provider".to_string()])
        );
        assert_eq!(
            closure.admitted_host_modules,
            BTreeSet::from([host_dependency])
        );
        assert!(closure.is_well_formed());

        let unknown =
            provider_dependencies("static/app", "static/provider", &[], &["static/app"], &[]);
        assert!(provider_closure("static/provider", &unknown).is_none());
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
            let admitted_path = spec::HostBehavior {
                reentry_free_builtin_modules: [spec::NodeBuiltinModule::Path].into_iter().collect(),
                ..spec::HostBehavior::default()
            };
            let path_dependency = AdmittedHostModuleDependency {
                source_module: "static/peer".to_string(),
                specifier: "path".to_string(),
                module: spec::HostModule::Node(spec::NodeBuiltinModule::Path),
                consumed_premise: analysis::analysis_inputs::HostBehaviorKey::reentry_free_module(
                    spec::HostModule::Node(spec::NodeBuiltinModule::Path),
                ),
            };
            for (provider, host_behavior, expected_read, expected_host_dependencies) in [
                (
                    "export const limit = 2;",
                    spec::HostBehavior::default(),
                    true,
                    BTreeSet::new(),
                ),
                (
                    "import '../app/app.js'; export const limit = 2;",
                    spec::HostBehavior::default(),
                    false,
                    BTreeSet::new(),
                ),
                (
                    "import './missing.js'; export const limit = 2;",
                    spec::HostBehavior::default(),
                    false,
                    BTreeSet::new(),
                ),
                (
                    "import 'path'; export const limit = 2;",
                    spec::HostBehavior::default(),
                    false,
                    BTreeSet::new(),
                ),
                (
                    "import 'path'; export const limit = 2;",
                    admitted_path.clone(),
                    true,
                    BTreeSet::from([path_dependency.clone()]),
                ),
                (
                    "import 'unknown-static-package'; export const limit = 2;",
                    admitted_path.clone(),
                    false,
                    BTreeSet::new(),
                ),
            ] {
                let artifact = test_bundle(&[
                    (
                        "static/app",
                        "import { limit as bytes } from '../peer/peer.js'; export const copied = bytes;",
                    ),
                    ("static/peer", provider),
                ]);
                let indexes = ArtifactIndexes::build(&artifact).expect("build indexes");
                let no_options = BTreeMap::new();
                let no_export_purity = BTreeMap::new();
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
                        vendor_plan: None,
                        replacement_packages: &BTreeMap::new(),
                    },
                    None,
                    &reparse_unretained_entries(&artifact, None),
                )
                .expect("program summary analysis succeeds");
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
                    let closure = read
                        .provider_closure
                        .as_ref()
                        .expect("a source read carries its complete provider closure");
                    assert!(closure.is_well_formed());
                    assert_eq!(closure.modules, read.dependency_closure);
                    assert_eq!(closure.admitted_host_modules, expected_host_dependencies);
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
    fn source_initialized_cell_summary_retains_admitted_host_leaf_premise() {
        let mut chunks = source_identity::ChunkTable::default();
        chunks.intern("static/app".to_string());
        let provider = chunks.intern("static/peer".to_string());
        let cell = ModuleCellId {
            chunk: provider,
            declaration_origin: 41,
        };
        let host_module = spec::HostModule::Node(spec::NodeBuiltinModule::Path);
        let host_dependency = AdmittedHostModuleDependency {
            source_module: "static/peer".to_string(),
            specifier: "path".to_string(),
            module: host_module,
            consumed_premise: analysis::analysis_inputs::HostBehaviorKey::reentry_free_module(
                host_module,
            ),
        };
        let dependencies = ResolvedStaticDependencies {
            targets: BTreeMap::from([
                (
                    "static/app".to_string(),
                    BTreeSet::from(["static/peer".to_string()]),
                ),
                ("static/peer".to_string(), BTreeSet::new()),
            ]),
            ordinary_requests: BTreeMap::from([
                (
                    "static/app".to_string(),
                    BTreeSet::from([("../peer/peer.js".to_string(), "static/peer".to_string())]),
                ),
                ("static/peer".to_string(), BTreeSet::new()),
            ]),
            admitted_host_modules: BTreeMap::from([
                ("static/app".to_string(), BTreeSet::new()),
                (
                    "static/peer".to_string(),
                    BTreeSet::from([host_dependency.clone()]),
                ),
            ]),
            complete_modules: BTreeSet::from(["static/app".to_string(), "static/peer".to_string()]),
        };
        let invariant = serde_json::from_value::<PrimitiveCellInvariant>(serde_json::json!({
            "declaration": {
                "owner": serde_json::to_value(source_identity::SourceUnitOrdinal(0)).unwrap(),
                "activation_callable": null,
                "binding_span": serde_json::to_value(swc_common::DUMMY_SP).unwrap(),
                "initializer_span": null,
            },
            "post_init_kinds": serde_json::to_value(PrimKinds::NUMBER).unwrap(),
            "stores": [],
        }))
        .expect("source invariant fixture decodes through its checked data schema");

        let (initialized, host_dependencies) = source_initialized_module_cells(
            &dependencies,
            &BTreeSet::from(["static/peer".to_string()]),
            &BTreeMap::from([(cell, invariant)]),
            &chunks,
        );

        assert_eq!(initialized["static/app"], BTreeSet::from([cell]));
        assert_eq!(
            host_dependencies["static/app"],
            BTreeSet::from([host_dependency])
        );
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
                    vendor_plan: None,
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
                    vendor_plan: None,
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
