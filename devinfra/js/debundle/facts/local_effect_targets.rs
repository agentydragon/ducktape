use super::*;

pub(crate) fn collect_local_effects(
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    graph: &ChunkCodeGraph,
    local_effect_context: &local_effects::LocalEffectContext,
) -> BTreeSet<Id> {
    let mut out = BTreeSet::new();
    if let Some(target) = recognized_local_effect_target(item, &hints.known_effects) {
        out.insert(target);
    }
    match hints.local_effect_policy {
        LocalEffectPolicy::KnownEffectsOnly => {}
        LocalEffectPolicy::VendorPrune => {
            out.extend(local_effect_context.local_effect_targets(item));
        }
        LocalEffectPolicy::LocalPropertyWrites => {
            out.extend(local_effect_context.local_property_write_targets(
                item,
                shadowed,
                &hints.declared_pure,
                graph,
            ));
        }
    }
    out
}

fn recognized_local_effect_target(
    item: &ModuleItem,
    known_effects: &BTreeMap<String, KnownEffect>,
) -> Option<Id> {
    let ModuleItem::Stmt(Stmt::Expr(expr_stmt)) = item else {
        return None;
    };
    let Expr::Call(call) = strip_parens(&expr_stmt.expr) else {
        return None;
    };
    let callee = call_callee_ident(call)?;
    if known_effects.get(callee.sym.as_ref()) != Some(&KnownEffect::TypescriptDecorateHelper) {
        return None;
    }
    typescript_decorate_helper_target(call)
}

fn call_callee_ident(call: &CallExpr) -> Option<&Ident> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    match strip_parens(callee) {
        Expr::Ident(ident) => Some(ident),
        _ => None,
    }
}

fn typescript_decorate_helper_target(call: &CallExpr) -> Option<Id> {
    if call.args.iter().any(|arg| arg.spread.is_some()) {
        return None;
    }
    match call.args.len() {
        2 => {
            if !decorator_array_is_static_reference_list(&call.args[0].expr) {
                return None;
            }
            class_or_prototype_target_binding(&call.args[1].expr)
        }
        4 => {
            if !decorator_array_is_static_reference_list(&call.args[0].expr)
                || !decorate_property_key_is_static(&call.args[2].expr)
                || !decorate_flags_are_static(&call.args[3].expr)
            {
                return None;
            }
            class_or_prototype_target_binding(&call.args[1].expr)
        }
        _ => None,
    }
}
fn decorator_array_is_static_reference_list(expr: &Expr) -> bool {
    let Expr::Array(array) = strip_parens(expr) else {
        return false;
    };
    array.elems.iter().all(|elem| {
        let Some(elem) = elem else {
            return false;
        };
        elem.spread.is_none() && static_reference_expr(&elem.expr)
    })
}

fn static_reference_expr(expr: &Expr) -> bool {
    match strip_parens(expr) {
        Expr::Ident(_) => true,
        Expr::Member(member) => {
            matches!(&member.prop, MemberProp::Ident(_))
                && static_reference_expr(member.obj.as_ref())
        }
        _ => false,
    }
}

fn class_or_prototype_target_binding(expr: &Expr) -> Option<Id> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.to_id()),
        Expr::Member(member) => {
            let MemberProp::Ident(prop) = &member.prop else {
                return None;
            };
            if prop.sym.as_ref() != "prototype" {
                return None;
            }
            match strip_parens(member.obj.as_ref()) {
                Expr::Ident(ident) => Some(ident.to_id()),
                _ => None,
            }
        }
        _ => None,
    }
}

fn decorate_property_key_is_static(expr: &Expr) -> bool {
    matches!(
        strip_parens(expr),
        Expr::Lit(Lit::Str(_)) | Expr::Lit(Lit::Num(_))
    )
}

fn decorate_flags_are_static(expr: &Expr) -> bool {
    matches!(strip_parens(expr), Expr::Lit(Lit::Num(_)))
}
