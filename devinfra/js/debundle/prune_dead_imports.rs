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
//! - Every file's module requests keep their order, so the emitted evaluation
//!   order is the one the realizability gate and translation validation
//!   judged: if trimming empties an import that ORIGINALLY had named
//!   specifiers, the statement is dropped only when an earlier import in the
//!   same file still loads the same target module `M` (resolved by path, not
//!   specifier string), which makes it a no-op request; otherwise it becomes a
//!   bare `import "M";` in place. A load of `M` elsewhere in the bundle is not
//!   enough: dropping this file's request would move `M`'s evaluation later.
//!   Imports that were ORIGINALLY bare (`import "x";`) are never touched — the
//!   lowerer emits those intentionally.

use std::collections::{HashMap, HashSet};

use artifact::paths::{join_module_path, module_path_dirname};
use artifact::{ChunkBundle, JsFile};
use swc_atoms::Atom;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

pub fn prune_dead_import_specifiers(bundle: &mut ChunkBundle) {
    let mut modules: Vec<_> = bundle
        .chunks
        .iter_mut()
        .flat_map(|chunk| &mut chunk.js.files)
        .filter_map(|file| {
            let path = file_abs_path(file);
            Some((path, &mut file.ast_mut()?.module))
        })
        .collect();
    prune_imports(&mut modules);
}

/// Operate on a stable, flat list of ASTs; no chunk metadata or analysis reports
/// participate in import liveness. Paths are rooted in the emitted output tree.
fn prune_imports(modules: &mut [(String, &mut Module)]) {
    // PASS 1: per file, trim dead named specifiers in place. Records every
    // import that originally had named specifiers and is now empty, keyed by
    // (file, item) so later passes can locate it by stable index — PASS
    // 1 only edits specifier vectors, never the ModuleItem list.
    let mut emptied: Vec<EmptiedImport> = Vec::new();
    for (file_index, (importer_abs, module)) in modules.iter_mut().enumerate() {
        let referenced = collect_referenced_syms(module);
        let reexported = collect_local_reexport_origs(module);
        for (item_index, item) in module.body.iter_mut().enumerate() {
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
                    file_index,
                    item_index,
                    target_abs: resolve_specifier(src, importer_abs),
                });
            }
        }
    }

    if emptied.is_empty() {
        return;
    }

    // PASS 2: in each touched file, walk imports in order. An emptied import
    // whose target an earlier import of the same file still loads is a no-op
    // request and is dropped; any other emptied import stays as a bare import
    // and loads its target from here on.
    let emptied: HashMap<(usize, usize), String> = emptied
        .into_iter()
        .map(|entry| ((entry.file_index, entry.item_index), entry.target_abs))
        .collect();
    for (file_index, (importer_abs, module)) in modules.iter_mut().enumerate() {
        let mut loaded: HashSet<String> = HashSet::new();
        let mut item_index = 0;
        module.body.retain(|item| {
            let key = (file_index, item_index);
            item_index += 1;
            let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                return true;
            };
            if let Some(target_abs) = emptied.get(&key) {
                return loaded.insert(target_abs.clone());
            }
            if let Some(src) = import.src.value.as_str() {
                loaded.insert(resolve_specifier(src, importer_abs));
            }
            true
        });
    }
}

/// One import that originally had named specifiers and was left empty by
/// PASS 1, located by stable (file, item) index for PASS 3.
struct EmptiedImport {
    file_index: usize,
    item_index: usize,
    target_abs: String,
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
    use js_ast::parse_js_module;

    use super::*;

    fn pruned_modules(files: &[(&str, &str)]) -> Vec<(String, Module)> {
        let mut modules: Vec<_> = files
            .iter()
            .map(|(path, source)| {
                (
                    (*path).to_string(),
                    parse_js_module(path, source).unwrap().module,
                )
            })
            .collect();
        let mut views: Vec<_> = modules
            .iter_mut()
            .map(|(path, module)| (format!("c/{path}"), module))
            .collect();
        prune_imports(&mut views);
        modules
    }

    /// One `(source, local)` pair per surviving named import specifier in
    /// `file_path`, sorted. `source` is the raw import specifier string.
    fn named_imports(modules: &[(String, Module)], file_path: &str) -> Vec<(String, String)> {
        let (_, module) = modules
            .iter()
            .find(|(path, _)| path == file_path)
            .expect("file");
        let mut pairs = Vec::new();
        for item in &module.body {
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
    fn import_statements(modules: &[(String, Module)], file_path: &str) -> Vec<(String, usize)> {
        let (_, module) = modules
            .iter()
            .find(|(path, _)| path == file_path)
            .expect("file");
        let mut stmts = Vec::new();
        for item in &module.body {
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
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import { used, dead } from \"./mod.js\";\nconsole.log(used);\n",
                ),
                (
                    "sib.js",
                    "import { other } from \"./mod.js\";\nexport const useOther = () => other;\n",
                ),
                (
                    "mod.js",
                    "export const used = 1;\nexport const dead = 2;\nexport const other = 3;\n",
                ),
            ]);
            assert_eq!(
                named_imports(&modules, "entry.js"),
                vec![("./mod.js".to_string(), "used".to_string())]
            );
        });
    }

    #[test]
    fn keeps_emptied_import_bare_when_target_loaded_elsewhere() {
        js_ast::with_swc_globals(|| {
            // The entry's only specifier (`dead`) is unused, so the import
            // empties. A sibling still loads `mod.js`, but the entry requests
            // it first: dropping the request would evaluate `mod.js` later.
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import { dead } from \"./mod.js\";\nimport { useLive } from \"./sib.js\";\nconsole.log(useLive());\n",
                ),
                (
                    "sib.js",
                    "import { live } from \"./mod.js\";\nexport const useLive = () => live;\n",
                ),
                ("mod.js", "export const dead = 1;\nexport const live = 2;\n"),
            ]);
            assert_eq!(
                import_statements(&modules, "entry.js"),
                vec![("./mod.js".to_string(), 0), ("./sib.js".to_string(), 1)]
            );
        });
    }

    #[test]
    fn drops_emptied_import_only_after_an_earlier_request_of_its_target() {
        js_ast::with_swc_globals(|| {
            // Each file requests `mod.js` twice; only the second request is a
            // no-op, so only an emptied second request may go.
            let modules = pruned_modules(&[
                (
                    "late.js",
                    "import { used } from \"./mod.js\";\nimport { dead } from \"./mod.js\";\nexport const x = used;\n",
                ),
                (
                    "early.js",
                    "import { dead } from \"./mod.js\";\nimport { used } from \"./mod.js\";\nexport const y = used;\n",
                ),
                ("mod.js", "export const used = 1;\nexport const dead = 2;\n"),
            ]);
            assert_eq!(
                import_statements(&modules, "late.js"),
                vec![("./mod.js".to_string(), 1)]
            );
            assert_eq!(
                import_statements(&modules, "early.js"),
                vec![("./mod.js".to_string(), 0), ("./mod.js".to_string(), 1)]
            );
        });
    }

    #[test]
    fn converts_sole_loaded_emptied_import_to_bare() {
        js_ast::with_swc_globals(|| {
            // Nobody else loads `mod.js`, so emptying the entry's import must
            // not drop the module evaluation: it becomes a bare `import
            // "./mod.js";` (statement kept, zero specifiers).
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import { dead } from \"./mod.js\";\nconsole.log(1);\n",
                ),
                (
                    "mod.js",
                    "globalThis.__sideEffect = 1;\nexport const dead = 2;\n",
                ),
            ]);
            assert_eq!(
                import_statements(&modules, "entry.js"),
                vec![("./mod.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn keeps_exactly_one_bare_import_per_sole_loaded_target() {
        js_ast::with_swc_globals(|| {
            // Two emptied imports of the same sole-loaded target: keep exactly
            // one bare import, drop the other.
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import { a } from \"./mod.js\";\nimport { b } from \"./mod.js\";\nconsole.log(1);\n",
                ),
                (
                    "mod.js",
                    "globalThis.__sideEffect = 1;\nexport const a = 1;\nexport const b = 2;\n",
                ),
            ]);
            assert_eq!(
                import_statements(&modules, "entry.js"),
                vec![("./mod.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn leaves_originally_bare_import_untouched() {
        js_ast::with_swc_globals(|| {
            // A side-effect-only import the lowerer emitted intentionally is
            // never touched, even though it loads a target nothing else loads.
            let modules = pruned_modules(&[("entry.js", "import \"./x.js\";\nconsole.log(1);\n")]);
            assert_eq!(
                import_statements(&modules, "entry.js"),
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
            let modules = pruned_modules(&[
                (
                    "entry.js",
                    "import def from \"./d.js\";\nimport * as ns from \"./n.js\";\nconsole.log(1);\n",
                ),
                ("d.js", "export default 1;\n"),
                ("n.js", "export const k = 1;\n"),
            ]);
            let module = &modules[0].1;
            let mut defaults = Vec::new();
            let mut namespaces = Vec::new();
            for item in &module.body {
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
            let modules = pruned_modules(&[(
                "mod.js",
                "import { x as a } from \"./v.js\";\nexport { a };\n",
            )]);
            assert_eq!(
                named_imports(&modules, "mod.js"),
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
            let modules = pruned_modules(&[
                (
                    "mod.js",
                    "import { f } from \"./m.js\";\nexport const g = (f) => f();\n",
                ),
                ("m.js", "export const f = () => 1;\n"),
            ]);
            assert!(
                named_imports(&modules, "mod.js").is_empty(),
                "shadowed import should be trimmed; the param is the only `f` reference"
            );
            // The whole import statement is gone (sole-loaded? no — nobody else
            // loads m.js, so it becomes bare). Module evaluation is preserved.
            assert_eq!(
                import_statements(&modules, "mod.js"),
                vec![("./m.js".to_string(), 0)]
            );
        });
    }

    #[test]
    fn unshadowed_use_outside_function_keeps_import() {
        js_ast::with_swc_globals(|| {
            // A param `f` shadows only inside its own function; a top-level
            // reference to the imported `f` still counts and keeps the import.
            let modules = pruned_modules(&[
                (
                    "mod.js",
                    "import { f } from \"./m.js\";\nconst h = (f) => f();\nexport const top = f;\n",
                ),
                ("m.js", "export const f = 1;\n"),
            ]);
            assert_eq!(
                named_imports(&modules, "mod.js"),
                vec![("./m.js".to_string(), "f".to_string())]
            );
        });
    }
}
