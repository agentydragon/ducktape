//! Split top-level declarations between the modules that claim their bindings.

use binding_targets::{binding_names, declaration_ids};
use js_ast::VarDeclShape;
use std::collections::HashMap;
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;

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

fn split_var_decl(
    var: &VarDecl,
    was_exported: bool,
    binding_assignment: &HashMap<Id, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Option<ModuleItem> {
    let mut residual_decls = Vec::new();
    for declarator in &var.decls {
        let ids: Vec<Id> = binding_names(&declarator.name).collect();
        if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
            // Binding comments are anchored by statement span `lo`
            // (`js_ast::emit_js_module_with_comments`), so statements split
            // from one comma list must not all inherit the list's `lo`.
            let split_span = if var.decls.len() > 1 {
                declarator.span
            } else {
                var.span
            };
            let selected_var =
                VarDeclShape::of(var).with_decls(split_span, vec![declarator.clone()]);
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
    let residual_var = VarDeclShape::of(var).with_decls(var.span, residual_decls);
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

fn assigned_module_for_ids(ids: &[Id], binding_assignment: &HashMap<Id, usize>) -> Option<usize> {
    ids.iter()
        .filter_map(|id| binding_assignment.get(id).copied())
        .next()
}
