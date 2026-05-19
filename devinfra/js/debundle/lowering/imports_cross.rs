//! Emit cross-module + residual-entry imports for a moved module body.
//! Both fresh-local minting paths flow through `LoweringPlan` via
//! `disambiguate_import_locals_via_plan` (cross-module, Phase 7b)
//! and `disambiguate_residual_entry_import_locals_via_plan`
//! (residual-entry, Phase 7c).

use super::chunk_renames::{
    disambiguate_import_locals_via_plan, disambiguate_residual_entry_import_locals_via_plan,
};
use super::util::import_decl_for_plan;
use super::*;

/// Shared rename-context bundle for the import-disambiguation
/// helpers. Bundling the three rename-state pieces in one struct
/// keeps function signatures under clippy's argument-count
/// threshold and makes the intent ("this is the rename state for
/// the surrounding module emit") visible at the call site.
pub(super) struct RenameContext<'a> {
    pub(super) occupied: &'a mut BTreeSet<String>,
    pub(super) renames: &'a mut BTreeMap<String, String>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
}

pub(super) fn cross_module_imports_for_plan(
    from_file: &str,
    mut imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    factorization: &ChunkFactorization,
    ctx: &mut RenameContext<'_>,
) -> Result<Vec<ModuleItem>> {
    // Sort providers by their position in the factorization's
    // `linker_order` (a topological linearization of `I ∪ S`).
    // ECMA-262's depth-first link traversal visits each module's
    // `import` directives in source order, and the deepest leaf
    // reached first evaluates first. Putting the earliest-in-`L`
    // provider at the top of the import list steers the traversal
    // toward an `I ∪ S`-respecting evaluation order. See DESIGN.md
    // "Lemma 2".
    let mut providers: Vec<usize> = imports_by_provider.keys().copied().collect();
    providers.sort_by_key(|&idx| {
        factorization
            .linker_position(ModuleId(LogicalModuleIndex(idx)))
            .unwrap_or(usize::MAX)
    });
    let mut items = Vec::new();
    for provider_index in providers {
        let Some(bindings) = imports_by_provider.remove(&provider_index) else {
            continue;
        };
        let Some(provider) = factorization
            .analysis
            .logical_module(LogicalModuleIndex(provider_index))
        else {
            continue;
        };
        let resolved = disambiguate_import_locals_via_plan(
            &bindings,
            ctx.occupied,
            ctx.renames,
            ctx.chunk_top_level_mark,
        )?;
        items.push(import_decl_for_plan(
            from_file,
            &provider.target_file,
            &resolved,
        ));
    }
    Ok(items)
}

pub(super) fn residual_entry_imports_for_moved_body(
    module_id: &str,
    entry_file: &str,
    from_file: &str,
    imports: BTreeMap<String, EntryExport>,
    missing_exports: BTreeSet<String>,
    ctx: &mut RenameContext<'_>,
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
    let resolved = disambiguate_residual_entry_import_locals_via_plan(
        &imports,
        ctx.occupied,
        ctx.renames,
        ctx.chunk_top_level_mark,
    )?;
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
