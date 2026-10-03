use crate::chunk_ast::{binding_ids, declaration_ids};
use analysis::{AtomicUnitConflict, DepKind};
use anyhow::Result;
use artifact::{join_module_path, normalize_module_path};
use std::collections::{BTreeSet, HashMap};
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;

pub(super) fn target_file_for_request(target_dir: &str, target_path: &str) -> Result<String> {
    let normalized = normalize_module_path(target_path)?;
    let with_ext = if normalized.ends_with(".js") {
        normalized
    } else {
        format!("{normalized}.js")
    };
    Ok(join_module_path(&[target_dir, &with_ext]))
}

pub(super) fn normalize_optional_relative_dir(value: &str) -> Result<String> {
    if value.is_empty() {
        return Ok(String::new());
    }
    normalize_module_path(value)
}

/// Selection leaves at most one item: an unchanged statement or one residual
/// declaration containing the unselected declarators. No temporary Vec is needed.
pub(super) fn remaining_item_after_selection(
    item: &ModuleItem,
    binding_assignment: &HashMap<Id, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Option<ModuleItem> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            split_var_decl(var, false, binding_assignment, selected_by_module)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match &export_decl.decl {
            Decl::Var(var) => split_var_decl(var, true, binding_assignment, selected_by_module),
            decl => {
                let ids = declaration_ids(decl);
                if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
                    selected_by_module[module_index]
                        .push(ModuleItem::Stmt(Stmt::Decl(decl.clone())));
                    None
                } else {
                    Some(item.clone())
                }
            }
        },
        ModuleItem::Stmt(Stmt::Decl(decl)) => {
            let ids = declaration_ids(decl);
            if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
                selected_by_module[module_index].push(item.clone());
                None
            } else {
                Some(item.clone())
            }
        }
        _ => Some(item.clone()),
    }
}

pub(super) fn split_var_decl(
    var: &VarDecl,
    was_exported: bool,
    binding_assignment: &HashMap<Id, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Option<ModuleItem> {
    let mut residual_decls = Vec::new();
    for declarator in &var.decls {
        let ids = binding_ids(&declarator.name);
        if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
            let selected_var = VarDecl {
                // Binding comments are anchored by statement span `lo`
                // (`js_ast::emit_js_module_with_comments`), so statements split
                // from one comma list must not all inherit the list's `lo`.
                span: if var.decls.len() > 1 {
                    declarator.span
                } else {
                    var.span
                },
                ctxt: var.ctxt,
                kind: var.kind,
                declare: var.declare,
                decls: vec![declarator.clone()],
            };
            selected_by_module[module_index].push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(
                Box::new(selected_var),
            ))));
        } else {
            residual_decls.push(declarator.clone());
        }
    }
    if residual_decls.is_empty() {
        return None;
    }
    let residual_var = VarDecl {
        span: var.span,
        ctxt: var.ctxt,
        kind: var.kind,
        declare: var.declare,
        decls: residual_decls,
    };
    if was_exported {
        Some(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            span: DUMMY_SP,
            decl: Decl::Var(Box::new(residual_var)),
        })))
    } else {
        Some(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(
            residual_var,
        )))))
    }
}

pub(super) fn assigned_module_for_ids(
    ids: &[Id],
    binding_assignment: &HashMap<Id, usize>,
) -> Option<usize> {
    ids.iter()
        .filter_map(|id| binding_assignment.get(id).copied())
        .next()
}

/// Per-cause guidance for the atomic-unit-conflict bail message —
/// gives the spec author vocabulary to search for (`cycle`,
/// `side-effect`, `mutable`, `assignment`, `cross-destination`).
pub(super) fn render_atomic_unit_cause_guidance(conflicts: &[AtomicUnitConflict]) -> String {
    // `AtomicUnit::causes` is already a `BTreeSet<DepKind>` — gather
    // per-conflict causes into one `BTreeSet` so iteration stays
    // `DepKind`-`Ord`-stable without a post-collection sort.
    let causes: BTreeSet<DepKind> = conflicts
        .iter()
        .flat_map(|c| c.causes.iter().copied())
        .collect();
    let mut out = String::new();
    for cause in &causes {
        out.push_str(match cause {
            DepKind::EagerUse => {
                "EagerUse cycle: a top-level statement reads a binding at-init; \
                 splitting reader and declarer across modules forms an evaluation-order cycle. "
            }
            DepKind::EagerRebind | DepKind::LazyRebind | DepKind::DeferredRebind => {
                "Rebind: a function or top-level statement performs an assignment \
                 to a mutable binding owned by a different module — the resulting ESM \
                 import would be read-only, so this cross-destination assignment is invalid. \
                 The assigner and the binding declarer must materialize together. "
            }
            DepKind::Sequenced => {
                "Sequenced side-effect chain: two top-level side-effect statements are \
                 forced into a fixed source order; splitting them across modules \
                 inverts the run order. "
            }
            DepKind::LocalEffect => {
                "Local effect: a trusted helper call mutates a target binding \
                 (for example a TypeScript decorator application on a class prototype); \
                 the mutating statement and target binding must materialize together. "
            }
            DepKind::CoDeclaration => {
                "Shared var declaration: all source statements that declare the same `var` \
                 binding must materialize in one module. "
            }
            DepKind::LazyUse => continue,
        });
    }
    out
}
