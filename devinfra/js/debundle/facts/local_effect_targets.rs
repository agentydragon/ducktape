use super::*;

pub(crate) fn collect_local_effects(
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    graph: &ChunkCodeGraph,
    local_effect_context: &local_effects::LocalEffectContext,
) -> BTreeSet<Id> {
    let mut out = BTreeSet::new();
    if let Some(target) =
        recognized_local_effect_target(item, &hints.assumptions.known_effects, local_effect_context)
    {
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
                &hints.assumptions.declared_pure,
                graph,
            ));
        }
    }
    out
}

fn recognized_local_effect_target(
    item: &ModuleItem,
    known_effects: &BTreeMap<String, KnownEffect>,
    context: &local_effects::LocalEffectContext,
) -> Option<Id> {
    let ModuleItem::Stmt(Stmt::Expr(expr_stmt)) = item else {
        return None;
    };
    let Expr::Call(call) = expr_stmt.expr.unwrap_parens() else {
        return None;
    };
    let callee = call_callee_ident(call)?;
    match known_effects.get(callee.sym.as_ref())? {
        KnownEffect::TypescriptDecorateHelper => typescript_decorate_helper_target(call),
        KnownEffect::TypescriptMetadataDecorateHelper
        | KnownEffect::TypescriptMetadataDecorateHelperWithMemberDecorators
        | KnownEffect::TypescriptMetadataDecorateHelperThreeArgs => {
            context.metadata_decorate_target(item).cloned()
        }
    }
}

pub(crate) struct MetadataDecorateCall {
    pub helper: Id,
    pub target: Id,
    pub decorators: Vec<Id>,
    /// A separately audited one-hop data-property lookup on an imported
    /// provider. The root is retained for dependency and mutation checks.
    pub member_decorators: Vec<Id>,
}

impl MetadataDecorateCall {
    pub(crate) fn stable_origins(&self, flow: &ChunkValueFlow) -> Option<(u32, u32)> {
        let helpers = flow.values_of_binding(&self.helper);
        let mut helpers = helpers.iter();
        let ValueOrigin::Function(helper) = helpers.next()? else {
            return None;
        };
        let targets = flow.values_of_binding(&self.target);
        let mut targets = targets.iter();
        let ValueOrigin::Class(target) = targets.next()? else {
            return None;
        };
        (helpers.next().is_none() && targets.next().is_none()).then_some((helper, target))
    }
}

/// The stronger timing assertion covers a complete member-decoration statement,
/// never calls nested in another expression or argument evaluation with effects.
pub(crate) fn metadata_decorate_call(
    item: &ModuleItem,
    known_effects: &BTreeMap<String, KnownEffect>,
) -> Option<MetadataDecorateCall> {
    let ModuleItem::Stmt(Stmt::Expr(statement)) = item else {
        return None;
    };
    let Expr::Call(call) = statement.expr.unwrap_parens() else {
        return None;
    };
    let helper = call_callee_ident(call)?;
    let effect = known_effects.get(helper.sym.as_ref())?;
    let argument_count = match effect {
        KnownEffect::TypescriptMetadataDecorateHelper
        | KnownEffect::TypescriptMetadataDecorateHelperWithMemberDecorators => 4,
        KnownEffect::TypescriptMetadataDecorateHelperThreeArgs => 3,
        _ => return None,
    };
    let member_contract =
        *effect == KnownEffect::TypescriptMetadataDecorateHelperWithMemberDecorators;
    if call.args.len() != argument_count || call.args.iter().any(|arg| arg.spread.is_some()) {
        return None;
    }
    let Expr::Array(array) = call.args[0].expr.unwrap_parens() else {
        return None;
    };
    if array.elems.is_empty() {
        return None;
    }
    let mut decorators = Vec::new();
    let mut member_decorators = Vec::new();
    for element in &array.elems {
        let element = element.as_ref()?;
        if element.spread.is_some() {
            return None;
        }
        match element.expr.unwrap_parens() {
            Expr::Ident(decorator) => decorators.push(decorator.to_id()),
            Expr::Member(member) if member_contract => {
                let (Expr::Ident(root), MemberProp::Ident(property)) =
                    (member.obj.unwrap_parens(), &member.prop)
                else {
                    return None;
                };
                if property.sym == "__proto__" || property.sym == "prototype" {
                    return None;
                }
                member_decorators.push(root.to_id());
            }
            _ => return None,
        }
    }
    let Expr::Member(prototype) = call.args[1].expr.unwrap_parens() else {
        return None;
    };
    let Expr::Ident(target) = prototype.obj.unwrap_parens() else {
        return None;
    };
    if !matches!(&prototype.prop, MemberProp::Ident(property) if property.sym == "prototype")
        || !matches!(call.args[2].expr.unwrap_parens(), Expr::Lit(Lit::Str(key))
            if key.value.to_string_lossy() != "__proto__")
        || (argument_count == 4
            && !matches!(call.args[3].expr.unwrap_parens(), Expr::Lit(Lit::Num(kind))
            if kind.value == 1.0 || kind.value == 2.0))
    {
        return None;
    }
    Some(MetadataDecorateCall {
        helper: helper.to_id(),
        target: target.to_id(),
        decorators,
        member_decorators,
    })
}

fn call_callee_ident(call: &CallExpr) -> Option<&Ident> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    match callee.unwrap_parens() {
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
    let Expr::Array(array) = expr.unwrap_parens() else {
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
    match expr.unwrap_parens() {
        Expr::Ident(_) => true,
        Expr::Member(member) => {
            matches!(&member.prop, MemberProp::Ident(_))
                && static_reference_expr(member.obj.as_ref())
        }
        _ => false,
    }
}

fn class_or_prototype_target_binding(expr: &Expr) -> Option<Id> {
    match expr.unwrap_parens() {
        Expr::Ident(ident) => Some(ident.to_id()),
        Expr::Member(member) => {
            let MemberProp::Ident(prop) = &member.prop else {
                return None;
            };
            if prop.sym.as_ref() != "prototype" {
                return None;
            }
            match member.obj.as_ref().unwrap_parens() {
                Expr::Ident(ident) => Some(ident.to_id()),
                _ => None,
            }
        }
        _ => None,
    }
}

fn decorate_property_key_is_static(expr: &Expr) -> bool {
    matches!(
        expr.unwrap_parens(),
        Expr::Lit(Lit::Str(_)) | Expr::Lit(Lit::Num(_))
    )
}

fn decorate_flags_are_static(expr: &Expr) -> bool {
    matches!(expr.unwrap_parens(), Expr::Lit(Lit::Num(_)))
}
