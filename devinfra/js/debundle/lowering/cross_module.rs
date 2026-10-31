//! Program-level cross-module purity pass: resolve every chunk's imports
//! against the artifact and run the purity fixpoint
//! (`analysis::cross_module_purity`). Output feeds each chunk's
//! `AnalysisInputs::imported_purities` so the per-chunk classifier can see
//! through calls to imported functions instead of bailing `unknown_call`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;
use std::sync::Arc;

use analysis::analysis_inputs::{
    ArgumentInputEvidence, ArgumentInputRef, CallableSummary, CommutingEndpoint,
    CommutingStatementsRef, NativeInputKind, NativeIntegrity, NativeOperationInputEvidence,
    NativeOperationInputRef, NativeOperationSelectorProvenance, ResolvedArgumentInput,
    ResolvedBindingValueInput, ResolvedCommutingStatements, ResolvedNativeOperationInput,
    SelectedBindingValueInput, SelectedNativeOperationInput, ValueInputEvidence, ValueInputRef,
};
use analysis::cross_module_purity::{
    CallableSummaryInputs, ExportValueSource, ModulePurityFacts, ResolvedImport, StableExportValue,
    resolve_asserted_callback_factory_members, resolve_asserted_fluent_bindings,
    resolve_asserted_inline_callback_factories, resolve_asserted_member_purities,
    resolve_asserted_namespace_independent_members, resolve_asserted_no_reentry_bindings,
    resolve_asserted_no_reentry_callbacks, resolve_asserted_no_reentry_members,
    resolve_asserted_no_reentry_record_members, resolve_asserted_pure_new_bindings,
    resolve_asserted_retained_callback_constructors, resolve_asserted_retained_callback_factories,
    resolve_asserted_retained_callback_factory_members, resolve_export_values,
    resolve_imported_callable_summaries, resolve_imported_purities,
};
use analysis::facts::abstract_domain::value::{PrimKinds, Value};
use analysis::purity::Purity;
use anyhow::{Context, Result, bail, ensure};
use artifact::reports::{ExportAliasRecord, ImportRecord, ImportSpecifierKind};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId};
use binding_targets::binding_name_strings;
use graph_provenance::{CurrentRoots, GraphProvenance};
use js_ast::{ParsedJsModule, source_unit_ordinal_for_statement};
use program_analysis::analyze_program_shallow;
use selector_outcome::Outcome as SelectorOutcome;
use selector_resolve::{Chunk as SelectorChunk, EntityIndex};
use source_identity::{SourceUnitOrdinal, StatementId};
use source_match::source_match_claim_member_selectors;
use spec::{ChunkExportPurity, DependencyGraphOptions, SourceUnitSelector};
use swc_common::Spanned;
use swc_ecma_ast::{Decl, Id, Module, ModuleDecl, ModuleItem, Pat, Stmt, VarDeclarator};

use super::materialize::ResolvedMemberSite;

/// Resolve a one-binding `source_match` through the selector engine and
/// return the declaring statement the template matched, with the source
/// binding it declares there. The engine canonicalizes a repeated `var` to
/// its first declaration site (SPEC.md § Repeated `var` declarations), which
/// is right for a member claim (assigning the binding claims every site) but
/// not for a source-unit endpoint: the author's template pins one statement,
/// and that statement's declarator is the selected unit. The matched site is
/// read back from the matcher's candidates for the resolved binding; a
/// template matching several of the binding's sites is rejected.
fn select_one_declared_binding(
    selector_chunk: &SelectorChunk<'_>,
    request_id: &str,
    claim: &spec::SourceMatchClaim,
) -> Result<(usize, String)> {
    ensure!(
        claim.bindings.len() == 1,
        "{request_id}.source_match must select exactly one declared binding"
    );
    let spec::SourceMatchBinding::Local(local) = &claim.bindings[0] else {
        bail!("{request_id}.source_match does not accept detailed name/rename payloads");
    };
    let mut selectors = source_match_claim_member_selectors(request_id, claim)?;
    ensure!(
        selectors.len() == 1,
        "{request_id}.source_match must produce one selector"
    );
    let selector = selectors.pop().expect("one selector was checked");
    ensure!(
        selector
            .parsed_selector
            .declared_binding_names()
            .iter()
            .any(|name| name == local),
        "{request_id}.source_match binding `{local}` must be declared by its pattern"
    );
    let parsed_selector = selector.parsed_selector.clone();
    let selector_module = spec_resolution::spec_module(
        request_id.to_owned(),
        vec![spec_resolution::member_from_source_match(
            selector.export_name,
            selector.parsed_selector,
        )],
        Vec::new(),
    );
    let resolution = selector_chunk.resolve(&[selector_module])?;
    let outcome = resolution
        .outcome(0, EntityIndex::Member(0))
        .with_context(|| format!("{request_id}.source_match produced no selector outcome"))?;
    let (representative, binding) = match &outcome.outcome {
        SelectorOutcome::Resolved {
            body_index,
            binding: Some(binding),
            ..
        } => (*body_index, binding.clone()),
        other => bail!(
            "{request_id}.source_match must select exactly one declared binding; selector result: {other:?}"
        ),
    };
    let mut sites = selector_chunk
        .matcher()
        .member_candidates(request_id, &parsed_selector)?
        .into_iter()
        .filter(|matched| matched.binding.binding_name == binding)
        .map(|matched| matched.body_idx)
        .collect::<BTreeSet<_>>();
    match sites.len() {
        1 => Ok((sites.pop_first().expect("one site"), binding)),
        0 => bail!(
            "{request_id}.source_match resolved `{binding}` to statement #{representative} but its template matches none of the binding's declaration sites"
        ),
        _ => bail!(
            "{request_id}.source_match matches `{binding}` at several declaration sites (statements {}); a source-unit endpoint must pin one",
            sites
                .iter()
                .map(|site| format!("#{site}"))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

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
    stable_export_values: BTreeMap<String, StableExportValue>,
}

/// Output of the program-level purity pass, keyed by chunk name.
pub(super) struct CrossModulePurities {
    /// Per-chunk imported-binding verdicts (`AnalysisInputs::imported_purities`).
    pub(super) bindings: BTreeMap<String, BTreeMap<String, Arc<Purity>>>,
    /// Per-chunk imported-binding values (`AnalysisInputs::imported_values`).
    pub(super) values: BTreeMap<String, BTreeMap<String, Value>>,
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
    /// Source-resolved native operation premises for the provider initializer.
    pub(super) native_operation_inputs: BTreeMap<String, Vec<ResolvedNativeOperationInput>>,
    pub(super) stable_set_spreads:
        BTreeMap<String, BTreeMap<u32, analysis::analysis_inputs::StableSetIterableUse>>,
    pub(super) argument_inputs: BTreeMap<String, Vec<ResolvedArgumentInput>>,
    /// Source-resolved commuting source-unit pairs, keyed by their chunk.
    pub(super) commuting_statements: BTreeMap<String, Vec<ResolvedCommutingStatements>>,
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
/// by emission. Vendor-swapped chunks are traversed like any other: their
/// bundled bodies stand for the replacement (SPEC.md § Vendor swap equivalence).
pub(super) fn selected_static_import_closure(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    selected_chunk_ids: &[String],
) -> Result<BTreeSet<ChunkId>> {
    let mut closure = BTreeSet::new();
    let mut pending = Vec::new();
    for chunk_id in selected_chunk_ids {
        let interned = artifact
            .chunk_table
            .get(chunk_id)
            .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
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
/// A vendor-swapped provider (full, partial or bundled partial) is analyzed
/// from its bundled body: the swap promises the replacement behaves like it
/// (SPEC.md § Vendor swap equivalence).
pub(super) struct CrossModuleAnalysisOptions<'a> {
    pub(super) chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub(super) chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub(super) graph_provenance: Option<&'a GraphProvenance>,
    pub(super) explicit_binding_destinations: &'a BTreeMap<String, HashMap<Id, usize>>,
    pub(super) resolved_member_sites: &'a BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    /// Export name → replacement package of every vendor-swapped export, by
    /// defining chunk; summaries derived from those bodies name the package.
    pub(super) replacement_packages: &'a BTreeMap<ChunkId, BTreeMap<String, String>>,
    pub(super) predefined_globals: spec::PredefinedGlobals,
}

pub(super) fn collect_cross_module_imported_purities(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    options: CrossModuleAnalysisOptions<'_>,
    declared_pure_new_bindings: &BTreeMap<String, BTreeSet<String>>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<CrossModulePurities> {
    let CrossModuleAnalysisOptions {
        chunk_export_purity,
        chunk_analysis_options,
        graph_provenance,
        explicit_binding_destinations,
        resolved_member_sites,
        replacement_packages,
        predefined_globals,
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
    let mut local_export_values =
        BTreeMap::<(String, String), (PrimKinds, BTreeSet<String>)>::new();
    let mut export_value_sources = BTreeMap::<(String, String), ExportValueSource>::new();
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
        for (export, value) in stable_export_values {
            let key = (chunk_name.to_string(), export.clone());
            let (source, imported_export) = match value {
                StableExportValue::Local { kinds, imports } => {
                    local_export_values.insert(key, (*kinds, imports.clone()));
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
                    }],
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
            export_value_sources.insert((chunk, export), ExportValueSource { kinds, copies });
        }
    }
    let export_values = resolve_export_values(export_value_sources);
    let value_inputs = resolve_value_inputs(
        artifact,
        &modules,
        chunk_analysis_options,
        graph_provenance,
        chunk_filter,
    )?;
    let native_operation_inputs = resolve_native_operation_inputs(
        artifact,
        &modules,
        chunk_analysis_options,
        graph_provenance,
        chunk_filter,
        resolved_member_sites,
    )?;
    let argument_inputs = resolve_argument_inputs(
        artifact,
        &modules,
        chunk_analysis_options,
        graph_provenance,
        chunk_filter,
    )?;
    let commuting_statements = resolve_commuting_statements(
        artifact,
        &modules,
        chunk_analysis_options,
        graph_provenance,
        chunk_filter,
        resolved_member_sites,
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
            explicit_binding_destinations,
            argument_inputs: &argument_inputs,
            predefined_globals,
        },
    )?;
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
        values: modules
            .iter()
            .map(|(name, module)| (name.clone(), module.imported_values(&export_values)))
            .collect(),
        bindings: resolve_imported_purities(&modules, &asserted_pure, &mut callable_summaries),
        members: resolve_asserted_member_purities(&modules, &asserted_members),
        fluent: resolve_asserted_fluent_bindings(&modules, &asserted_fluent),
        constructors: resolve_asserted_pure_new_bindings(&modules, &asserted_pure_new),
        imported_callables: callable_summaries.imported_callables,
        native_integrity: callable_summaries.native_integrity,
        value_inputs: callable_summaries.value_inputs,
        native_operation_inputs: callable_summaries.native_operation_inputs,
        stable_set_spreads: callable_summaries.stable_set_spreads,
        argument_inputs: callable_summaries.argument_inputs,
        commuting_statements,
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
        let spec_path = author_input_spec_path(provenance)?;
        let module = Module {
            span: Default::default(),
            body: facts.body.to_vec(),
            shebang: None,
        };
        // Build the canonical structural index once for this provider. Each
        // authored input below contributes only a selector claim against it.
        let structural = analysis::facts::collect_chunk_source_facts(&module, None, |_| None);
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

fn resolve_native_operation_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, DependencyGraphOptions>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<BTreeMap<String, Vec<SelectedNativeOperationInput>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.native_operation_inputs.is_empty()
            && options.native_call_identity_inputs.is_empty()
        {
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
        let spec_path = author_input_spec_path(provenance)?;
        let module = Module {
            span: Default::default(),
            body: facts.body.to_vec(),
            shebang: None,
        };
        let structural = analysis::facts::collect_chunk_source_facts(&module, None, |_| None);
        let selector_chunk = SelectorChunk::new(chunk_name, &module, structural);
        let parser_input_file = provenance.resolve(
            &source.input_file.root,
            &source.input_file.path,
            &CurrentRoots::default(),
        )?;
        let contracts = options
            .native_operation_inputs
            .iter()
            .enumerate()
            .map(|(index, contract)| {
                (
                    index,
                    format!("chunk_analysis_options.{chunk_name}.native_operation_inputs[{index}]"),
                    contract.selector.clone(),
                    &contract.audited_source_sha256,
                    contract.note.clone(),
                    NativeInputKind::Operation(contract.operation),
                )
            })
            .chain(options.native_call_identity_inputs.iter().enumerate().map(
                |(index, contract)| {
                    (
                        options.native_operation_inputs.len() + index,
                        format!("chunk_analysis_options.{chunk_name}.native_call_identity_inputs[{index}]"),
                        SourceUnitSelector::SourceMatch(contract.source_match.clone()),
                        &contract.audited_source_sha256,
                        contract.note.clone(),
                        NativeInputKind::CallIdentity(contract.callee),
                    )
                },
            ));
        let mut chunk_inputs = Vec::with_capacity(
            options.native_operation_inputs.len() + options.native_call_identity_inputs.len(),
        );
        for (input_index, request_id, selector, audited_source_sha256, note, operation) in contracts
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
            let (body_index, selected_binding, selector_provenance) = match selector {
                SourceUnitSelector::SourceMatch(source_match) => {
                    let (body_index, binding) =
                        select_one_declared_binding(&selector_chunk, &request_id, &source_match)?;
                    (
                        body_index,
                        binding,
                        NativeOperationSelectorProvenance::SourceMatch(source_match),
                    )
                }
                SourceUnitSelector::ResolvedMember(member) => {
                    let sites = resolved_member_sites
                        .get(&(member.module.clone(), member.name.clone()))
                        .with_context(|| {
                            format!("{request_id}.resolved_member names no logical module member")
                        })?;
                    let mut provider_sites = sites
                        .iter()
                        .filter(|site| site.provider_chunk == *chunk_name);
                    let site = provider_sites.next().with_context(|| {
                        format!(
                            "{request_id}.resolved_member belongs to another provider, not {chunk_name}"
                        )
                    })?;
                    ensure!(
                        provider_sites.next().is_none(),
                        "{request_id}.resolved_member is ambiguous in provider {chunk_name}"
                    );
                    let declaration = site.declaration.as_ref().with_context(|| {
                        format!(
                            "{request_id}.resolved_member has no unique original local declaration"
                        )
                    })?;
                    (
                        declaration.body_index,
                        declaration.binding.clone(),
                        NativeOperationSelectorProvenance::ResolvedMember(member),
                    )
                }
            };
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
                spec_location: format!("{}:{request_id}", spec_path.display()),
                selector: selector_provenance,
                operation,
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: parser_input_file.to_string_lossy().into_owned(),
                audited_source_sha256: audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                declaration_location: None,
                initializer_location: None,
                note,
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

/// Resolve every `commuting_statements` pair to two distinct post-split source
/// units of its chunk. Resolution failures, ambiguity, a foreign provider, a
/// self-pair and a repeated unordered pair are spec errors naming the entry and
/// endpoint. Nothing consumes the resolved pairs yet.
fn resolve_commuting_statements(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, DependencyGraphOptions>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<BTreeMap<String, Vec<ResolvedCommutingStatements>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.commuting_statements.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.commuting_statements");
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
        let spec_path = author_input_spec_path(
            graph_provenance.context("commuting statements require graph provenance")?,
        )?;
        let module = Module {
            span: Default::default(),
            body: facts.body.to_vec(),
            shebang: None,
        };
        let structural = analysis::facts::collect_chunk_source_facts(&module, None, |_| None);
        let selector_chunk = SelectorChunk::new(chunk_name, &module, structural);
        let mut seen_pairs = BTreeMap::<(SourceUnitOrdinal, SourceUnitOrdinal), usize>::new();
        let mut chunk_pairs = Vec::with_capacity(options.commuting_statements.len());
        for (input_index, contract) in options.commuting_statements.iter().enumerate() {
            let request_id = format!("{prefix}[{input_index}]");
            let a = resolve_source_unit_endpoint(
                &selector_chunk,
                facts.body,
                chunk_name,
                &format!("{request_id}.a"),
                &contract.a,
                resolved_member_sites,
            )?;
            let b = resolve_source_unit_endpoint(
                &selector_chunk,
                facts.body,
                chunk_name,
                &format!("{request_id}.b"),
                &contract.b,
                resolved_member_sites,
            )?;
            ensure!(
                a.source_unit != b.source_unit,
                "{request_id} selects the same source unit {} at both endpoints",
                a.source_unit
            );
            let key = (
                a.source_unit.min(b.source_unit),
                a.source_unit.max(b.source_unit),
            );
            if let Some(earlier) = seen_pairs.insert(key, input_index) {
                bail!(
                    "{request_id} duplicates {prefix}[{earlier}]: both select source units {} and {}",
                    key.0,
                    key.1
                );
            }
            chunk_pairs.push(ResolvedCommutingStatements {
                reference: CommutingStatementsRef {
                    defining_chunk: chunk_name.clone(),
                    input_index,
                },
                spec_location: format!("{}:{request_id}", spec_path.display()),
                a,
                b,
                note: contract.note.clone(),
            });
        }
        resolved.insert(chunk_name.clone(), chunk_pairs);
    }
    Ok(resolved)
}

/// Resolve one endpoint selector to exactly one post-split source unit of
/// `chunk_name`, through the shared selector engine or an already resolved
/// member site.
fn resolve_source_unit_endpoint(
    selector_chunk: &SelectorChunk<'_>,
    body: &[ModuleItem],
    chunk_name: &str,
    endpoint_id: &str,
    selector: &SourceUnitSelector,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<CommutingEndpoint> {
    let (body_index, selected_binding) = match selector {
        SourceUnitSelector::SourceMatch(claim) if claim.bindings.is_empty() => {
            let parsed = spec_resolution::parse_anonymous_selector(
                endpoint_id,
                format!("<anonymous source_match in {endpoint_id}>"),
                &spec::AnonymousStatementSelector {
                    match_source: claim.match_source.clone(),
                    identifiers: claim.identifiers,
                    target_binding: None,
                },
            )?;
            let selector_module = spec_resolution::spec_module(
                endpoint_id.to_owned(),
                Vec::new(),
                vec![spec_resolution::anonymous_statement(0, parsed)],
            );
            let resolution = selector_chunk
                .resolve(&[selector_module])
                .with_context(|| format!("{endpoint_id}.source_match"))?;
            let outcome = resolution
                .outcome(0, EntityIndex::AnonymousStatement(0))
                .with_context(|| {
                    format!("{endpoint_id}.source_match produced no selector outcome")
                })?;
            match &outcome.outcome {
                SelectorOutcome::Resolved { body_index, .. } => (*body_index, None),
                other => bail!(
                    "{endpoint_id}.source_match must select exactly one anonymous statement; selector outcome: {} {other:?}",
                    other.kind().as_str()
                ),
            }
        }
        SourceUnitSelector::SourceMatch(claim) => {
            ensure!(
                claim.bindings.len() == 1,
                "{endpoint_id}.source_match must name at most one declared binding"
            );
            let (body_index, binding) =
                select_one_declared_binding(selector_chunk, endpoint_id, claim)?;
            (body_index, Some(binding))
        }
        SourceUnitSelector::ResolvedMember(member) => {
            let sites = resolved_member_sites
                .get(&(member.module.clone(), member.name.clone()))
                .with_context(|| {
                    format!("{endpoint_id}.resolved_member names no logical module member")
                })?;
            let mut provider_sites = sites
                .iter()
                .filter(|site| site.provider_chunk == chunk_name);
            let site = provider_sites.next().with_context(|| {
                format!(
                    "{endpoint_id}.resolved_member belongs to another provider, not {chunk_name}"
                )
            })?;
            ensure!(
                provider_sites.next().is_none(),
                "{endpoint_id}.resolved_member is ambiguous in provider {chunk_name}"
            );
            let declaration = site.declaration.as_ref().with_context(|| {
                format!("{endpoint_id}.resolved_member has no unique original local declaration")
            })?;
            (declaration.body_index, Some(declaration.binding.clone()))
        }
    };
    let item = body
        .get(body_index)
        .with_context(|| format!("{endpoint_id} selected a missing module item"))?;
    let first_ordinal = source_unit_ordinal_for_statement(body, StatementId(body_index));
    let declarator_offset = match (&selected_binding, split_var_declarators(item)) {
        (Some(binding), Some(declarators)) => declarators
            .iter()
            .position(|declarator| {
                binding_name_strings(&declarator.name)
                    .iter()
                    .any(|name| name == binding)
            })
            .with_context(|| {
                format!("{endpoint_id} selected binding `{binding}` outside its declaration")
            })?,
        _ => 0,
    };
    Ok(CommutingEndpoint {
        selector: selector.clone(),
        source_unit: SourceUnitOrdinal(first_ordinal.0 + declarator_offset),
        selected_binding,
    })
}

/// The declarators of a top-level variable declaration the analysis view
/// splits into several source units; `None` for every single-unit item.
fn split_var_declarators(item: &ModuleItem) -> Option<&[VarDeclarator]> {
    let decl = match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
        _ => return None,
    };
    match decl {
        Decl::Var(var) if var.decls.len() > 1 => Some(var.decls.as_slice()),
        _ => None,
    }
}

fn resolve_argument_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, DependencyGraphOptions>,
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
        let spec_path = author_input_spec_path(provenance)?;
        let module = Module {
            span: Default::default(),
            body: facts.body.to_vec(),
            shebang: None,
        };
        let source_facts = analysis::facts::collect_chunk_source_facts(&module, None, |_| None);
        let source_contradictions = source_facts.per_statement.clone();
        let selector_chunk = SelectorChunk::new(chunk_name, &module, source_facts);
        let parser_input_file = provenance.resolve(
            &source.input_file.root,
            &source.input_file.path,
            &CurrentRoots::default(),
        )?;
        let mut selected = Vec::new();
        for (input_index, contract) in options.argument_inputs.iter().enumerate() {
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
            let (body_index, selected_binding) =
                select_one_declared_binding(&selector_chunk, &request_id, &contract.source_match)?;
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
                spec_location: format!("{}:{request_id}", spec_path.display()),
                source_match: contract.source_match.clone(),
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: parser_input_file.to_string_lossy().into_owned(),
                audited_source_sha256: contract.audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                parameter_index: contract.parameter_index,
                exact_arity: contract.exact_arity,
                declaration_location: None,
                note: contract.note.clone(),
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

/// Return the original authored flat-spec or tree-config path recorded when
/// the graph was captured. Flat specs do not populate `config_path`; their
/// canonical `--spec` argument is retained in the provenance generation
/// command and is already the source of truth for replay.
fn author_input_spec_path(provenance: &GraphProvenance) -> Result<PathBuf> {
    if let Some(config_path) = &provenance.config_path {
        return Ok(provenance.roots.spec.join(config_path));
    }
    let spec_arg = provenance
        .generation_command
        .iter()
        .position(|argument| argument == "--spec")
        .and_then(|index| provenance.generation_command.get(index + 1))
        .context("graph provenance has no recorded flat spec path for author input evidence")?;
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
            let facts = collect_cross_module_imported_purities(
                &artifact,
                &indexes,
                CrossModuleAnalysisOptions {
                    chunk_export_purity: &BTreeMap::new(),
                    chunk_analysis_options: &BTreeMap::new(),
                    graph_provenance: None,
                    explicit_binding_destinations: &BTreeMap::new(),
                    resolved_member_sites: &BTreeMap::new(),
                    replacement_packages: &BTreeMap::new(),
                    predefined_globals: spec::PredefinedGlobals::Unknown,
                },
                &BTreeMap::new(),
                None,
            )
            .expect("cross-module analysis succeeds");

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
            let facts = collect_cross_module_imported_purities(
                &artifact,
                &indexes,
                CrossModuleAnalysisOptions {
                    chunk_export_purity: &assertions,
                    chunk_analysis_options: &BTreeMap::new(),
                    graph_provenance: None,
                    explicit_binding_destinations: &BTreeMap::new(),
                    resolved_member_sites: &BTreeMap::new(),
                    replacement_packages: &BTreeMap::new(),
                    predefined_globals: spec::PredefinedGlobals::Unknown,
                },
                &BTreeMap::new(),
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
