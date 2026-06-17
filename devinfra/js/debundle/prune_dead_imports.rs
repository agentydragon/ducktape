//! Trim dead named import specifiers across the whole emitted bundle.
//!
//! The lowerer over-imports: an emitted file (the entry most of all)
//! frequently carries `import { name } from "M"` specifiers for `name`s it
//! never references — residual/hub bindings the per-module emit path imported
//! defensively. A dead named import is doubly wasteful: it bloats the file, and
//! because the name-based export prune (`prune_unimported_module_exports`)
//! treats any imported name as "consumed", every dead import keeps a
//! module-internal helper exported for no real consumer. Running this pass
//! immediately before that one lets the export prune drop the now-unimported
//! exports as a cascade.
//!
//! This is the bundle-wide post-emission analogue of
//! `lowering::exports::trim_dead_named_specifiers` (which trims a single body
//! during lowering); its reference collector mirrors that module's
//! `TargetedRefCollector`. The two are deliberately copies: the lowering one is
//! private to its crate and gated by a candidate set, while this one runs over
//! the final artifact and collects the full referenced-sym set per file.
//!
//! Soundness (a violation breaks the rendered app):
//! - Only ever removes named import *specifiers* or whole import *statements* —
//!   never a binding declaration, an export, or a side-effecting statement.
//! - Only `Named` specifiers are trimmed; `Default` and `Namespace` (`* as ns`)
//!   are always kept (a namespace import can read any export by property).
//! - A named specifier `import { orig as local } from "M"` is dead iff `local`
//!   is referenced nowhere in the importing file's body (honoring shadowing)
//!   AND `local` is not the `orig` of any local `export { … }` (re-export
//!   shim — keep it so the import isn't orphaned).
//! - Module evaluation/side effects are preserved: if trimming empties an
//!   import that ORIGINALLY had named specifiers, the statement is dropped only
//!   when the same target module `M` (resolved by path, not specifier string)
//!   is still loaded by some other surviving import anywhere in the bundle;
//!   otherwise it is converted to a bare side-effect import `import "M";`, with
//!   exactly one bare import kept per such sole-loaded target. Imports that were
//!   ORIGINALLY bare (`import "x";`) are never touched — the lowerer emits those
//!   intentionally.

use std::collections::HashSet;

use artifact::{ChunkBundle, JsFile, join_module_path, module_path_dirname};
use swc_atoms::Atom;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

pub fn prune_dead_import_specifiers(bundle: &mut ChunkBundle) {
    // PASS 1: per file, trim dead named specifiers in place. Records every
    // import that originally had named specifiers and is now empty, keyed by
    // (chunk, file, item) so later passes can locate it by stable index — PASS
    // 1 only edits specifier vectors, never the ModuleItem list.
    let mut emptied: Vec<EmptiedImport> = Vec::new();
    for (chunk_index, chunk) in bundle.chunks.iter_mut().enumerate() {
        for (file_index, file) in chunk.js.files.iter_mut().enumerate() {
            let importer_abs = file_abs_path(file);
            let Some(ast) = file.ast_mut() else {
                continue;
            };
            let referenced = collect_referenced_syms(&ast.module);
            let reexported = collect_local_reexport_origs(&ast.module);
            for (item_index, item) in ast.module.body.iter_mut().enumerate() {
                let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                    continue;
                };
                // Originally-bare imports exist solely to evaluate the target;
                // never touch them.
                let had_named = import
                    .specifiers
                    .iter()
                    .any(|spec| matches!(spec, ImportSpecifier::Named(_)));
                if !had_named {
                    continue;
                }
                import.specifiers.retain(|spec| match spec {
                    ImportSpecifier::Default(_) | ImportSpecifier::Namespace(_) => true,
                    ImportSpecifier::Named(named) => {
                        let local = &named.local.sym;
                        referenced.contains(local) || reexported.contains(local)
                    }
                });
                if import.specifiers.is_empty()
                    && let Some(src) = import.src.value.as_str()
                {
                    emptied.push(EmptiedImport {
                        chunk_index,
                        file_index,
                        item_index,
                        target_abs: resolve_specifier(src, &importer_abs),
                    });
                }
            }
        }
    }

    if emptied.is_empty() {
        return;
    }

    // PASS 2: resolved targets that, after PASS 1, are still loaded by some
    // surviving import statement — one that kept ≥1 specifier or was originally
    // bare. An emptied import whose target is in this set is redundant and can
    // be dropped outright; otherwise the target is loaded only by emptied
    // imports and exactly one must survive as a bare side-effect import.
    let targets_with_surviving_load = collect_targets_with_surviving_load(bundle);

    // PASS 3 decisions, computed before mutating bodies (item indices from PASS
    // 1 stay valid only until the body is rebuilt). For each emptied import:
    // drop it if the target is independently loaded, else keep the first
    // occurrence per target as a bare import and drop the rest.
    let mut kept_bare: HashSet<String> = HashSet::new();
    let mut drop_items: HashSet<(usize, usize, usize)> = HashSet::new();
    let mut bare_items: HashSet<(usize, usize, usize)> = HashSet::new();
    for entry in &emptied {
        let key = (entry.chunk_index, entry.file_index, entry.item_index);
        if targets_with_surviving_load.contains(&entry.target_abs) {
            drop_items.insert(key);
        } else if kept_bare.insert(entry.target_abs.clone()) {
            bare_items.insert(key);
        } else {
            drop_items.insert(key);
        }
    }

    // PASS 3: rebuild each touched body, dropping the marked import ModuleItems
    // and clearing specifiers on the kept-bare ones.
    for (chunk_index, chunk) in bundle.chunks.iter_mut().enumerate() {
        for (file_index, file) in chunk.js.files.iter_mut().enumerate() {
            let Some(ast) = file.ast_mut() else {
                continue;
            };
            let mut item_index = 0;
            ast.module.body.retain_mut(|item| {
                let key = (chunk_index, file_index, item_index);
                item_index += 1;
                if drop_items.contains(&key) {
                    return false;
                }
                if bare_items.contains(&key)
                    && let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item
                {
                    import.specifiers.clear();
                }
                true
            });
        }
    }
}

/// One import that originally had named specifiers and was left empty by
/// PASS 1, located by stable (chunk, file, item) index for PASS 3.
struct EmptiedImport {
    chunk_index: usize,
    file_index: usize,
    item_index: usize,
    target_abs: String,
}

/// Resolved target paths still loaded by an import that survives PASS 1 with at
/// least one specifier, or that was originally bare. Such a target's module
/// evaluation is already guaranteed, so an emptied import of it carries no
/// side effect worth preserving.
fn collect_targets_with_surviving_load(bundle: &ChunkBundle) -> HashSet<String> {
    let mut targets = HashSet::new();
    for chunk in &bundle.chunks {
        for file in &chunk.js.files {
            let importer_abs = file_abs_path(file);
            let Some(ast) = file.ast() else {
                continue;
            };
            for item in &ast.module.body {
                let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                    continue;
                };
                // After PASS 1 a non-empty specifier list is a real load; an
                // empty one is either an originally-bare import (a real load
                // too) or an import PASS 1 just emptied (not yet a guaranteed
                // load — its fate is decided against this very set).
                let loads = !import.specifiers.is_empty();
                if loads && let Some(src) = import.src.value.as_str() {
                    targets.insert(resolve_specifier(src, &importer_abs));
                }
            }
        }
    }
    targets
}

/// Local names re-exported by this body via `export { local … }` (no `from`).
/// Such a name is the `orig` of a re-export shim whose import must be kept even
/// when the local is otherwise unreferenced, or the export would be orphaned.
fn collect_local_reexport_origs(module: &Module) -> HashSet<Atom> {
    let mut origs = HashSet::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
            continue;
        };
        if named.src.is_some() {
            continue;
        }
        for spec in &named.specifiers {
            if let ExportSpecifier::Named(n) = spec
                && let ModuleExportName::Ident(ident) = &n.orig
            {
                origs.insert(ident.sym.clone());
            }
        }
    }
    origs
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

/// Every symbol referenced (read) anywhere in a body, honoring shadowing.
/// Mirrors `lowering::exports::TargetedRefCollector`'s binding/shadowing rules,
/// but collects the full referenced set rather than tracking a candidate quota.
fn collect_referenced_syms(module: &Module) -> HashSet<Atom> {
    let mut collector = ReferencedSymCollector::default();
    for item in &module.body {
        item.visit_with(&mut collector);
    }
    collector.found
}

#[derive(Default)]
struct ReferencedSymCollector {
    found: HashSet<Atom>,
    shadowed_scopes: Vec<HashSet<Atom>>,
}

impl ReferencedSymCollector {
    fn is_shadowed(&self, name: &Atom) -> bool {
        self.shadowed_scopes
            .iter()
            .rev()
            .any(|scope| scope.contains(name))
    }

    fn with_shadowed_scope<F: FnOnce(&mut Self)>(&mut self, names: HashSet<Atom>, f: F) {
        self.shadowed_scopes.push(names);
        f(self);
        self.shadowed_scopes.pop();
    }
}

impl Visit for ReferencedSymCollector {
    fn visit_ident(&mut self, node: &Ident) {
        if !self.is_shadowed(&node.sym) {
            self.found.insert(node.sym.clone());
        }
    }

    // Binding sites are not references.
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    // Import binding sites (`import { x }`) are not references; the whole
    // declaration is skipped so the specifier locals never count themselves.
    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_function(&mut self, node: &Function) {
        let shadowed = node
            .params
            .iter()
            .flat_map(|param| param_pat_syms(&param.pat))
            .collect();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let shadowed = node.params.iter().flat_map(param_pat_syms).collect();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        node.obj.visit_with(self);
        // `.prop` is a property name, not a binding reference, unless computed.
        if let MemberProp::Computed(computed) = &node.prop {
            computed.expr.visit_with(self);
        }
    }

    fn visit_prop_name(&mut self, node: &PropName) {
        if let PropName::Computed(computed) = node {
            computed.expr.visit_with(self);
        }
    }

    // JSX element/attribute names are not value references to imports here.
    fn visit_jsx_element_name(&mut self, _node: &JSXElementName) {}

    fn visit_jsx_attr_name(&mut self, _node: &JSXAttrName) {}
}

/// Binding-identifier syms a parameter pattern introduces, used to shadow
/// references inside the owning function/arrow body.
fn param_pat_syms(pat: &Pat) -> Vec<Atom> {
    let mut collector = PatBindingCollector::default();
    pat.visit_with(&mut collector);
    collector.syms
}

#[derive(Default)]
struct PatBindingCollector {
    syms: Vec<Atom>,
}

impl Visit for PatBindingCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.syms.push(node.id.sym.clone());
    }

    // `const { a: b }` binds `b`, not `a`; a computed key (`{ [k]: v }`) is an
    // expression, not a binding, so don't descend into key positions.
    fn visit_key_value_pat_prop(&mut self, node: &KeyValuePatProp) {
        node.value.visit_with(self);
    }

    // Default-value expressions (`(a = expr) => …`) are evaluated in the outer
    // scope and may reference imports, but they are not binding sites; only the
    // bound name matters for shadowing, so skip the initializer.
    fn visit_assign_pat(&mut self, node: &AssignPat) {
        node.left.visit_with(self);
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

    /// One `(source, local)` pair per surviving named import specifier in
    /// `file_path`, sorted. `source` is the raw import specifier string.
    fn named_imports(bundle: &ChunkBundle, file_path: &str) -> Vec<(String, String)> {
        let file = bundle.chunks[0]
            .js
            .files
            .iter()
            .find(|f| f.path == file_path)
            .expect("file");
        let mut pairs = Vec::new();
        for item in &file.ast().expect("ast").module.body {
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
                let src = import.src.value.as_str().expect("utf8 src").to_string();
                for spec in &import.specifiers {
                    if let ImportSpecifier::Named(named) = spec {
                        pairs.push((src.clone(), named.local.sym.to_string()));
                    }
                }
            }
        }
        pairs.sort();
        pairs
    }

    /// Import statements in `file_path` as `(source, specifier_count)`, sorted —
    /// lets a test assert a statement survives with zero specifiers (bare).
    fn import_statements(bundle: &ChunkBundle, file_path: &str) -> Vec<(String, usize)> {
        let file = bundle.chunks[0]
            .js
            .files
            .iter()
            .find(|f| f.path == file_path)
            .expect("file");
        let mut stmts = Vec::new();
        for item in &file.ast().expect("ast").module.body {
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
                let src = import.src.value.as_str().expect("utf8 src").to_string();
                stmts.push((src, import.specifiers.len()));
            }
        }
        stmts.sort();
        stmts
    }

    #[test]
    fn drops_dead_named_specifier_keeps_used() {
        js_ast::with_swc_globals(|| {
            // The entry imports `{ used, dead }` but references only `used`;
            // `mod.js` is also imported by a sibling, so the entry import is not
            // emptied and simply loses the `dead` specifier.
            let mut bundle = bundle(&[
                (
                    "entry.js",
                    "import { used, dead } from \"./mod.js\";\nconsole.log(used);\n",
                    FileRole::Entry,
                ),
                (
                    "sib.js",
                    "import { other } from \"./mod.js\";\nexport const useOther = () => other;\n",
                    FileRole::Module,
                ),
                (
                    "mod.js",
                    "export const used = 1;\nexport const dead = 2;\nexport const other = 3;\n",
                    FileRole::Module,
                ),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            assert_eq!(
                named_imports(&bundle, "entry.js"),
                vec![("./mod.js".to_string(), "used".to_string())]
            );
        });
    }

    #[test]
    fn drops_emptied_import_when_target_loaded_elsewhere() {
        js_ast::with_swc_globals(|| {
            // The entry's only specifier (`dead`) is unused, so the import
            // empties. `mod.js` is still loaded (used) by a sibling, so the
            // emptied entry import is dropped outright — not left bare.
            let mut bundle = bundle(&[
                (
                    "entry.js",
                    "import { dead } from \"./mod.js\";\nconsole.log(1);\n",
                    FileRole::Entry,
                ),
                (
                    "sib.js",
                    "import { live } from \"./mod.js\";\nexport const useLive = () => live;\n",
                    FileRole::Module,
                ),
                (
                    "mod.js",
                    "export const dead = 1;\nexport const live = 2;\n",
                    FileRole::Module,
                ),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            assert!(
                import_statements(&bundle, "entry.js").is_empty(),
                "emptied entry import of an elsewhere-loaded target should be dropped"
            );
        });
    }

    #[test]
    fn converts_sole_loaded_emptied_import_to_bare() {
        js_ast::with_swc_globals(|| {
            // Nobody else loads `mod.js`, so emptying the entry's import must
            // not drop the module evaluation: it becomes a bare `import
            // "./mod.js";` (statement kept, zero specifiers).
            let mut bundle = bundle(&[
                (
                    "entry.js",
                    "import { dead } from \"./mod.js\";\nconsole.log(1);\n",
                    FileRole::Entry,
                ),
                (
                    "mod.js",
                    "globalThis.__sideEffect = 1;\nexport const dead = 2;\n",
                    FileRole::Module,
                ),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            assert_eq!(
                import_statements(&bundle, "entry.js"),
                vec![("./mod.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn keeps_exactly_one_bare_import_per_sole_loaded_target() {
        js_ast::with_swc_globals(|| {
            // Two emptied imports of the same sole-loaded target: keep exactly
            // one bare import, drop the other.
            let mut bundle = bundle(&[
                (
                    "entry.js",
                    "import { a } from \"./mod.js\";\nimport { b } from \"./mod.js\";\nconsole.log(1);\n",
                    FileRole::Entry,
                ),
                (
                    "mod.js",
                    "globalThis.__sideEffect = 1;\nexport const a = 1;\nexport const b = 2;\n",
                    FileRole::Module,
                ),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            assert_eq!(
                import_statements(&bundle, "entry.js"),
                vec![("./mod.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn leaves_originally_bare_import_untouched() {
        js_ast::with_swc_globals(|| {
            // A side-effect-only import the lowerer emitted intentionally is
            // never touched, even though it loads a target nothing else loads.
            let mut bundle = bundle(&[(
                "entry.js",
                "import \"./x.js\";\nconsole.log(1);\n",
                FileRole::Entry,
            )]);
            prune_dead_import_specifiers(&mut bundle);
            assert_eq!(
                import_statements(&bundle, "entry.js"),
                vec![("./x.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn never_trims_default_or_namespace_specifiers() {
        js_ast::with_swc_globals(|| {
            // Neither the default binding nor the namespace binding is
            // referenced, yet both must survive (a namespace can read any
            // export by property; a default is a single binding this pass
            // doesn't reason about).
            let mut bundle = bundle(&[
                (
                    "entry.js",
                    "import def from \"./d.js\";\nimport * as ns from \"./n.js\";\nconsole.log(1);\n",
                    FileRole::Entry,
                ),
                ("d.js", "export default 1;\n", FileRole::Module),
                ("n.js", "export const k = 1;\n", FileRole::Module),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            let file = &bundle.chunks[0].js.files[0];
            let mut defaults = Vec::new();
            let mut namespaces = Vec::new();
            for item in &file.ast().expect("ast").module.body {
                if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
                    for spec in &import.specifiers {
                        match spec {
                            ImportSpecifier::Default(d) => defaults.push(d.local.sym.to_string()),
                            ImportSpecifier::Namespace(n) => {
                                namespaces.push(n.local.sym.to_string())
                            }
                            ImportSpecifier::Named(_) => {}
                        }
                    }
                }
            }
            assert_eq!(defaults, vec!["def".to_string()]);
            assert_eq!(namespaces, vec!["ns".to_string()]);
        });
    }

    #[test]
    fn keeps_import_referenced_only_by_local_reexport() {
        js_ast::with_swc_globals(|| {
            // `a` is unused in the body but re-exported by a local
            // `export { a }`; trimming the import would orphan the export, so
            // the specifier is kept.
            let mut bundle = bundle(&[(
                "mod.js",
                "import { x as a } from \"./v.js\";\nexport { a };\n",
                FileRole::Module,
            )]);
            prune_dead_import_specifiers(&mut bundle);
            assert_eq!(
                named_imports(&bundle, "mod.js"),
                vec![("./v.js".to_string(), "a".to_string())]
            );
        });
    }

    #[test]
    fn shadowed_param_does_not_count_as_reference() {
        js_ast::with_swc_globals(|| {
            // The import `f` is shadowed by the arrow param `f`, so the body's
            // `f()` refers to the param, not the import. The import is unused
            // and dropped.
            let mut bundle = bundle(&[
                (
                    "mod.js",
                    "import { f } from \"./m.js\";\nexport const g = (f) => f();\n",
                    FileRole::Module,
                ),
                ("m.js", "export const f = () => 1;\n", FileRole::Module),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            assert!(
                named_imports(&bundle, "mod.js").is_empty(),
                "shadowed import should be trimmed; the param is the only `f` reference"
            );
            // The whole import statement is gone (sole-loaded? no — nobody else
            // loads m.js, so it becomes bare). Module evaluation is preserved.
            assert_eq!(
                import_statements(&bundle, "mod.js"),
                vec![("./m.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn unshadowed_use_outside_function_keeps_import() {
        js_ast::with_swc_globals(|| {
            // A param `f` shadows only inside its own function; a top-level
            // reference to the imported `f` still counts and keeps the import.
            let mut bundle = bundle(&[
                (
                    "mod.js",
                    "import { f } from \"./m.js\";\nconst h = (f) => f();\nexport const top = f;\n",
                    FileRole::Module,
                ),
                ("m.js", "export const f = 1;\n", FileRole::Module),
            ]);
            prune_dead_import_specifiers(&mut bundle);
            assert_eq!(
                named_imports(&bundle, "mod.js"),
                vec![("./m.js".to_string(), "f".to_string())]
            );
        });
    }
}
