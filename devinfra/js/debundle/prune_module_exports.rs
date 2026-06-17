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

use std::collections::BTreeSet;

use artifact::{ChunkBundle, FileRole, JsFile, join_module_path, module_path_dirname};
use swc_ecma_ast::*;

pub fn prune_unimported_module_exports(bundle: &mut ChunkBundle) {
    let mut consumed: BTreeSet<String> = BTreeSet::new();
    let mut namespace_protected: BTreeSet<String> = BTreeSet::new();
    for chunk in &bundle.chunks {
        for file in &chunk.js.files {
            let Some(ast) = file.ast() else {
                continue;
            };
            collect_consumers(
                &ast.module,
                &file_abs_path(file),
                &mut consumed,
                &mut namespace_protected,
            );
        }
    }

    for chunk in &mut bundle.chunks {
        for file in &mut chunk.js.files {
            if file.metadata.role != FileRole::Module {
                continue;
            }
            let file_abs = file_abs_path(file);
            if namespace_protected.contains(&file_abs) {
                continue;
            }
            let Some(ast) = file.ast_mut() else {
                continue;
            };
            let import_locals = collect_import_locals(&ast.module);
            prune_local_named_exports(&mut ast.module, &consumed, &import_locals);
        }
    }
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
                                Some(name) => export_name_string(name),
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
                            consumed.insert(export_name_string(&n.orig));
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
) {
    module.body.retain_mut(|item| {
        // Only local `export { ... }` blocks (no `from`) re-export bindings
        // owned by this module. Re-exports, inline `export const/function`,
        // and default exports are left untouched.
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
            return true;
        };
        if named.src.is_some() {
            return true;
        }
        named.specifiers.retain(|spec| {
            let ExportSpecifier::Named(n) = spec else {
                return true;
            };
            // Re-export of an imported binding: keep it (pruning would orphan
            // the import). Only dead exports of locally declared bindings —
            // the decorator scaffolding — are pruned.
            let local = export_name_string(&n.orig);
            if import_locals.contains(&local) {
                return true;
            }
            let public = match &n.exported {
                Some(name) => export_name_string(name),
                None => local.clone(),
            };
            consumed.contains(&public)
        });
        // Drop a now-empty `export {};` rather than emit it.
        !named.specifiers.is_empty()
    });
}

fn export_name_string(name: &ModuleExportName) -> String {
    match name {
        ModuleExportName::Ident(ident) => ident.sym.to_string(),
        ModuleExportName::Str(s) => s.value.as_str().unwrap_or_default().to_string(),
    }
}

#[cfg(test)]
mod tests {
    use artifact::{
        ChunkAnalysisReport, ChunkArtifact, ChunkBundle, ChunkMetadata, ChunkTable, FileMetadata,
        FileRole, JsChunk, JsFile, JsFileBody,
    };
    use js_ast::parse_js_module;

    use super::*;

    /// One chunk `c` (source path `c.js`), files laid out flat so a sibling's
    /// `./mod.js` resolves to `c/mod.js`.
    fn bundle(files: &[(&str, &str, FileRole)]) -> ChunkBundle {
        let mut bundle = ChunkBundle {
            chunks: Vec::new(),
            chunk_table: ChunkTable::default(),
        };
        let chunk_id = bundle.chunk_table.intern("c".to_string());
        let js_files = files
            .iter()
            .map(|(path, source, role)| {
                let parsed = parse_js_module(&format!("c/{path}"), source).expect("parse");
                JsFile {
                    path: path.to_string(),
                    body: JsFileBody::Ast(parsed),
                    header_lines: Vec::new(),
                    binding_comments: std::collections::BTreeMap::new(),
                    leading_item_comments: std::collections::BTreeMap::new(),
                    metadata: FileMetadata {
                        chunk_id: "c".to_string(),
                        chunk_file: path.to_string(),
                        role: *role,
                        source_path: "c.js".to_string(),
                    },
                }
            })
            .collect();
        bundle.chunks.push(ChunkArtifact {
            chunk_id,
            js: JsChunk {
                entry_file: "entry.js".to_string(),
                files: js_files,
                metadata: ChunkMetadata {
                    source_path: "c.js".to_string(),
                },
            },
            analysis: ChunkAnalysisReport {
                chunk_id: "c".to_string(),
                source_path: "c.js".to_string(),
                parser: Default::default(),
                entry_file: "entry.js".to_string(),
                counts: Default::default(),
                files: Vec::new(),
                imports: Vec::new(),
                export_aliases: Vec::new(),
                unresolved_exports: Vec::new(),
                kept_top_level_declarations: Vec::new(),
            },
        });
        bundle
    }

    fn emitted_exports(bundle: &ChunkBundle, file_path: &str) -> Vec<String> {
        let file = bundle.chunks[0]
            .js
            .files
            .iter()
            .find(|f| f.path == file_path)
            .expect("file");
        let mut names = Vec::new();
        for item in &file.ast().expect("ast").module.body {
            if let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item
                && named.src.is_none()
            {
                for spec in &named.specifiers {
                    if let ExportSpecifier::Named(n) = spec {
                        names.push(match &n.exported {
                            Some(name) => export_name_string(name),
                            None => export_name_string(&n.orig),
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
            let mut bundle = bundle(&[
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
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(emitted_exports(&bundle, "mod.js"), vec!["used".to_string()]);
        });
    }

    #[test]
    fn keeps_export_consumed_via_alias_import() {
        js_ast::with_swc_globals(|| {
            // `import { used as u }` still consumes the source name `used`.
            let mut bundle = bundle(&[
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
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(emitted_exports(&bundle, "mod.js"), vec!["used".to_string()]);
        });
    }

    #[test]
    fn removes_emptied_export_block() {
        js_ast::with_swc_globals(|| {
            let mut bundle = bundle(&[(
                "mod.js",
                "const a = 1;\nconst b = 2;\nexport { a, b };\n",
                FileRole::Module,
            )]);
            prune_unimported_module_exports(&mut bundle);
            assert!(emitted_exports(&bundle, "mod.js").is_empty());
            // The whole `export { ... }` ModuleItem is gone, not left empty.
            let has_export = bundle.chunks[0].js.files[0]
                .ast()
                .unwrap()
                .module
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
            // outside the bundle); they are never pruned even if unimported.
            let mut bundle = bundle(&[(
                "entry.js",
                "const publicApi = 1;\nexport { publicApi };\n",
                FileRole::Entry,
            )]);
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(
                emitted_exports(&bundle, "entry.js"),
                vec!["publicApi".to_string()]
            );
        });
    }

    #[test]
    fn keeps_namespace_imported_module_exports() {
        js_ast::with_swc_globals(|| {
            // A namespace import accesses any export by property, so every
            // export of the target module is protected.
            let mut bundle = bundle(&[
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
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(
                emitted_exports(&bundle, "mod.js"),
                vec!["a".to_string(), "b".to_string()]
            );
        });
    }

    #[test]
    fn keeps_reexported_name() {
        js_ast::with_swc_globals(|| {
            // The entry re-exports `inner` out of `mod.js` via `export … from`;
            // the source must keep exporting it.
            let mut bundle = bundle(&[
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
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(
                emitted_exports(&bundle, "mod.js"),
                vec!["inner".to_string()]
            );
        });
    }

    #[test]
    fn keeps_export_consumed_by_sibling_module() {
        js_ast::with_swc_globals(|| {
            // Consumption by a sibling logical module (not the entry) keeps the
            // export alive too.
            let mut bundle = bundle(&[
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
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(emitted_exports(&bundle, "b.js"), vec!["shared".to_string()]);
        });
    }

    #[test]
    fn keeps_reexported_imported_binding_shim() {
        js_ast::with_swc_globals(|| {
            // `mod.js` re-imports a vendor binding and re-exports it under a
            // readable name. Even with no in-bundle consumer the export is
            // kept: pruning it would orphan the now-unused import. Only dead
            // exports of *locally declared* bindings (`dead`) are pruned.
            let mut bundle = bundle(&[(
                "mod.js",
                "import { x as a } from \"./vendor.js\";\nconst dead = 1;\nexport { a, dead };\n",
                FileRole::Module,
            )]);
            prune_unimported_module_exports(&mut bundle);
            assert_eq!(emitted_exports(&bundle, "mod.js"), vec!["a".to_string()]);
        });
    }
}
