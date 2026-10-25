use super::*;

pub(crate) fn item_purity(
    item: &ModuleItem,
    kind: StatementKind,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    graph: &ChunkCodeGraph,
    has_local_effect: bool,
) -> Purity {
    match kind {
        StatementKind::Import | StatementKind::FnDecl => Purity::Pure,
        // `export { ... }` / `export * from ...` / `export default
        // function` run no code at init — but `export default <expr>`
        // evaluates the expression and `export default class` runs
        // observable static parts (extends, static blocks, computed
        // keys), so those route through the regular classifiers.
        StatementKind::Export => match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                classify_expr_purity(
                    &default_expr.expr,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.assumptions.declared_pure,
                    graph,
                )
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_)) => match class_of_item(item) {
                Some(c)
                    if class_has_static_observable(
                        c,
                        shadowed,
                        &BTreeSet::new(),
                        &hints.assumptions.declared_pure,
                        graph,
                    ) =>
                {
                    Purity::from_reason(PurityRule::ClassStaticObservable, c.span)
                }
                _ => Purity::Pure,
            },
            _ => Purity::Pure,
        },
        StatementKind::VarDecl if has_trusted_initializer_purity(item, hints) => Purity::Pure,
        StatementKind::VarDecl if has_local_effect => Purity::Pure,
        // Top-level (non-function-body) scope: a chunk-top read of a
        // PlainData const is legitimately pure, so no PlainData name is
        // lexically shadowed here.
        StatementKind::VarDecl => var_decl_of_item(item)
            .map(|var| {
                classify_var_decl_purity(
                    var,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.assumptions.declared_pure,
                    graph,
                )
            })
            .unwrap_or(Purity::Pure),
        StatementKind::ClassDecl => match class_of_item(item) {
            Some(c)
                if class_has_static_observable(
                    c,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.assumptions.declared_pure,
                    graph,
                ) =>
            {
                Purity::from_reason(PurityRule::ClassStaticObservable, c.span)
            }
            _ => Purity::Pure,
        },
        StatementKind::SideEffect if has_local_effect => Purity::Pure,
        StatementKind::SideEffect => match item {
            ModuleItem::Stmt(Stmt::Expr(expr)) => classify_expr_purity(
                &expr.expr,
                shadowed,
                &BTreeSet::new(),
                &hints.assumptions.declared_pure,
                graph,
            ),
            // Bare blocks, control flow, loops, etc. — soundness-first.
            _ => Purity::from_reason(PurityRule::BareControlFlow, item.span()),
        },
    }
}

/// Whether this exact top-level item is the simple initialized declarator
/// carrying the initializer-only trust assertion. It deliberately does not
/// feed any call or constructor purity rule.
pub(crate) fn has_trusted_initializer_purity(item: &ModuleItem, hints: &AnalysisInputs) -> bool {
    let Some(var) = var_decl_of_item(item) else {
        return false;
    };
    if var.decls.len() != 1 {
        return false;
    }
    let declarator = &var.decls[0];
    let Pat::Ident(binding) = &declarator.name else {
        return false;
    };
    declarator.init.is_some()
        && hints
            .assumptions
            .declared_pure_initializers
            .contains(binding.id.sym.as_ref())
}
