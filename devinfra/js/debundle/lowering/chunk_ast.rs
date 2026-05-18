//! Pre-naturalization analysis of one chunk AST: which top-level
//! statements declare what, which imports are runtime, which patterns
//! introduce destructure siblings, etc. `ChunkAstAnalysis` is the
//! input to `materialize_logical_chunk`s spec-driven plan resolution.

use super::*;

#[derive(Debug, Clone)]
pub(super) struct TopLevelDecl {
    pub(super) ordinal: usize,
    pub(super) names: Vec<String>,
    pub(super) ids: Vec<Id>,
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
}

pub(super) fn analyze_chunk_ast(module: &Module) -> ChunkAstAnalysis {
    let mut imports = HashMap::<Id, RuntimeImportInfo>::new();
    let mut declarations = Vec::new();
    let mut pre_existing_entry_exports = HashSet::<Id>::new();
    let mut destructure_siblings = BTreeMap::<String, BTreeSet<String>>::new();
    for (ordinal, item) in module.body.iter().enumerate() {
        let (names, exported) = top_level_declaration_names(item);
        let ids = top_level_declaration_ids(item);
        if !names.is_empty() {
            if exported {
                pre_existing_entry_exports.extend(ids.iter().cloned());
            }
            declarations.push(TopLevelDecl {
                ordinal,
                names,
                ids,
                exported,
            });
        }
        record_destructure_sibling_groups(item, &mut destructure_siblings);
        record_runtime_imports(item, &mut imports);
        record_pre_existing_named_exports(item, &mut pre_existing_entry_exports);
    }
    let declaration_by_name = declarations
        .iter()
        .flat_map(|decl| decl.ids.iter().map(|id| (id.clone(), decl.ordinal)))
        .collect::<HashMap<_, _>>();
    ChunkAstAnalysis {
        runtime_import_facts: RuntimeImportFacts { imports },
        declarations,
        declaration_by_name,
        destructure_siblings,
        pre_existing_entry_exports,
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
        let names = binding_names(&declarator.name);
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
pub(super) fn record_pre_existing_named_exports(item: &ModuleItem, out: &mut HashSet<Id>) {
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
        // The exported value is the local binding (`orig`); the
        // public name (`exported`) is irrelevant to the
        // emit-resolvability check, which keys off the local name.
        if let ModuleExportName::Ident(ident) = &specifier.orig {
            out.insert(ident.to_id());
        }
    }
}

pub(super) fn top_level_declaration_names(item: &ModuleItem) -> (Vec<String>, bool) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => (declaration_names(decl), false),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            (declaration_names(&export_decl.decl), true)
        }
        _ => (Vec::new(), false),
    }
}

pub(super) fn top_level_declaration_ids(item: &ModuleItem) -> Vec<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_ids(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_ids(&export_decl.decl)
        }
        _ => Vec::new(),
    }
}

pub(super) fn declaration_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(function) => vec![function.ident.sym.to_string()],
        Decl::Class(class) => vec![class.ident.sym.to_string()],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|decl| binding_names(&decl.name))
            .collect(),
        _ => Vec::new(),
    }
}

pub(super) fn declaration_ids(decl: &Decl) -> Vec<Id> {
    match decl {
        Decl::Fn(function) => vec![function.ident.to_id()],
        Decl::Class(class) => vec![class.ident.to_id()],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|decl| binding_ids(&decl.name))
            .collect(),
        _ => Vec::new(),
    }
}

pub(super) fn binding_names(pattern: &Pat) -> Vec<String> {
    match pattern {
        Pat::Ident(ident) => vec![ident.id.sym.to_string()],
        Pat::Rest(rest) => binding_names(&rest.arg),
        Pat::Assign(assign) => binding_names(&assign.left),
        Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .flat_map(binding_names)
            .collect(),
        Pat::Object(object) => object
            .props
            .iter()
            .flat_map(|prop| match prop {
                ObjectPatProp::KeyValue(key_value) => binding_names(&key_value.value),
                ObjectPatProp::Assign(assign) => vec![assign.key.id.sym.to_string()],
                ObjectPatProp::Rest(rest) => binding_names(&rest.arg),
            })
            .collect(),
        _ => Vec::new(),
    }
}

pub(super) fn binding_ids(pattern: &Pat) -> Vec<Id> {
    match pattern {
        Pat::Ident(ident) => vec![ident.id.to_id()],
        Pat::Rest(rest) => binding_ids(&rest.arg),
        Pat::Assign(assign) => binding_ids(&assign.left),
        Pat::Array(array) => array.elems.iter().flatten().flat_map(binding_ids).collect(),
        Pat::Object(object) => object
            .props
            .iter()
            .flat_map(|prop| match prop {
                ObjectPatProp::KeyValue(key_value) => binding_ids(&key_value.value),
                ObjectPatProp::Assign(assign) => vec![assign.key.to_id()],
                ObjectPatProp::Rest(rest) => binding_ids(&rest.arg),
            })
            .collect(),
        _ => Vec::new(),
    }
}
