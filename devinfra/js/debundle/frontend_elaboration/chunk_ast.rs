//! Pre-naturalization analysis of one chunk AST: which top-level
//! statements declare what, which imports are runtime, which patterns
//! introduce destructure siblings, etc. `ChunkAstAnalysis` is the
//! input to selector projection and downstream planning.

use binding_targets::binding_names as bt_binding_names;
pub use binding_targets::{
    binding_name_strings as binding_names, declaration_ids,
    declaration_name_strings as declaration_names,
};
use selector_outcome::{Declaration, DeclarationKind};
use source_identity::StatementId;

use crate::source_ast::{RuntimeImportFacts, RuntimeImportInfo, record_runtime_imports};
use analysis::{DestructuringDeclaratorGroup, NestedVarStatement, SourceUnitSyntaxLocation};
use anyhow::{Context, Result, bail};
use js_ast::post_split_top_level_count;
use source_identity::SourceUnitOrdinal;
use std::collections::{HashMap, HashSet};
use swc_ecma_ast::*;

#[derive(Debug, Clone)]
pub struct TopLevelDecl {
    pub statement: StatementId,
    pub bindings: Vec<(String, Id)>,
    pub exported: bool,
}

/// Identity of one literal public export in the source module. Alias exports
/// name the local binding they export; re-exports and default/namespace exports
/// are identified by their source statement and specifier ordinal.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum ExportedNameOrigin {
    Binding(Id),
    Statement {
        statement: StatementId,
        specifier: usize,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SourceExportIdentity {
    pub public_name: String,
    pub origin: ExportedNameOrigin,
}

pub struct ChunkAstAnalysis {
    pub runtime_import_facts: RuntimeImportFacts,
    pub declarations: Vec<TopLevelDecl>,
    /// Every source statement that declares a top-level binding, in source order.
    /// `var` may redeclare the same hygienic Id at several sites; consumers that
    /// need one body_index choose a representative explicitly instead of losing the
    /// earlier sites to `HashMap::collect`'s last-write behavior.
    pub declaration_by_name: HashMap<Id, Vec<StatementId>>,
    /// Exact AST identities of top-level destructuring declarators, retained
    /// during this single source-AST pass for both request planning and final
    /// placement projection.
    pub source_unit_syntax_locations: Vec<SourceUnitSyntaxLocation>,
    pub destructuring_groups: Vec<DestructuringDeclaratorGroup>,
    pub nested_var_statements: Vec<NestedVarStatement>,
    /// Bindings that the source chunk's entry already exports (via
    /// `export { foo, bar }` re-exports of local bindings, or
    /// `export const foo = …` style declarations). The materializer
    /// consults this set in `auto_grown_residual_exports` so the
    /// auto-grown `export { name }` block doesn't duplicate an
    /// existing source-level export — emitting a duplicate would be
    /// a `SyntaxError: Duplicate export of 'name'` at load time.
    pub pre_existing_entry_exports: HashSet<Id>,
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
    pub pre_existing_public_export_names: HashSet<String>,
    /// Literal public export names in the already-resolved AST, keyed by their
    /// owning source statement. Collected in this pass rather than by reparsing
    /// source text; coarse residual planning uses the identities to decide
    /// whether independent source units can share one emitted ESM module.
    pub source_public_export_identities_by_statement:
        HashMap<StatementId, Vec<SourceExportIdentity>>,
}

pub fn analyze_chunk_ast(module: &Module) -> ChunkAstAnalysis {
    let mut imports = HashMap::<Id, RuntimeImportInfo>::new();
    let mut declarations = Vec::new();
    let mut pre_existing_entry_exports = HashSet::<Id>::new();
    let mut pre_existing_public_export_names = HashSet::<String>::new();
    let mut source_public_export_identities_by_statement =
        HashMap::<StatementId, Vec<SourceExportIdentity>>::new();
    let mut source_unit_syntax_locations = Vec::new();
    let mut destructuring_groups = Vec::new();
    let mut nested_var_statements = Vec::new();
    let mut next_source_unit_ordinal = 0usize;
    for (ordinal, item) in module.body.iter().enumerate() {
        let statement = StatementId(ordinal);
        let source_exports = source_public_export_identities(item, statement);
        if !source_exports.is_empty() {
            source_public_export_identities_by_statement.insert(statement, source_exports);
        }
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
        record_runtime_imports(item, statement, &mut imports);
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
        source_public_export_identities_by_statement,
    }
}

fn source_public_export_identities(
    item: &ModuleItem,
    statement: StatementId,
) -> Vec<SourceExportIdentity> {
    let mut identities = Vec::new();
    let mut record = |public_name, origin| {
        identities.push(SourceExportIdentity {
            public_name,
            origin,
        });
    };
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
            for (specifier_index, specifier) in named.specifiers.iter().enumerate() {
                match specifier {
                    ExportSpecifier::Named(named_specifier) => {
                        let public_name = named_specifier
                            .exported
                            .as_ref()
                            .map(binding_targets::module_export_name)
                            .unwrap_or_else(|| {
                                binding_targets::module_export_name(&named_specifier.orig)
                            });
                        let origin = if named.src.is_none()
                            && let ModuleExportName::Ident(local) = &named_specifier.orig
                        {
                            ExportedNameOrigin::Binding(local.to_id())
                        } else {
                            ExportedNameOrigin::Statement {
                                statement,
                                specifier: specifier_index,
                            }
                        };
                        record(public_name, origin);
                    }
                    ExportSpecifier::Namespace(namespace) => record(
                        binding_targets::module_export_name(&namespace.name),
                        ExportedNameOrigin::Statement {
                            statement,
                            specifier: specifier_index,
                        },
                    ),
                    ExportSpecifier::Default(default) => record(
                        default.exported.sym.to_string(),
                        ExportedNameOrigin::Statement {
                            statement,
                            specifier: specifier_index,
                        },
                    ),
                }
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => record(
            "default".to_string(),
            ExportedNameOrigin::Statement {
                statement,
                specifier: 0,
            },
        ),
        // Export declarations are represented by their declared binding
        // identities in the dependency graph; their generated binding exports
        // are added by coarse residual planning. `export *` has no literal
        // public name to collide with a named export.
        _ => {}
    }
    identities
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
pub fn binding_declaration(
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
pub fn record_pre_existing_named_exports(
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

pub fn top_level_declaration_names(item: &ModuleItem) -> (Vec<String>, bool) {
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

pub fn top_level_declaration_ids(item: &ModuleItem) -> Vec<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_ids(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_ids(&export_decl.decl)
        }
        ModuleItem::Stmt(stmt) => binding_targets::hoisted_var_ids(stmt),
        _ => Vec::new(),
    }
}

pub fn binding_ids(pattern: &Pat) -> Vec<Id> {
    bt_binding_names(pattern).collect()
}
