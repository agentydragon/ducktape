use super::*;
use source_identity::SourceUnitOrdinal;

/// Inline mutation bodies whose receiver is fresh during this call. Later
/// escapes or member writes do not invalidate this call-time proof. This is
/// deliberately separate from the persistent PlainData classification.
pub(super) fn fresh_mutation_callables(
    body: &[TopLevelItemView<'_>],
    facts: &[StatementFacts],
    has_dynamic_scope: bool,
) -> BTreeSet<u32> {
    let mut writers = BTreeMap::<Id, BTreeSet<SourceUnitOrdinal>>::new();
    for fact in facts {
        for id in fact
            .declared
            .iter()
            .chain(fact.rebinds.eager.iter())
            .chain(fact.rebinds.lazy.iter())
        {
            writers.entry(id.clone()).or_default().insert(fact.ordinal);
        }
    }
    let mut fresh = BTreeSet::new();
    for (ordinal, item) in body.iter().enumerate() {
        let Some(declaration) = var_decl_of_item(item.as_module_item()) else {
            continue;
        };
        let [declarator] = declaration.decls.as_slice() else {
            continue;
        };
        let Pat::Ident(binding) = &declarator.name else {
            continue;
        };
        let Some(Expr::Call(call)) = declarator.init.as_deref().map(Expr::unwrap_parens) else {
            continue;
        };
        if !crate::purity::is_ts_enum_iife_call_for_binding(call, binding.id.sym.as_ref()) {
            continue;
        }
        let bare_fresh = matches!(call.args[0].expr.unwrap_parens(), Expr::Object(object)
            if object.props.is_empty());
        if !bare_fresh
            && (has_dynamic_scope
                || declaration.kind != VarDeclKind::Var
                || writers.get(&binding.id.to_id())
                    != Some(&BTreeSet::from([SourceUnitOrdinal(ordinal)])))
        {
            continue;
        }
        // The shared recognizer proves that the body is synchronous, only
        // writes primitive data properties of its sole parameter, and returns
        // that parameter. It excludes calls, escapes, and prototype changes.
        let Callee::Expr(callee) = &call.callee else {
            unreachable!("the shared IIFE recognizer requires an expression callee");
        };
        let key = match callee.unwrap_parens() {
            Expr::Arrow(arrow) => arrow.span.lo.0,
            Expr::Fn(function) => function.function.span.lo.0,
            _ => unreachable!("the shared IIFE recognizer requires an inline callable"),
        };
        fresh.insert(key);
    }
    fresh
}
