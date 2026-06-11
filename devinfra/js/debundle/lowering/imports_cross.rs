//! Emit cross-module + residual-entry imports for a moved module body.
//! Both call `disambiguate_*_import_locals` (import_emit.rs) to mint fresh
//! locals when names collide with already-occupied bindings.

use super::import_emit::{
    disambiguate_import_locals, disambiguate_residual_entry_import_locals, import_decl_for_plan,
};
use super::*;

/// Build one `import { … } from "./<provider>.js";` per provider,
/// tagged with the provider's `ModuleId`. UNSORTED — the caller
/// merges these with phantom side-effect imports and the
/// residual-entry import and orders the whole list through the
/// shared `EsmImportOrder::sort_module_imports` (the same rule the
/// realizability gate's evaluation simulator applies), so the
/// emitted order and the gate's predicted order cannot diverge.
///
/// Providers are processed in ascending index order so import-local
/// minting (`disambiguate_import_locals`) stays deterministic and
/// independent of the final emitted order.
pub(super) fn cross_module_imports_for_plan(
    from_file: &str,
    imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    factorization: &ChunkFactorization,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> Vec<(ModuleId, ModuleItem)> {
    imports_by_provider
        .into_iter()
        .filter_map(|(provider_index, bindings)| {
            factorization
                .analysis
                .logical_module(LogicalModuleIndex(provider_index))
                .map(|provider| {
                    let resolved = disambiguate_import_locals(&bindings, occupied, renames);
                    (
                        ModuleId(LogicalModuleIndex(provider_index)),
                        import_decl_for_plan(from_file, &provider.target_file, &resolved),
                    )
                })
        })
        .collect()
}

/// Build `import "./<provider>.js";` (side-effect-only, no
/// specifiers) for each phantom provider, tagged with the provider's
/// `ModuleId`. The phantom imports surface ducktape's at-init
/// promotion constraints as real ESM imports so the linker's
/// DFS visits the providers as dependencies of this module — needed
/// because the actual bindings being read live in residual function
/// decls (e.g. a top-level helper → another helper → reads a
/// logger binding), so the per-module emit path never adds an explicit
/// import for the logger binding in the consuming module on its own.
///
/// UNSORTED — the caller merges these with the cross-module binding
/// imports and the residual-entry import and orders the whole list
/// through the shared `EsmImportOrder::sort_module_imports`,
/// mirroring the gate simulator's neighbor order exactly.
pub(super) fn phantom_side_effect_imports(
    from_file: &str,
    phantom_providers: BTreeSet<usize>,
    factorization: &ChunkFactorization,
) -> Vec<(ModuleId, ModuleItem)> {
    phantom_providers
        .into_iter()
        .filter_map(|provider_index| {
            let provider = factorization
                .analysis
                .logical_module(LogicalModuleIndex(provider_index))?;
            let source = super::import_emit::relative_source(from_file, &provider.target_file);
            Some((
                ModuleId(LogicalModuleIndex(provider_index)),
                import_decl_module_item(Vec::new(), &source),
            ))
        })
        .collect()
}

pub(super) fn residual_entry_imports_for_moved_body(
    module_id: &str,
    entry_file: &str,
    from_file: &str,
    imports: BTreeMap<String, EntryExport>,
    missing_exports: BTreeSet<String>,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> Result<Vec<ModuleItem>> {
    if !missing_exports.is_empty() {
        // Defense-in-depth: `auto_grown_residual_exports` is supposed
        // to make sure every cross-destination read into residual
        // entry resolves to an exported binding. If a name slips
        // through (the binding is declared in the chunk but never
        // got an entry export), that's an internal materializer
        // invariant violation rather than a user-facing spec error
        // — bail with the offending names so the bug stays
        // diagnosable.
        bail!(
            "materialize_logical_modules: moved module {module_id} references residual entry binding(s) {} that are not exported by entry. This is an internal invariant violation in `auto_grown_residual_exports` — the export-growth pass should have surfaced these names before per-module emission. Report with the chunk's `owner_graph.json`.",
            missing_exports.into_iter().collect::<Vec<_>>().join(", "),
        );
    }
    if imports.is_empty() {
        return Ok(Vec::new());
    }
    let resolved = disambiguate_residual_entry_import_locals(&imports, occupied, renames);
    Ok(vec![import_decl_for_plan(from_file, entry_file, &resolved)])
}

pub(super) fn collect_entry_exports_by_original_local(
    entry_body: &[ModuleItem],
    entry_renames: &BTreeMap<String, String>,
    chunk_top_level_mark: swc_common::Mark,
) -> HashMap<Id, EntryExport> {
    let final_to_original = entry_renames
        .iter()
        .map(|(original, final_name)| (final_name.clone(), original.clone()))
        .collect::<BTreeMap<_, _>>();
    let mut exports = HashMap::<Id, EntryExport>::new();
    for item in entry_body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none() => {
                for specifier in &named.specifiers {
                    let ExportSpecifier::Named(specifier) = specifier else {
                        continue;
                    };
                    let Some(final_local) = module_export_ident_name(&specifier.orig) else {
                        continue;
                    };
                    let Some(exported_name) =
                        named_export_public_ident_name(&specifier.exported, &final_local)
                    else {
                        continue;
                    };
                    let original = final_to_original
                        .get(&final_local)
                        .cloned()
                        .unwrap_or_else(|| final_local.clone());
                    exports
                        .entry(top_level_id(&original, chunk_top_level_mark))
                        .or_insert(EntryExport {
                            local_name: final_local,
                            exported_name,
                        });
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for final_local in declaration_names(&export_decl.decl) {
                    let original = final_to_original
                        .get(&final_local)
                        .cloned()
                        .unwrap_or_else(|| final_local.clone());
                    exports
                        .entry(top_level_id(&original, chunk_top_level_mark))
                        .or_insert(EntryExport {
                            local_name: final_local.clone(),
                            exported_name: final_local,
                        });
                }
            }
            _ => {}
        }
    }
    exports
}

pub(super) fn module_export_ident_name(name: &ModuleExportName) -> Option<String> {
    match name {
        ModuleExportName::Ident(ident) => Some(ident.sym.to_string()),
        ModuleExportName::Str(_) => None,
    }
}

pub(super) fn named_export_public_ident_name(
    exported: &Option<ModuleExportName>,
    fallback: &str,
) -> Option<String> {
    match exported {
        Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
        Some(ModuleExportName::Str(_)) => None,
        None => Some(fallback.to_string()),
    }
}

pub(super) fn final_module_exports(
    exports: &BTreeMap<String, String>,
    local_renames: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    exports
        .iter()
        .map(|(local, exported)| {
            (
                local_renames
                    .get(local)
                    .cloned()
                    .unwrap_or_else(|| local.clone()),
                exported.clone(),
            )
        })
        .collect()
}
