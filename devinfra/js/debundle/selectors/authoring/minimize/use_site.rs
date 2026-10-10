//! Use-site fallback for declarations with no distinguishing self anchor.
//!
//! A named object property can describe the role of an otherwise identical
//! declaration. The property value is emitted as a free selector identifier;
//! the existing resolver then claims the declaration that identifier binds to.

use std::collections::BTreeSet;

use anyhow::Result;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::finish_minimized_selector;
use crate::render::{anything_expr, emit_selector, hole_object_padded, ident_node, named_pat};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
};

use source_match_holes::{ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD};

fn single_object_initializer(item: &ModuleItem) -> Option<(&VarDecl, &ObjectLit)> {
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = item else {
        return None;
    };
    let [declarator] = var.decls.as_slice() else {
        return None;
    };
    let Some(Expr::Object(object)) = declarator.init.as_deref() else {
        return None;
    };
    Some((var, object))
}

fn named_property_value(prop: &PropOrSpread) -> Option<(&KeyValueProp, &Ident)> {
    let PropOrSpread::Prop(prop) = prop else {
        return None;
    };
    let Prop::KeyValue(key_value) = prop.as_ref() else {
        return None;
    };
    if !matches!(&key_value.key, PropName::Ident(_) | PropName::Str(_)) {
        return None;
    }
    let Expr::Ident(value) = key_value.value.as_ref() else {
        return None;
    };
    Some((key_value, value))
}

/// Direct references through named object properties, indexed once per chunk.
pub(crate) fn named_object_use_bindings(item: &ModuleItem) -> BTreeSet<String> {
    let Some((_, object)) = single_object_initializer(item) else {
        return BTreeSet::new();
    };
    object
        .props
        .iter()
        .filter_map(named_property_value)
        .map(|(_, value)| value.sym.to_string())
        .collect()
}

fn top_level_call(item: &ModuleItem) -> Option<(&ExprStmt, &CallExpr)> {
    let ModuleItem::Stmt(Stmt::Expr(stmt)) = item else {
        return None;
    };
    let Expr::Call(call) = stmt.expr.as_ref() else {
        return None;
    };
    Some((stmt, call))
}

/// Direct references passed to a top-level call beside a literal argument.
/// The literal identifies the call without relying on source adjacency.
pub(crate) fn call_use_bindings(item: &ModuleItem) -> BTreeSet<String> {
    let Some((_, call)) = top_level_call(item) else {
        return BTreeSet::new();
    };
    if !call
        .args
        .iter()
        .any(|arg| arg.spread.is_none() && matches!(arg.expr.as_ref(), Expr::Lit(Lit::Str(_))))
    {
        return BTreeSet::new();
    }
    call.args
        .iter()
        .filter(|arg| arg.spread.is_none())
        .filter_map(|arg| match arg.expr.as_ref() {
            Expr::Ident(value) => Some(value.sym.to_string()),
            _ => None,
        })
        .collect()
}

fn args_hole() -> ExprOrSpread {
    ExprOrSpread {
        spread: None,
        expr: Box::new(Expr::Ident(ident_node(ARGS_HOLE_KEYWORD))),
    }
}

/// Select a declaration by a direct call argument next to a string marker.
/// `ARGS` gaps allow unrelated arguments to move without pinning call arity.
pub(super) fn render_via_call_use_site(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
) -> Result<Option<SpecializedSelector>> {
    let Some(sites) = index.call_use_sites.get(&target.runtime_binding) else {
        return Ok(None);
    };
    for &body_idx in sites {
        let Some((stmt, call)) = index.module.body.get(body_idx).and_then(top_level_call) else {
            continue;
        };
        for (string_idx, string_arg) in call.args.iter().enumerate() {
            if string_arg.spread.is_some()
                || !matches!(string_arg.expr.as_ref(), Expr::Lit(Lit::Str(_)))
            {
                continue;
            }
            for (target_idx, target_arg) in call.args.iter().enumerate() {
                if target_idx == string_idx || target_arg.spread.is_some() {
                    continue;
                }
                if !matches!(target_arg.expr.as_ref(), Expr::Ident(value) if value.sym == *target.runtime_binding)
                {
                    continue;
                }
                let target_arg = ExprOrSpread {
                    spread: None,
                    expr: Box::new(Expr::Ident(ident_node(&target.selector_local))),
                };
                let mut holed = call.clone();
                holed.callee = Callee::Expr(Box::new(anything_expr()));
                holed.args = vec![args_hole()];
                if string_idx < target_idx {
                    holed.args.push(string_arg.clone());
                    holed.args.push(args_hole());
                    holed.args.push(target_arg);
                } else {
                    holed.args.push(target_arg);
                    holed.args.push(args_hole());
                    holed.args.push(string_arg.clone());
                }
                holed.args.push(args_hole());
                let mut statement = stmt.clone();
                statement.expr = Box::new(Expr::Call(holed));
                let source = emit_selector(ModuleItem::Stmt(Stmt::Expr(statement)))?;
                if let Some(selector) = finish_minimized_selector(index, decl, target, source)? {
                    return Ok(Some(selector));
                }
            }
        }
    }
    Ok(None)
}

/// Render `{ role: runtimeBinding }` as a padded object-key anchor whose value
/// is the target's free selector identifier. Only a proven match is returned.
pub(super) fn render_via_named_object_use_site(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
) -> Result<Option<SpecializedSelector>> {
    let Some(sites) = index.named_object_use_sites.get(&target.runtime_binding) else {
        return Ok(None);
    };
    for &body_idx in sites {
        let Some((var, object)) = index
            .module
            .body
            .get(body_idx)
            .and_then(single_object_initializer)
        else {
            continue;
        };
        for prop in &object.props {
            let Some((key_value, value)) = named_property_value(prop) else {
                continue;
            };
            if value.sym != *target.runtime_binding {
                continue;
            }
            let key_span = key_value.key.span();
            let kept = BTreeSet::from([(key_span.lo.0, key_span.hi.0)]);
            let mut holed_object = hole_object_padded(object, &kept);
            for prop in &mut holed_object.props {
                let PropOrSpread::Prop(prop) = prop else {
                    continue;
                };
                let Prop::KeyValue(holed_value) = prop.as_mut() else {
                    continue;
                };
                if holed_value.key.span() == key_span {
                    *holed_value.value = Expr::Ident(ident_node(&target.selector_local));
                }
            }

            let mut holed_var = var.clone();
            let mut declarator = holed_var.decls[0].clone();
            declarator.name = named_pat(ANYTHING_HOLE_KEYWORD);
            declarator.init = Some(Box::new(Expr::Object(holed_object)));
            holed_var.decls = vec![declarator];
            let source =
                emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(holed_var)))))?;
            if let Some(selector) = finish_minimized_selector(index, decl, target, source)? {
                return Ok(Some(selector));
            }
        }
    }
    Ok(None)
}
