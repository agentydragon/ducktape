//! Pre-naturalization analysis of one chunk AST: which top-level
//! statements declare what, which imports are runtime, which patterns
//! introduce destructure siblings, etc. `ChunkAstAnalysis` is the
//! input to `materialize_logical_chunk`s spec-driven plan resolution.

use binding_targets::binding_names as bt_binding_names;
pub(super) use binding_targets::{
    binding_name_strings as binding_names, declaration_ids,
    declaration_name_strings as declaration_names,
};
use selector_outcome::{Declaration, DeclarationKind};
use source_identity::StatementId;

use crate::imports::{RuntimeImportFacts, RuntimeImportInfo, record_runtime_imports};
use analysis::{DestructuringDeclaratorGroup, NestedVarStatement, SourceUnitSyntaxLocation};
use anyhow::{Context, Result, bail};
use js_ast::post_split_top_level_count;
use source_identity::SourceUnitOrdinal;
use std::collections::{HashMap, HashSet};
use swc_ecma_ast::*;

#[derive(Debug, Clone)]
pub(super) struct TopLevelDecl {
    pub(super) statement: StatementId,
    pub(super) bindings: Vec<(String, Id)>,
    pub(super) exported: bool,
}

pub(super) struct ChunkAstAnalysis {
    pub(super) runtime_import_facts: RuntimeImportFacts,
    pub(super) declarations: Vec<TopLevelDecl>,
    /// Every source statement that declares a top-level binding, in source order.
    /// `var` may redeclare the same hygienic Id at several sites; consumers that
    /// need one body_index choose a representative explicitly instead of losing the
    /// earlier sites to `HashMap::collect`'s last-write behavior.
    pub(super) declaration_by_name: HashMap<Id, Vec<StatementId>>,
    /// Exact AST identities of top-level destructuring declarators, retained
    /// during this single source-AST pass for both request planning and final
    /// placement projection.
    pub(super) source_unit_syntax_locations: Vec<SourceUnitSyntaxLocation>,
    pub(super) destructuring_groups: Vec<DestructuringDeclaratorGroup>,
    pub(super) nested_var_statements: Vec<NestedVarStatement>,
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
    let mut source_unit_syntax_locations = Vec::new();
    let mut destructuring_groups = Vec::new();
    let mut nested_var_statements = Vec::new();
    let mut next_source_unit_ordinal = 0usize;
    for (ordinal, item) in module.body.iter().enumerate() {
        let (names, exported) = top_level_declaration_names(item);
        let ids = top_level_declaration_ids(item);
        if !names.is_empty() {
            let bindings: Vec<(String, Id)> = names.into_iter().zip(ids.iter().cloned()).collect();
            if exported {
                pre_existing_entry_exports.extend(bindings.iter().map(|(_, id)| id.clone()));
                // `export const foo = …` / `export function foo()` /
                // `export class Foo {}` — the declared name is also
                // the public name.
                pre_existing_public_export_names
                    .extend(bindings.iter().map(|(name, _)| name.clone()));
            }
            declarations.push(TopLevelDecl {
                statement: StatementId(ordinal),
                bindings,
                exported,
            });
        }
        record_exact_destructuring_groups(
            item,
            StatementId(ordinal),
            SourceUnitOrdinal(next_source_unit_ordinal),
            &mut destructuring_groups,
        );
        let count = post_split_top_level_count(item);
        let variable_declaration = matches!(
            item,
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(_)))
                | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                    decl: Decl::Var(_),
                    ..
                }))
        );
        source_unit_syntax_locations.extend((0..count).map(|offset| SourceUnitSyntaxLocation {
            ordinal: SourceUnitOrdinal(next_source_unit_ordinal + offset),
            statement: StatementId(ordinal),
            declarator_offset: variable_declaration.then_some(offset),
        }));
        next_source_unit_ordinal += count;
        if matches!(item, ModuleItem::Stmt(statement) if !matches!(statement, Stmt::Decl(_)))
            && !ids.is_empty()
        {
            nested_var_statements.push(NestedVarStatement {
                statement: StatementId(ordinal),
                bindings: ids,
            });
        }
        record_runtime_imports(item, &mut imports);
        record_pre_existing_named_exports(
            item,
            &mut pre_existing_entry_exports,
            &mut pre_existing_public_export_names,
        );
    }
    let mut declaration_by_name = HashMap::<Id, Vec<StatementId>>::new();
    for decl in &declarations {
        for (_, id) in &decl.bindings {
            declaration_by_name
                .entry(id.clone())
                .or_default()
                .push(decl.statement);
        }
    }
    ChunkAstAnalysis {
        runtime_import_facts: RuntimeImportFacts { imports },
        declarations,
        declaration_by_name,
        source_unit_syntax_locations,
        destructuring_groups,
        nested_var_statements,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    }
}

fn record_exact_destructuring_groups(
    item: &ModuleItem,
    statement: StatementId,
    first_ordinal: SourceUnitOrdinal,
    out: &mut Vec<DestructuringDeclaratorGroup>,
) {
    let decl = match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
        _ => return,
    };
    let Decl::Var(var) = decl else {
        return;
    };
    for (declarator_offset, declarator) in var.decls.iter().enumerate() {
        let bindings = binding_ids(&declarator.name);
        if bindings.len() < 2 {
            continue;
        }
        out.push(DestructuringDeclaratorGroup {
            ordinal: SourceUnitOrdinal(first_ordinal.0 + declarator_offset),
            statement,
            declarator_offset,
            bindings,
        });
    }
}

/// The first top-level statement that declares `binding`, or else the import
/// that binds it. The full source-ordered declaration-site list stays in
/// `declaration_by_name`; this helper returns one representative for diagnostics
/// whose wire format names a single declaration.
pub(super) fn binding_declaration(
    body: &[ModuleItem],
    declaration_by_name: &HashMap<Id, Vec<StatementId>>,
    binding: &Id,
) -> Result<Declaration> {
    let body_index = match declaration_by_name
        .get(binding)
        .and_then(|sites| sites.first())
    {
        Some(&ordinal) => ordinal,
        None => StatementId(
            body.iter()
                .position(|item| {
                    let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                        return false;
                    };
                    import.specifiers.iter().any(|specifier| {
                        let local = match specifier {
                            ImportSpecifier::Named(named) => &named.local,
                            ImportSpecifier::Default(default) => &default.local,
                            ImportSpecifier::Namespace(namespace) => &namespace.local,
                        };
                        local.to_id() == *binding
                    })
                })
                .with_context(|| format!("no top-level statement declares `{}`", binding.0))?,
        ),
    };
    let decl = match &body[body_index.0] {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => {
            return Ok(Declaration {
                body_index: body_index.0,
                kind: DeclarationKind::Import,
            });
        }
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
        // A `var` hoisted out of a block, loop or `try`.
        ModuleItem::Stmt(_) => {
            return Ok(Declaration {
                body_index: body_index.0,
                kind: DeclarationKind::Var,
            });
        }
        ModuleItem::ModuleDecl(_) => bail!("body[{body_index}] is not a declaration"),
    };
    let kind = match decl {
        Decl::Fn(_) => DeclarationKind::Function,
        Decl::Class(_) => DeclarationKind::Class,
        Decl::Var(var) => match var.kind {
            VarDeclKind::Var => DeclarationKind::Var,
            VarDeclKind::Let => DeclarationKind::Let,
            VarDeclKind::Const => DeclarationKind::Const,
        },
        Decl::Using(using) if using.is_await => DeclarationKind::AwaitUsing,
        Decl::Using(_) => DeclarationKind::Using,
        _ => bail!("body[{body_index}] is a TypeScript declaration"),
    };
    Ok(Declaration {
        body_index: body_index.0,
        kind,
    })
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
        let public_name = specifier
            .exported
            .as_ref()
            .map(binding_targets::module_export_name)
            .unwrap_or_else(|| orig_ident.sym.to_string());
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

pub(super) fn binding_ids(pattern: &Pat) -> Vec<Id> {
    bt_binding_names(pattern).collect()
}
