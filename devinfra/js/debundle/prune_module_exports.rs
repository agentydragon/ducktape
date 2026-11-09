//! Drop dead named exports from emitted logical-module files.
//!
//! `materialize_logical_modules` exports every top-level binding a logical
//! module owns, but many are referenced only inside their own module — the
//! clearest case being the esbuild decorator scaffolding (`__defProp` /
//! `__getOwnPropDesc` / `__decorateClass`) that each mobx-decorated module
//! emits and uses only via its own `__decorateClass(...)` calls. Those land in
//! the module's `export { ... }` even though no other file imports them, so
//! they are dead exports that also surface as rename-queue holdouts that have
//! to be named for no downstream consumer.
//!
//! This pass removes a `FileRole::Module` file's `export { local as public }`
//! specifier when nothing anywhere in the bundle imports `public`. It is the
//! export-side analogue of `lowering::exports::trim_dead_named_specifiers`
//! (which trims dead *import* specifiers within a single body).
//!
//! Soundness:
//! - A name is "consumed" if any file's `import { name … }` or re-export
//!   `export { name … } from …` references it as the imported name. That set
//!   is a *superset* of "imported from this specific module", so keeping every
//!   consumed name never drops a live export. The check is name-based, not
//!   path-resolved: a name imported from *some* module is kept on *every*
//!   module that exports it — conservative, but never unsound.
//! - `import * as ns` / `export *` consume their target's whole export surface
//!   without naming bindings. Static module specifiers are always string
//!   literals, so each such target is resolved and its file is left untouched.
//! - Entry files (`FileRole::Entry`) are the chunk's public surface, consumed
//!   from outside the bundle; they are never pruned.
//! - Dynamic `import()` never targets a logical-module file (those are emitted
//!   inside their chunk, not as separately-loadable chunks), so dynamic
//!   imports are irrelevant here.
//! - `Source`-bodied files (no AST) are upstream-verbatim and skipped.

use binding_targets::module_export_name;
use std::collections::{BTreeMap, BTreeSet};

use artifact::paths::{join_module_path, module_path_dirname};
use artifact::pruning::{PruningChanges, pruning_receipt, request_identity};
use artifact::reports::{
    CandidateRewriteElision, CandidateRewriteReceipt, CandidateRewriteStage,
    CandidateSourceCorrespondence, OutputFileAddress,
};
use artifact::{ChunkBundle, FileRole, JsFile};
use source_identity::ChunkId;
use swc_ecma_ast::*;

pub fn prune_unimported_module_exports(
    bundle: &mut ChunkBundle,
    correspondence: &mut BTreeMap<ChunkId, CandidateSourceCorrespondence>,
) -> Vec<CandidateRewriteReceipt> {
    let mut modules = Vec::new();
    let mut snapshots = Vec::new();
    for chunk in &mut bundle.chunks {
        for file in &mut chunk.js.files {
            let path = file_abs_path(file);
            let role = file.metadata.role;
            let output = OutputFileAddress {
                chunk: chunk.chunk_id,
                file: file.path.clone(),
            };
            let Some(ast) = file.ast_mut() else { continue };
            let before = ast
                .module
                .body
                .iter()
                .enumerate()
                .filter_map(|(index, item)| {
                    request_identity(correspondence.get(&chunk.chunk_id), &output, index, item)
                })
                .collect::<Vec<_>>();
            snapshots.push((output, before));
            modules.push((path, role, &mut ast.module));
        }
    }
    let changes = prune_exports(&mut modules);
    let mut receipts = Vec::new();
    for ((output, before), mut changes) in snapshots.into_iter().zip(changes) {
        for (identity, issue) in before {
            let index = identity.item_index;
            let retained = changes
                .retained_specifiers
                .remove(&index)
                .unwrap_or_else(|| {
                    identity
                        .bindings
                        .iter()
                        .map(|binding| binding.specifier_index)
                        .collect()
                });
            receipts.push(pruning_receipt(
                &output,
                CandidateRewriteStage::DeadExportPruning,
                identity,
                issue,
                changes.item_positions[index],
                &retained,
                changes.elisions.remove(&index).unwrap_or_default(),
            ));
        }
    }
    for source in correspondence.values_mut() {
        source.apply_pruning_receipts(&mut receipts);
    }
    receipts
}

fn prune_exports(modules: &mut [(String, FileRole, &mut Module)]) -> Vec<PruningChanges> {
    let mut consumed = BTreeSet::new();
    let mut namespace_protected = BTreeSet::new();
    for (path, _, module) in modules.iter() {
        collect_consumers(module, path, &mut consumed, &mut namespace_protected);
    }
    modules
        .iter_mut()
        .map(|(path, role, module)| {
            if *role != FileRole::Module || namespace_protected.contains(path) {
                return PruningChanges::preserved(module.body.len());
            }
            let import_locals = collect_import_locals(module);
            prune_local_named_exports(module, &consumed, &import_locals)
        })
        .collect()
}

/// Local names bound by this module's `import` statements. A re-export of such
/// a binding (`import { x as a } from "…"; export { a };`) is an import+export
/// shim — pruning the `export` would orphan a now-unused import — so those are
/// exempted; only dead exports of *locally declared* bindings are pruned.
fn collect_import_locals(module: &Module) -> BTreeSet<String> {
    let mut locals = BTreeSet::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        for spec in &import.specifiers {
            let local = match spec {
                ImportSpecifier::Named(n) => &n.local,
                ImportSpecifier::Default(d) => &d.local,
                ImportSpecifier::Namespace(ns) => &ns.local,
            };
            locals.insert(local.sym.to_string());
        }
    }
    locals
}

/// Output-tree-rooted path of a file: `<chunk_dir>/<file.path>`, where the
/// chunk dir is the chunk's source path with the `.js` suffix dropped. Imports
/// resolve against this same coordinate system (see `resolve_specifier`).
fn file_abs_path(file: &JsFile) -> String {
    let source_path = file.metadata.source_path.as_str();
    let chunk_dir = source_path.strip_suffix(".js").unwrap_or(source_path);
    join_module_path(&[chunk_dir, file.path.as_str()])
}

/// Resolve a relative module specifier against the importing file's absolute
/// path, normalizing `.` / `..` so the result is directly comparable to a
/// target file's `file_abs_path`.
fn resolve_specifier(specifier: &str, importer_abs: &str) -> String {
    let dir = module_path_dirname(importer_abs);
    join_module_path(&[dir.as_str(), specifier])
}

fn collect_consumers(
    module: &Module,
    file_abs: &str,
    consumed: &mut BTreeSet<String>,
    namespace_protected: &mut BTreeSet<String>,
) {
    for item in &module.body {
        let ModuleItem::ModuleDecl(decl) = item else {
            continue;
        };
        match decl {
            ModuleDecl::Import(import) => {
                for spec in &import.specifiers {
                    match spec {
                        ImportSpecifier::Named(named) => {
                            consumed.insert(match &named.imported {
                                Some(name) => module_export_name(name),
                                None => named.local.sym.to_string(),
                            });
                        }
                        ImportSpecifier::Namespace(_) => {
                            if let Some(src) = import.src.value.as_str() {
                                namespace_protected.insert(resolve_specifier(src, file_abs));
                            }
                        }
                        // The default import binds the source's default
                        // export; this pass only prunes named exports.
                        ImportSpecifier::Default(_) => {}
                    }
                }
            }
            // `export { orig as public } from "src"` re-exports `orig` out of
            // `src`, so `src` must keep exporting it.
            ModuleDecl::ExportNamed(named) if named.src.is_some() => {
                for spec in &named.specifiers {
                    match spec {
                        ExportSpecifier::Named(n) => {
                            consumed.insert(module_export_name(&n.orig));
                        }
                        ExportSpecifier::Namespace(_) => {
                            if let Some(src) = &named.src
                                && let Some(spec) = src.value.as_str()
                            {
                                namespace_protected.insert(resolve_specifier(spec, file_abs));
                            }
                        }
                        ExportSpecifier::Default(_) => {}
                    }
                }
            }
            ModuleDecl::ExportAll(all) => {
                if let Some(src) = all.src.value.as_str() {
                    namespace_protected.insert(resolve_specifier(src, file_abs));
                }
            }
            _ => {}
        }
    }
}

fn prune_local_named_exports(
    module: &mut Module,
    consumed: &BTreeSet<String>,
    import_locals: &BTreeSet<String>,
) -> PruningChanges {
    let mut changes = PruningChanges::default();
    let mut item_index = 0;
    let mut after_index = 0;
    module.body.retain_mut(|item| {
        let index = item_index;
        item_index += 1;
        let mut keep = true;
        if let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item
            && named.src.is_none()
        {
            let mut retained = Vec::new();
            let mut elisions = Vec::new();
            let mut specifier_index = 0;
            named.specifiers.retain(|spec| {
                let spec_index = specifier_index;
                specifier_index += 1;
                let keep = match spec {
                    ExportSpecifier::Named(named) => {
                        let local = module_export_name(&named.orig);
                        let public = named
                            .exported
                            .as_ref()
                            .map_or_else(|| local.clone(), module_export_name);
                        let keep = import_locals.contains(&local) || consumed.contains(&public);
                        if !keep && let ModuleExportName::Ident(local) = &named.orig {
                            elisions.push(CandidateRewriteElision::DeadLocalExport {
                                local: local.to_id(),
                                exported_name: public,
                            });
                        }
                        keep
                    }
                    _ => true,
                };
                if keep {
                    retained.push(spec_index);
                }
                keep
            });
            keep = !named.specifiers.is_empty();
            changes.retained_specifiers.insert(index, retained);
            changes.elisions.insert(index, elisions);
        }
        changes.item_positions.push(keep.then_some(after_index));
        if keep {
            after_index += 1;
        }
        keep
    });
    changes
}

#[cfg(test)]
mod tests {
    use js_ast::parse_js_module;

    use super::*;

    #[test]
    fn string_export_names_do_not_collapse_to_empty() {
        js_ast::with_swc_globals(|| {
            let parsed =
                parse_js_module("surrogate.js", r#"const a = 1; export { a as "\ud800" };"#)
                    .unwrap();
            let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) = &parsed.module.body[1]
            else {
                panic!("named export");
            };
            let ExportSpecifier::Named(specifier) = &export.specifiers[0] else {
                panic!("named specifier");
            };
            assert!(!module_export_name(specifier.exported.as_ref().unwrap()).is_empty());
        });
    }

    fn pruned_modules(files: &[(&str, &str, FileRole)]) -> Vec<(String, Module)> {
        let mut modules: Vec<_> = files
            .iter()
            .map(|(path, source, _)| {
                (
                    (*path).to_string(),
                    parse_js_module(path, source).unwrap().module,
                )
            })
            .collect();
        let mut views: Vec<_> = modules
            .iter_mut()
            .zip(files)
            .map(|((path, module), (_, _, role))| (format!("c/{path}"), *role, module))
            .collect();
        let _ = prune_exports(&mut views);
        modules
    }

    fn emitted_exports(modules: &[(String, Module)], file_path: &str) -> Vec<String> {
        let (_, module) = modules
            .iter()
            .find(|(path, _)| path == file_path)
            .expect("file");
        let mut names = Vec::new();
        for item in &module.body {
            if let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item
                && named.src.is_none()
            {
                for spec in &named.specifiers {
                    if let ExportSpecifier::Named(n) = spec {
                        names.push(match &n.exported {
                            Some(name) => module_export_name(name),
                            None => module_export_name(&n.orig),
                        });
                    }
                }
            }
        }
        names.sort();
        names
    }

    #[test]
    fn drops_module_internal_only_export() {
        js_ast::with_swc_globals(|| {
            // `mod.js` owns `used` (imported by the entry) and `internalOnly`
            // (referenced only inside `mod.js`). The dead export is dropped.
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import { used } from \"./mod.js\";\nconsole.log(used);\n",
                    FileRole::Entry,
                ),
                (
                    "mod.js",
                    "const internalOnly = 1;\nconst used = internalOnly + 1;\nexport { internalOnly, used };\n",
                    FileRole::Module,
                ),
            ]);
            assert_eq!(
                emitted_exports(&modules, "mod.js"),
                vec!["used".to_string()]
            );
        });
    }

    #[test]
    fn keeps_export_consumed_via_alias_import() {
        js_ast::with_swc_globals(|| {
            // `import { used as u }` still consumes the source name `used`.
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import { used as u } from \"./mod.js\";\nconsole.log(u);\n",
                    FileRole::Entry,
                ),
                (
                    "mod.js",
                    "const used = 1;\nexport { used };\n",
                    FileRole::Module,
                ),
            ]);
            assert_eq!(
                emitted_exports(&modules, "mod.js"),
                vec!["used".to_string()]
            );
        });
    }

    #[test]
    fn removes_emptied_export_block() {
        js_ast::with_swc_globals(|| {
            let modules = pruned_modules(&[(
                "mod.js",
                "const a = 1;\nconst b = 2;\nexport { a, b };\n",
                FileRole::Module,
            )]);
            assert!(emitted_exports(&modules, "mod.js").is_empty());
            // The whole `export { ... }` ModuleItem is gone, not left empty.
            let has_export = modules[0]
                .1
                .body
                .iter()
                .any(|i| matches!(i, ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(_))));
            assert!(!has_export, "empty export block should be removed");
        });
    }

    #[test]
    fn never_prunes_entry_file() {
        js_ast::with_swc_globals(|| {
            // An entry's exports are the chunk's public surface (consumed
            // outside the modules); they are never pruned even if unimported.
            let modules = pruned_modules(&[(
                "entry.js",
                "const publicApi = 1;\nexport { publicApi };\n",
                FileRole::Entry,
            )]);
            assert_eq!(
                emitted_exports(&modules, "entry.js"),
                vec!["publicApi".to_string()]
            );
        });
    }

    #[test]
    fn keeps_namespace_imported_module_exports() {
        js_ast::with_swc_globals(|| {
            // A namespace import accesses any export by property, so every
            // export of the target module is protected.
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import * as ns from \"./mod.js\";\nconsole.log(ns);\n",
                    FileRole::Entry,
                ),
                (
                    "mod.js",
                    "const a = 1;\nconst b = 2;\nexport { a, b };\n",
                    FileRole::Module,
                ),
            ]);
            assert_eq!(
                emitted_exports(&modules, "mod.js"),
                vec!["a".to_string(), "b".to_string()]
            );
        });
    }

    #[test]
    fn keeps_reexported_name() {
        js_ast::with_swc_globals(|| {
            // The entry re-exports `inner` out of `mod.js` via `export … from`;
            // the source must keep exporting it.
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "export { inner } from \"./mod.js\";\n",
                    FileRole::Entry,
                ),
                (
                    "mod.js",
                    "const inner = 1;\nconst dead = 2;\nexport { inner, dead };\n",
                    FileRole::Module,
                ),
            ]);
            assert_eq!(
                emitted_exports(&modules, "mod.js"),
                vec!["inner".to_string()]
            );
        });
    }

    #[test]
    fn keeps_export_consumed_by_sibling_module() {
        js_ast::with_swc_globals(|| {
            // Consumption by a sibling logical module (not the entry) keeps the
            // export alive too.
            let modules = pruned_modules(&[
                (
                    "a.js",
                    "import { shared } from \"./b.js\";\nexport const useShared = () => shared;\n",
                    FileRole::Module,
                ),
                (
                    "b.js",
                    "const shared = 1;\nconst priv = 2;\nexport { shared, priv };\n",
                    FileRole::Module,
                ),
            ]);
            assert_eq!(
                emitted_exports(&modules, "b.js"),
                vec!["shared".to_string()]
            );
        });
    }

    #[test]
    fn keeps_reexported_imported_binding_shim() {
        js_ast::with_swc_globals(|| {
            // `mod.js` re-imports a vendor binding and re-exports it under a
            // readable name. Even with no in-modules consumer the export is
            // kept: pruning it would orphan the now-unused import. Only dead
            // exports of *locally declared* bindings (`dead`) are pruned.
            let modules = pruned_modules(&[(
                "mod.js",
                "import { x as a } from \"./vendor.js\";\nconst dead = 1;\nexport { a, dead };\n",
                FileRole::Module,
            )]);
            assert_eq!(emitted_exports(&modules, "mod.js"), vec!["a".to_string()]);
        });
    }

    #[test]
    fn receipt_coordinates_preserve_import_shims_and_request_order() {
        js_ast::with_swc_globals(|| {
            let mut module = parse_js_module(
                "module.js",
                r#"
                import { imported } from "./foreign.js";
                const dead = 0;
                const alive = 1;
                export { dead as unused, alive as used, imported as bridge };
                export { value as alias } from "./foreign.js";
                export * from "./star.js";
                export { dead as extra };
                import "./last.js";
            "#,
            )
            .unwrap()
            .module;
            let imports = collect_import_locals(&module);
            let consumed = BTreeSet::from(["used".to_owned()]);
            let changes = prune_local_named_exports(&mut module, &consumed, &imports);
            assert_eq!(
                changes.item_positions,
                vec![
                    Some(0),
                    Some(1),
                    Some(2),
                    Some(3),
                    Some(4),
                    Some(5),
                    None,
                    Some(6)
                ]
            );
            assert_eq!(changes.retained_specifiers[&3], vec![1, 2]);
            assert!(
                matches!(changes.elisions[&3].as_slice(), [CandidateRewriteElision::DeadLocalExport { exported_name, .. }] if exported_name == "unused")
            );
            assert!(
                matches!(changes.elisions[&6].as_slice(), [CandidateRewriteElision::DeadLocalExport { exported_name, .. }] if exported_name == "extra")
            );
            assert!(
                matches!(&module.body[4], ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_some())
            );
            assert!(matches!(
                &module.body[5],
                ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_))
            ));
            assert!(matches!(
                &module.body[6],
                ModuleItem::ModuleDecl(ModuleDecl::Import(_))
            ));
        });
    }
}
