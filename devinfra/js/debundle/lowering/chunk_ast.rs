//! Pre-naturalization analysis of one chunk AST: which top-level
//! statements declare what, which imports are runtime, which patterns
//! introduce destructure siblings, etc. `ChunkAstAnalysis` is the
//! input to `materialize_logical_chunk`s spec-driven plan resolution.

use binding_targets::{
    binding_name_strings, binding_names as bt_binding_names, declaration_ids as bt_declaration_ids,
    declaration_name_strings,
};

use super::*;

#[derive(Debug, Clone)]
pub(super) struct TopLevelDecl {
    pub(super) ordinal: usize,
    pub(super) bindings: Vec<(String, Id)>,
    pub(super) exported: bool,
}

pub(super) struct ChunkAstAnalysis {
    pub(super) runtime_import_facts: RuntimeImportFacts,
    pub(super) declarations: Vec<TopLevelDecl>,
    pub(super) declaration_by_name: HashMap<Id, usize>,
    /// Sibling sets for top-level destructuring declarators only.
    /// For a destructuring declarator like `const { x, y } = obj`
    /// both `x` and `y` map to the set `{x, y}`. Plain
    /// single-name declarators (`const a = 1`) are not recorded —
    /// they don't need atomicity enforcement, and absence here is
    /// the signal that there are no siblings to consider. Used by
    /// `build_module_plans` to enforce destructure-atomicity:
    /// claiming any one binding from a destructure pulls the rest
    /// into the same module, because the materializer's
    /// `split_var_decl` moves a destructuring declarator as one
    /// atomic unit.
    pub(super) destructure_siblings: BTreeMap<String, BTreeSet<String>>,
    /// Bindings that the source chunk's entry already exports (via
    /// `export { foo, bar }` re-exports of local bindings, or
    /// `export const foo = …` style declarations). The materializer
    /// consults this set in `auto_grown_residual_exports` so the
    /// auto-grown `export { name }` block doesn't duplicate an
    /// existing source-level export — emitting a duplicate would be
    /// a `SyntaxError: Duplicate export of 'name'` at load time.
    pub(super) pre_existing_entry_exports: HashSet<Id>,
    /// **Public** names the source chunk's entry already uses (the
    /// `exported` side of `export { orig as exported }` specifiers
    /// and the declared name of `export const foo = …` style
    /// declarations). Distinct from `pre_existing_entry_exports`,
    /// which is the **local** side. Consulted by
    /// `auto_grown_residual_exports` so the auto-grown `export {
    /// local as public }` doesn't reuse a public name that's
    /// already taken — e.g. when entry has `export { X as av }` and
    /// a peeled module references a different local binding named
    /// `av`, growing the export under the same public name would
    /// produce a duplicate-export `SyntaxError` at load time.
    pub(super) pre_existing_public_export_names: HashSet<String>,
}

pub(super) fn analyze_chunk_ast(module: &Module) -> ChunkAstAnalysis {
    let mut imports = HashMap::<Id, RuntimeImportInfo>::new();
    let mut declarations = Vec::new();
    let mut pre_existing_entry_exports = HashSet::<Id>::new();
    let mut pre_existing_public_export_names = HashSet::<String>::new();
    let mut destructure_siblings = BTreeMap::<String, BTreeSet<String>>::new();
    for (ordinal, item) in module.body.iter().enumerate() {
        let (names, exported) = top_level_declaration_names(item);
        let ids = top_level_declaration_ids(item);
        if !names.is_empty() {
            let bindings: Vec<(String, Id)> = names.into_iter().zip(ids).collect();
            if exported {
                pre_existing_entry_exports.extend(bindings.iter().map(|(_, id)| id.clone()));
                // `export const foo = …` / `export function foo()` /
                // `export class Foo {}` — the declared name is also
                // the public name.
                pre_existing_public_export_names
                    .extend(bindings.iter().map(|(name, _)| name.clone()));
            }
            declarations.push(TopLevelDecl {
                ordinal,
                bindings,
                exported,
            });
        }
        record_destructure_sibling_groups(item, &mut destructure_siblings);
        record_runtime_imports(item, &mut imports);
        record_pre_existing_named_exports(
            item,
            &mut pre_existing_entry_exports,
            &mut pre_existing_public_export_names,
        );
    }
    let declaration_by_name = declarations
        .iter()
        .flat_map(|decl| {
            decl.bindings
                .iter()
                .map(|(_, id)| (id.clone(), decl.ordinal))
        })
        .collect::<HashMap<_, _>>();
    ChunkAstAnalysis {
        runtime_import_facts: RuntimeImportFacts { imports },
        declarations,
        declaration_by_name,
        destructure_siblings,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    }
}

/// For each top-level `var/let/const` declarator whose pattern binds
/// more than one name (i.e. a destructure like `const { x, y } = obj`
/// or `const [a, b] = arr`), record a sibling set mapping every name
/// in the pattern to the set of all names from that pattern.
/// Single-name declarators add nothing.
pub(super) fn record_destructure_sibling_groups(
    item: &ModuleItem,
    out: &mut BTreeMap<String, BTreeSet<String>>,
) {
    let decl = match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
        _ => return,
    };
    let Decl::Var(var) = decl else {
        return;
    };
    for declarator in &var.decls {
        let names = binding_name_strings(&declarator.name);
        if names.len() < 2 {
            continue;
        }
        let group: BTreeSet<String> = names.iter().cloned().collect();
        for name in &names {
            out.entry(name.clone())
                .or_default()
                .extend(group.iter().cloned());
        }
    }
}

/// Pick up `export { foo, bar as baz }` (no `from`) — i.e. re-exports
/// of locally-declared bindings. `export … from …` is excluded
/// because those don't bind a local name in entry. `ExportDecl`
/// (e.g. `export const foo = …`) is already covered by
/// `top_level_declaration_ids` returning `(ids, exported = true)`.
///
/// Populates two parallel sets:
/// - `local_out` — the `orig` (local-binding) side of each
///   specifier, keyed on hygiene-aware `Id` so the emit-resolvability
///   check in `auto_grown_residual_exports` matches the binding cells
///   the analysis records.
/// - `public_out` — the `exported` (public-name) side of each
///   specifier (falls back to `orig`'s sym when no `as` rename is
///   present). Keyed on bare `String` because export names are pure
///   labels, not bound to a hygienic scope.
pub(super) fn record_pre_existing_named_exports(
    item: &ModuleItem,
    local_out: &mut HashSet<Id>,
    public_out: &mut HashSet<String>,
) {
    let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
        return;
    };
    if named.src.is_some() {
        return;
    }
    for specifier in &named.specifiers {
        let ExportSpecifier::Named(specifier) = specifier else {
            continue;
        };
        let ModuleExportName::Ident(orig_ident) = &specifier.orig else {
            continue;
        };
        local_out.insert(orig_ident.to_id());
        let public_name = match &specifier.exported {
            Some(ModuleExportName::Ident(ident)) => ident.sym.to_string(),
            Some(ModuleExportName::Str(_)) => continue,
            None => orig_ident.sym.to_string(),
        };
        public_out.insert(public_name);
    }
}

pub(super) fn top_level_declaration_names(item: &ModuleItem) -> (Vec<String>, bool) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => (declaration_names(decl), false),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            (declaration_names(&export_decl.decl), true)
        }
        // `var`s hoist to module scope out of `try`/`if`/loop blocks;
        // the enclosing statement is the declarer. Keeps
        // `declaration_by_name` in sync with the analyzer's
        // `collect_declared_names`.
        ModuleItem::Stmt(stmt) => (
            binding_targets::hoisted_var_ids(stmt)
                .iter()
                .map(|(atom, _)| atom.to_string())
                .collect(),
            false,
        ),
        _ => (Vec::new(), false),
    }
}

pub(super) fn top_level_declaration_ids(item: &ModuleItem) -> Vec<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_ids(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_ids(&export_decl.decl)
        }
        ModuleItem::Stmt(stmt) => binding_targets::hoisted_var_ids(stmt),
        _ => Vec::new(),
    }
}

pub(super) fn declaration_names(decl: &Decl) -> Vec<String> {
    declaration_name_strings(decl)
}

pub(super) fn declaration_ids(decl: &Decl) -> Vec<Id> {
    bt_declaration_ids(decl)
}

pub(super) fn binding_names(pattern: &Pat) -> Vec<String> {
    binding_name_strings(pattern)
}

pub(super) fn binding_ids(pattern: &Pat) -> Vec<Id> {
    bt_binding_names(pattern).collect()
}
