//! Emit cross-module + residual-entry imports for a moved module body.
//! Both call `disambiguate_*_import_locals` (`imports/import_emit.rs`), which mint
//! fresh locals through the consuming plan's import ledger
//! (`RenameLedger::mint`, seeded with the body's binding names); the
//! minted `original → fresh` renames are submitted into the same ledger
//! (scope: that plan's `Module`, origin: `ImportInduced`) and applied
//! from the sealed projection in `prepare_module_emission`.

use super::import_emit::{
    disambiguate_import_locals, disambiguate_residual_entry_import_locals, import_decl_for_plan,
};
use crate::imports::EntryExport;
use crate::rename_ledger::{RenameIntent, RenameLedger, RenameOrigin, RenameScope};
use analysis::{ModuleId, top_level_id};
use anyhow::{Result, bail};
use binding_targets::declaration_name_strings;
use gate::AssignmentAnalysis;
use js_ast::import_decl_module_item;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::*;

pub(crate) const CROSS_MODULE_IMPORT_CONTRIBUTOR: &str = "cross-module import-local disambiguation";
pub(crate) const RESIDUAL_ENTRY_IMPORT_CONTRIBUTOR: &str =
    "residual-entry import-local disambiguation";

/// Where one plan's import-local mint renames go: the per-plan ledger
/// plus the scope/key context its intents need.
pub(crate) struct ImportLocalRenameSink<'a> {
    pub(crate) module: ModuleId,
    pub(crate) chunk_top_level_mark: swc_common::Mark,
    pub(crate) ledger: &'a mut RenameLedger,
}

impl ImportLocalRenameSink<'_> {
    fn scope(&self) -> RenameScope {
        RenameScope::Module(self.module)
    }

    /// Submit one minting pass's `original → fresh` renames as
    /// `Module`-scope `ImportInduced` intents for the consuming plan.
    fn submit(&mut self, renames: BTreeMap<String, String>, contributor: &'static str) {
        for (original, fresh) in renames {
            self.ledger.submit(RenameIntent {
                scope: RenameScope::Module(self.module),
                from: top_level_id(&original, self.chunk_top_level_mark),
                to: fresh.as_str().into(),
                origin: RenameOrigin::ImportInduced { contributor },
            });
        }
    }
}

/// Build one `import { … } from "./<provider>.js";` per provider,
/// tagged with the provider's `ModuleId`. UNSORTED — the caller
/// merges these with phantom side-effect imports and the
/// residual-entry import and orders the whole list through the
/// verified `EsmImportSchedule` retained by assignment analysis (the same
/// concrete target lists checked by the realizability gate), so the
/// emitted order and the gate's predicted order cannot diverge.
///
/// Providers are processed in ascending index order so import-local
/// minting (`disambiguate_import_locals`) stays deterministic and
/// independent of the final emitted order.
pub(crate) fn cross_module_imports_for_plan(
    from_file: &str,
    imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    assignment_analysis: &AssignmentAnalysis,
    sink: &mut ImportLocalRenameSink<'_>,
) -> Vec<(ModuleId, ModuleItem)> {
    imports_by_provider
        .into_iter()
        .filter_map(|(provider_index, bindings)| {
            assignment_analysis
                .inputs()
                .planned_module(ModuleId(provider_index))
                .map(|provider| {
                    let mut minted = BTreeMap::new();
                    let scope = sink.scope();
                    let resolved =
                        disambiguate_import_locals(&bindings, sink.ledger, scope, &mut minted);
                    sink.submit(minted, CROSS_MODULE_IMPORT_CONTRIBUTOR);
                    (
                        ModuleId(provider_index),
                        import_decl_for_plan(
                            from_file,
                            &provider.target_file,
                            &resolved,
                            sink.chunk_top_level_mark,
                        ),
                    )
                })
        })
        .collect()
}

/// Emit side-effect imports for canonical C/I targets lacking binding imports.
/// The caller sorts these together with binding imports using the concrete
/// retained schedule. Entry is a valid target too; importing the active root
/// is an ESM traversal no-op but must still appear in the verified target list.
pub(crate) fn phantom_side_effect_imports(
    from_file: &str,
    entry_file: &str,
    phantom_providers: BTreeSet<usize>,
    assignment_analysis: &AssignmentAnalysis,
) -> Vec<(ModuleId, ModuleItem)> {
    let entry = assignment_analysis
        .assignment()
        .entry_destination()
        .module_id();
    phantom_providers
        .into_iter()
        .map(|provider_index| {
            let module = ModuleId(provider_index);
            let target_file = if module == entry {
                entry_file
            } else {
                &assignment_analysis
                    .inputs()
                    .planned_module(module)
                    .expect("scheduled nonentry module has an emitted plan")
                    .target_file
            };
            let source = super::import_emit::relative_source(from_file, target_file);
            (module, import_decl_module_item(Vec::new(), &source))
        })
        .collect()
}

pub(crate) fn residual_entry_imports_for_moved_body(
    module_id: &str,
    entry_file: &str,
    from_file: &str,
    imports: BTreeMap<String, EntryExport>,
    missing_exports: BTreeSet<String>,
    sink: &mut ImportLocalRenameSink<'_>,
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
            "materialize_logical_modules: moved module {module_id} references residual entry binding(s) {} that are not exported by entry. This is an internal invariant violation in `auto_grown_residual_exports` — the export-growth pass should have surfaced these names before per-module emission. Report with the chunk's `dependency_graph.json.gz`.",
            missing_exports.into_iter().collect::<Vec<_>>().join(", "),
        );
    }
    if imports.is_empty() {
        return Ok(Vec::new());
    }
    let mut minted = BTreeMap::new();
    let scope = sink.scope();
    let resolved =
        disambiguate_residual_entry_import_locals(&imports, sink.ledger, scope, &mut minted);
    sink.submit(minted, RESIDUAL_ENTRY_IMPORT_CONTRIBUTOR);
    Ok(vec![import_decl_for_plan(
        from_file,
        entry_file,
        &resolved,
        sink.chunk_top_level_mark,
    )])
}

pub(crate) fn collect_entry_exports_by_original_local(
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
                for final_local in declaration_name_strings(&export_decl.decl) {
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

pub(crate) fn module_export_ident_name(name: &ModuleExportName) -> Option<String> {
    match name {
        ModuleExportName::Ident(ident) => Some(ident.sym.to_string()),
        ModuleExportName::Str(_) => None,
    }
}

pub(crate) fn named_export_public_ident_name(
    exported: &Option<ModuleExportName>,
    fallback: &str,
) -> Option<String> {
    match exported {
        Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
        Some(ModuleExportName::Str(_)) => None,
        None => Some(fallback.to_string()),
    }
}

pub(crate) fn final_module_exports(
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
