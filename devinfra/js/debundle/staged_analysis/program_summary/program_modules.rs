//! Per-module purity facts, export value sources and static requests gathered
//! from the prepared artifact.

use super::chunk_allowed;
use super::static_dependencies::resolved_target_is_entry;
use analysis_model::{PrimKinds, StableExportValue};
use analysis_vocabulary::inputs::PrimitiveExportCell;
use artifact::reports::{ImportSpecifierKind, StaticModuleRequest};
use artifact::{ArtifactIndexes, ChunkBundle};
use cross_module_purity::export_primitives::ExportValueSource;
use cross_module_purity::module_facts::ModulePurityFacts;
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::{Decl, ModuleDecl, ModuleItem, Pat};
use value_flow::program_bindings::ResolvedImport;

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
/// Oracle inputs assembled from the prepared syntax and matching manifests.
pub(super) struct ProgramModules<'a> {
    pub(super) modules: BTreeMap<String, ModulePurityFacts<'a>>,
    pub(super) export_value_sources: BTreeMap<(String, String), ExportValueSource>,
    pub(super) lexically_closed_modules: BTreeSet<String>,
    pub(super) static_requests: BTreeMap<String, (ChunkId, String, Vec<StaticModuleRequest>)>,
}

pub(super) fn program_modules<'a>(
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
