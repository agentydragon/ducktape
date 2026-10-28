//! Program-level cross-module purity pass: resolve every chunk's imports
//! against the artifact and run the purity fixpoint
//! (`analysis::cross_module_purity`). Output feeds each chunk's
//! `AnalysisInputs::imported_purities` so the per-chunk classifier can see
//! through calls to imported functions instead of bailing `unknown_call`.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::Arc;

use analysis::analysis_inputs::{
    CallableSummary, NativeIntegrity, ResolvedBindingValueInput, SelectedBindingValueInput,
    ValueInputEvidence, ValueInputRef,
};
use analysis::cross_module_purity::{
    ModulePurityFacts, ResolvedImport, resolve_asserted_callback_factory_members,
    resolve_asserted_fluent_bindings, resolve_asserted_inline_callback_factories,
    resolve_asserted_member_purities, resolve_asserted_namespace_independent_members,
    resolve_asserted_no_reentry_bindings, resolve_asserted_no_reentry_callbacks,
    resolve_asserted_no_reentry_members, resolve_asserted_no_reentry_record_members,
    resolve_asserted_pure_new_bindings, resolve_asserted_retained_callback_constructors,
    resolve_asserted_retained_callback_factories,
    resolve_asserted_retained_callback_factory_members, resolve_imported_callable_summaries,
    resolve_imported_purities,
};
use analysis::purity::Purity;
use anyhow::{Context, Result, bail, ensure};
use artifact::reports::{ExportAliasRecord, ImportRecord, ImportSpecifierKind};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId};
use graph_provenance::{CurrentRoots, GraphProvenance};
use js_ast::ParsedJsModule;
use program_analysis::analyze_program_shallow;
use selector_outcome::Outcome as SelectorOutcome;
use selector_resolve::{Chunk as SelectorChunk, EntityIndex};
use source_match::source_match_claim_member_selectors;
use spec::{ChunkExportPurity, DependencyGraphOptions};
use swc_common::Spanned;
use swc_ecma_ast::{Decl, Module, ModuleDecl, ModuleItem, Pat};

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
/// kept as raw source (non-selected chunks). The oracle parses these itself —
/// every listed chunk participates in cross-module purity, not just the
/// selected ones.
struct ReparsedEntry {
    parsed: ParsedJsModule,
    imports: Vec<ImportRecord>,
    export_aliases: Vec<ExportAliasRecord>,
    primitive_exports: BTreeSet<String>,
    primitive_export_import_dependencies: BTreeMap<String, BTreeSet<String>>,
    primitive_export_reexports: BTreeMap<String, (String, String)>,
}

/// Output of the program-level purity pass, keyed by chunk name.
pub(super) struct CrossModulePurities {
    /// Per-chunk imported-binding verdicts (`AnalysisInputs::imported_purities`).
    pub(super) bindings: BTreeMap<String, BTreeMap<String, Arc<Purity>>>,
    pub(super) primitives: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk pure-member sets for imported namespace-like bindings,
    /// merged into `AnalysisAssumptions::declared_pure_members`.
    pub(super) members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    /// Per-chunk fluent-trusted import binding names (deep-purity roots),
    /// merged into `AnalysisAssumptions::fluent_bindings`.
    pub(super) fluent: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk local names whose imported exports have an asserted pure
    /// construction contract (`chunk_export_purity.pure_new_exports`).
    pub(super) constructors: BTreeMap<String, BTreeSet<String>>,
    /// Per-chunk bare imported callable summaries with their defining evidence.
    pub(super) imported_callables: BTreeMap<String, BTreeMap<String, CallableSummary>>,
    /// Source-derived native mutation census shared by every analyzed chunk.
    pub(super) native_integrity: Option<NativeIntegrity>,
    /// Source-resolved author inputs for the provider's own local analysis.
    pub(super) value_inputs: BTreeMap<String, Vec<ResolvedBindingValueInput>>,
    pub(super) inline_callback_factories: BTreeMap<String, BTreeSet<String>>,
    pub(super) callback_factory_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    pub(super) retained_callback_factories:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub(super) retained_callback_factory_members:
        BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>>,
    pub(super) retained_callback_constructors:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub(super) no_reentry_exports: BTreeMap<String, BTreeSet<String>>,
    pub(super) no_reentry_callback_exports:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub(super) no_reentry_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    pub(super) no_reentry_record_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    pub(super) namespace_independent_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
}

/// Static-import chunk closure for program-level purity analysis.
///
/// Starts from the user-selected chunks and follows each retained chunk's
/// shallow-analysis import records through the same artifact-index resolver used
/// by emission. Excluded chunks are opacity boundaries: they are neither added
/// to the closure nor traversed.
pub(super) fn selected_static_import_closure(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    selected_chunk_ids: &[String],
    excluded_chunks: &BTreeSet<ChunkId>,
) -> Result<BTreeSet<ChunkId>> {
    let mut closure = BTreeSet::new();
    let mut pending = Vec::new();
    for chunk_id in selected_chunk_ids {
        let interned = artifact
            .chunk_table
            .get(chunk_id)
            .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
        if excluded_chunks.contains(&interned) {
            continue;
        }
        if closure.insert(interned) {
            pending.push(interned);
        }
    }

    while let Some(chunk_id) = pending.pop() {
        let Some(chunk_artifact) = artifact.find_chunk(chunk_id) else {
            continue;
        };
        let entry_file = &chunk_artifact.analysis.entry_file;
        for import in &chunk_artifact.analysis.imports {
            let Some(target) = indexes.resolve_runtime_import_reference(
                &import.source,
                chunk_id,
                entry_file,
                &artifact.chunk_table,
            ) else {
                continue;
            };
            if excluded_chunks.contains(&target.target_chunk_id) {
                continue;
            }
            if closure.insert(target.target_chunk_id) {
                pending.push(target.target_chunk_id);
            }
        }
    }

    Ok(closure)
}

fn chunk_allowed(chunk_id: ChunkId, chunk_filter: Option<&BTreeSet<ChunkId>>) -> bool {
    chunk_filter.is_none_or(|filter| filter.contains(&chunk_id))
}

fn resolved_target_is_entry(
    artifact: &ChunkBundle,
    target: &artifact::ResolvedImportReference,
) -> bool {
    artifact
        .find_chunk(target.target_chunk_id)
        .is_some_and(|chunk| chunk.analysis.entry_file == target.target_file)
}

/// Per-chunk-name imported-binding purity maps for the whole artifact.
/// A chunk whose entry can neither be reused (retained AST) nor re-parsed
/// stays opaque: its exports get no verdicts and imports of it stay
/// unresolved (`unknown_call`), exactly as before this pass existed.
///
/// `excluded_chunks` (fully vendor-swapped chunks excluded from the
/// emission set) stay opaque to the oracle: an excluded chunk's body
/// is replaced by the upstream package at serve time, so verdicts
/// derived from the bundle copy would assert purity about code that is
/// never emitted. Imports of such chunks stay `unknown_call`.
pub(super) struct CrossModuleAnalysisOptions<'a> {
    pub(super) chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub(super) chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub(super) graph_provenance: Option<&'a GraphProvenance>,
}

pub(super) fn collect_cross_module_imported_purities(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    options: CrossModuleAnalysisOptions<'_>,
    declared_pure_new_bindings: &BTreeMap<String, BTreeSet<String>>,
    excluded_chunks: &BTreeSet<ChunkId>,
    primitive_opaque_chunks: &BTreeSet<ChunkId>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<CrossModulePurities> {
    let CrossModuleAnalysisOptions {
        chunk_export_purity,
        chunk_analysis_options,
        graph_provenance,
    } = options;
    // Pass 1: re-parse entries stored as raw source so every chunk's body is
    // analyzable. Parse failures only warn — the pass is an analysis
    // refinement, and an unparseable chunk degrades to today's conservative
    // behavior rather than failing the build.
    let mut reparsed: BTreeMap<ChunkId, ReparsedEntry> = BTreeMap::new();
    for chunk_artifact in &artifact.chunks {
        if !chunk_allowed(chunk_artifact.chunk_id, chunk_filter) {
            continue;
        }
        if excluded_chunks.contains(&chunk_artifact.chunk_id) {
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
                        primitive_exports: analysis.runtime_facts.stable_primitive_exports,
                        primitive_export_import_dependencies: analysis
                            .runtime_facts
                            .stable_primitive_export_import_dependencies,
                        primitive_export_reexports: analysis
                            .runtime_facts
                            .stable_primitive_export_reexports,
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

    // Pass 2: assemble each chunk's oracle inputs from whichever body is
    // available (retained AST or re-parsed entry).
    let mut modules = BTreeMap::new();
    let mut primitive_exports = BTreeMap::<String, BTreeSet<String>>::new();
    let mut primitive_export_import_candidates =
        BTreeMap::<String, BTreeMap<String, BTreeSet<String>>>::new();
    let mut primitive_export_reexport_dependencies =
        BTreeMap::<(String, String), ResolvedImport>::new();
    for chunk_artifact in &artifact.chunks {
        if !chunk_allowed(chunk_artifact.chunk_id, chunk_filter) {
            continue;
        }
        if excluded_chunks.contains(&chunk_artifact.chunk_id) {
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
        if !primitive_opaque_chunks.contains(&chunk_artifact.chunk_id) {
            let (exports, import_candidates, reexport_candidates) =
                match reparsed.get(&chunk_artifact.chunk_id) {
                    Some(entry) => (
                        &entry.primitive_exports,
                        &entry.primitive_export_import_dependencies,
                        &entry.primitive_export_reexports,
                    ),
                    None => {
                        let facts = &chunk_artifact.analysis.runtime_facts;
                        (
                            &facts.stable_primitive_exports,
                            &facts.stable_primitive_export_import_dependencies,
                            &facts.stable_primitive_export_reexports,
                        )
                    }
                };
            primitive_exports.insert(chunk_name.to_string(), exports.clone());
            primitive_export_import_candidates
                .insert(chunk_name.to_string(), import_candidates.clone());
            for (export, (source, imported_export)) in reexport_candidates {
                let Some(target) = indexes.resolve_runtime_import_reference(
                    source,
                    chunk_artifact.chunk_id,
                    entry_file,
                    &artifact.chunk_table,
                ) else {
                    continue;
                };
                if excluded_chunks.contains(&target.target_chunk_id)
                    || !chunk_allowed(target.target_chunk_id, chunk_filter)
                    || !resolved_target_is_entry(artifact, &target)
                {
                    continue;
                }
                primitive_export_reexport_dependencies.insert(
                    (chunk_name.to_string(), export.clone()),
                    ResolvedImport {
                        module: artifact
                            .chunk_table
                            .name(target.target_chunk_id)
                            .to_string(),
                        export: imported_export.clone(),
                    },
                );
            }
        }
        let body = &parsed.module.body;
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
            if excluded_chunks.contains(&target.target_chunk_id)
                || !chunk_allowed(target.target_chunk_id, chunk_filter)
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
        for item in body {
            if let ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) = item {
                for name in exported_decl_names(&export_decl.decl) {
                    exports.entry(name.clone()).or_insert(name);
                }
            }
        }
        modules.insert(
            chunk_name.to_string(),
            ModulePurityFacts {
                body,
                imports,
                exports,
                source: Some((
                    chunk_artifact.js.get_file(entry_file).map_or_else(
                        || chunk_artifact.analysis.source_path.clone(),
                        |file| file.metadata.source_path.clone(),
                    ),
                    parsed.line_index(),
                )),
            },
        );
    }

    let mut primitive_export_import_dependencies =
        BTreeMap::<(String, String), Vec<ResolvedImport>>::new();
    for (chunk, candidates) in primitive_export_import_candidates {
        let Some(module) = modules.get(&chunk) else {
            continue;
        };
        for (export, local_imports) in candidates {
            let dependencies: Option<Vec<_>> = local_imports
                .iter()
                .map(|local| module.imports.get(local).cloned())
                .collect();
            if let Some(dependencies) = dependencies {
                primitive_export_import_dependencies.insert((chunk.clone(), export), dependencies);
            }
        }
    }

    // Primitive values are source facts only when supported by a direct
    // expression or a chain that reaches one. Start from those direct seeds
    // and add an alias only when every dependency is already proven. An
    // unsupported alias cycle therefore stays unknown.
    let mut primitive_exports_changed = true;
    while primitive_exports_changed {
        primitive_exports_changed = false;
        let mut additions = Vec::new();
        for ((chunk, export), dependencies) in &primitive_export_import_dependencies {
            if dependencies.iter().all(|dependency| {
                primitive_exports
                    .get(&dependency.module)
                    .is_some_and(|exports| exports.contains(&dependency.export))
            }) {
                additions.push((chunk.clone(), export.clone()));
            }
        }
        for (key, dependency) in &primitive_export_reexport_dependencies {
            if primitive_exports
                .get(&dependency.module)
                .is_some_and(|exports| exports.contains(&dependency.export))
            {
                additions.push(key.clone());
            }
        }
        for (chunk, export) in additions {
            if primitive_exports.entry(chunk).or_default().insert(export) {
                primitive_exports_changed = true;
            }
        }
    }
    let value_inputs = resolve_value_inputs(
        artifact,
        &modules,
        chunk_analysis_options,
        graph_provenance,
        excluded_chunks,
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
    let callable_summaries =
        resolve_imported_callable_summaries(&modules, &asserted_callback_factories, &value_inputs)?;
    Ok(CrossModulePurities {
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
        primitives: modules
            .iter()
            .map(|(name, module)| {
                let locals = module
                    .imports
                    .iter()
                    .filter(|(_, target)| {
                        primitive_exports
                            .get(&target.module)
                            .is_some_and(|exports| exports.contains(&target.export))
                    })
                    .map(|(local, _)| local.clone())
                    .collect();
                (name.clone(), locals)
            })
            .collect(),
        bindings: resolve_imported_purities(&modules, &asserted_pure, &callable_summaries),
        members: resolve_asserted_member_purities(&modules, &asserted_members),
        fluent: resolve_asserted_fluent_bindings(&modules, &asserted_fluent),
        constructors: resolve_asserted_pure_new_bindings(&modules, &asserted_pure_new),
        imported_callables: callable_summaries.imported_callables,
        native_integrity: callable_summaries.native_integrity,
        value_inputs: callable_summaries.value_inputs,
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
    options: &BTreeMap<String, DependencyGraphOptions>,
    graph_provenance: Option<&GraphProvenance>,
    excluded_chunks: &BTreeSet<ChunkId>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<SelectedBindingValueInput>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.value_inputs.is_empty() {
            continue;
        }
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
            !excluded_chunks.contains(&chunk.chunk_id),
            "chunk_analysis_options.{chunk_name}.value_inputs cannot target a fully swapped or excluded provider"
        );
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
        let spec_path = value_input_spec_path(provenance)?;
        let module = Module {
            span: Default::default(),
            body: facts.body.to_vec(),
            shebang: None,
        };
        // Build the canonical structural index once for this provider. Each
        // authored input below contributes only a selector claim against it.
        let structural = analysis::facts::analyze_chunk_structural(&module, None, |_| None);
        let selector_chunk = SelectorChunk::new(chunk_name, &module, structural);
        let mut chunk_inputs = Vec::with_capacity(options.value_inputs.len());
        for (input_index, contract) in options.value_inputs.iter().enumerate() {
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
            ensure!(
                contract.source_match.bindings.len() == 1,
                "chunk_analysis_options.{chunk_name}.value_inputs[{input_index}].source_match must select exactly one declared binding"
            );
            let spec::SourceMatchBinding::Local(local) = &contract.source_match.bindings[0] else {
                bail!(
                    "chunk_analysis_options.{chunk_name}.value_inputs[{input_index}].source_match does not accept detailed name/rename payloads"
                );
            };
            let request_id =
                format!("chunk_analysis_options.{chunk_name}.value_inputs[{input_index}]");
            let mut selectors =
                source_match_claim_member_selectors(&request_id, &contract.source_match)?;
            ensure!(
                selectors.len() == 1,
                "{request_id}.source_match did not produce one selector"
            );
            let selector = selectors.pop().expect("one selector was checked");
            ensure!(
                selector
                    .parsed_selector
                    .declared_binding_names()
                    .iter()
                    .any(|name| name == local),
                "{request_id}.source_match binding `{local}` must be declared by its pattern; free-reference selection is unsupported"
            );
            let selector_module = spec_resolution::spec_module(
                format!("value_input_{input_index}"),
                vec![spec_resolution::member_from_source_match(
                    selector.export_name,
                    selector.parsed_selector,
                )],
                Vec::new(),
            );
            let resolution = selector_chunk.resolve(&[selector_module])?;
            let outcome = resolution
                .outcome(0, EntityIndex::Member(0))
                .with_context(|| {
                    format!("{request_id}.source_match produced no selector outcome")
                })?;
            let (body_index, selected_binding) = match &outcome.outcome {
                SelectorOutcome::Resolved {
                    body_index,
                    binding: Some(binding),
                    ..
                } => (*body_index, binding.clone()),
                other => bail!(
                    "{request_id}.source_match must select exactly one declared binding; selector result: {other:?}"
                ),
            };
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
            let parser_input_file = provenance.resolve(
                &source.input_file.root,
                &source.input_file.path,
                &CurrentRoots::default(),
            )?;
            let reference = ValueInputRef {
                defining_chunk: chunk_name.clone(),
                input_index,
            };
            let evidence = ValueInputEvidence {
                reference,
                spec_location: format!("{}:{request_id}", spec_path.display()),
                source_match: contract.source_match.clone(),
                value_kind: contract.value.kind,
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: parser_input_file.to_string_lossy().into_owned(),
                audited_source_sha256: contract.audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                declaration_location: None,
                initializer_location: None,
                expected_export: contract.expected_export.clone(),
                note: contract.note.clone(),
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

/// Return the original authored flat-spec or tree-config path recorded when
/// the graph was captured. Flat specs do not populate `config_path`; their
/// canonical `--spec` argument is retained in the provenance generation
/// command and is already the source of truth for replay.
fn value_input_spec_path(provenance: &GraphProvenance) -> Result<PathBuf> {
    if let Some(config_path) = &provenance.config_path {
        return Ok(provenance.roots.spec.join(config_path));
    }
    let spec_arg = provenance
        .generation_command
        .iter()
        .position(|argument| argument == "--spec")
        .and_then(|index| provenance.generation_command.get(index + 1))
        .context("graph provenance has no recorded flat spec path for value input evidence")?;
    Ok(PathBuf::from(spec_arg))
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::reports::{ChunkAnalysisReport, ChunkFileRecord};
    use artifact::{
        ChunkArtifact, ChunkBundle, ChunkMetadata, ChunkTable, FileMetadata, FileRole, JsChunk,
        JsFile, JsFileBody,
    };

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

    fn closure_names(artifact: &ChunkBundle, closure: &BTreeSet<ChunkId>) -> BTreeSet<String> {
        closure
            .iter()
            .map(|chunk_id| artifact.chunk_table.name(*chunk_id).to_string())
            .collect()
    }

    #[test]
    fn selected_static_import_closure_stops_at_excluded_chunks() {
        let artifact = test_bundle(&[
            (
                "static/app",
                "import { callWrap } from \"./adapter.js\";\ncallWrap(() => 1);\n",
            ),
            (
                "static/adapter",
                "import { wrap } from \"./vendorlib.js\";\n\
export function callWrap(f) { return wrap(f); }\n",
            ),
            (
                "static/vendorlib",
                "export function wrap(f) { return { impl: f }; }\n",
            ),
        ]);
        let indexes = ArtifactIndexes::build(&artifact).expect("build indexes");
        let excluded = BTreeSet::from([artifact
            .chunk_table
            .get("static/adapter")
            .expect("adapter chunk")]);
        let closure = selected_static_import_closure(
            &artifact,
            &indexes,
            &["static/app".to_string()],
            &excluded,
        )
        .expect("closure");

        assert_eq!(
            closure_names(&artifact, &closure),
            BTreeSet::from(["static/app".to_string()])
        );
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
            assert!(facts.stable_primitive_exports.contains("label"));

            let parsed = js_ast::parse_js_module(
                "cjs-surface.js",
                "if (false) import.meta.require('./deferred.js');\ntypeof module;\nexport const label = 'tool';",
            )
            .expect("hybrid module source parses");
            let facts = analyze_program_shallow(&parsed).runtime_facts;
            assert!(facts.has_cjs_interop);
            assert!(facts.has_own_cjs_export_surface);
            assert!(!facts.stable_primitive_exports.contains("label"));
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
            let facts = collect_cross_module_imported_purities(
                &artifact,
                &indexes,
                CrossModuleAnalysisOptions {
                    chunk_export_purity: &BTreeMap::new(),
                    chunk_analysis_options: &BTreeMap::new(),
                    graph_provenance: None,
                },
                &BTreeMap::new(),
                &BTreeSet::new(),
                &BTreeSet::new(),
                None,
            )
            .expect("cross-module analysis succeeds");

            let app_primitives = facts
                .primitives
                .get("static/app")
                .expect("app primitive fact set");
            assert!(!app_primitives.contains("copied"));
            assert!(!app_primitives.contains("forwarded"));
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
            let facts = collect_cross_module_imported_purities(
                &artifact,
                &indexes,
                CrossModuleAnalysisOptions {
                    chunk_export_purity: &assertions,
                    chunk_analysis_options: &BTreeMap::new(),
                    graph_provenance: None,
                },
                &BTreeMap::new(),
                &BTreeSet::new(),
                &BTreeSet::new(),
                None,
            )
            .expect("cross-module analysis succeeds");

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
