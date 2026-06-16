use super::*;

pub(crate) fn item_purity(
    item: &ModuleItem,
    kind: StatementKind,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
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
                    &hints.declared_pure,
                    graph,
                )
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_)) => match class_of_item(item) {
                Some(c)
                    if class_has_static_observable(
                        c,
                        shadowed,
                        &BTreeSet::new(),
                        &hints.declared_pure,
                        graph,
                    ) =>
                {
                    Purity::NotPure {
                        reasons: vec![PurityReason {
                            rule: PurityRule::ClassStaticObservable,
                            span: c.span,
                            source_location: None,
                            detail: None,
                        }],
                    }
                }
                _ => Purity::Pure,
            },
            _ => Purity::Pure,
        },
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
                    &hints.declared_pure,
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
                    &hints.declared_pure,
                    graph,
                ) =>
            {
                Purity::NotPure {
                    reasons: vec![PurityReason {
                        rule: PurityRule::ClassStaticObservable,
                        span: c.span,
                        source_location: None,
                        detail: None,
                    }],
                }
            }
            _ => Purity::Pure,
        },
        StatementKind::SideEffect if has_local_effect => Purity::Pure,
        StatementKind::SideEffect => match item {
            ModuleItem::Stmt(Stmt::Expr(expr)) => classify_expr_purity(
                &expr.expr,
                shadowed,
                &BTreeSet::new(),
                &hints.declared_pure,
                graph,
            ),
            // Bare blocks, control flow, loops, etc. — soundness-first.
            _ => Purity::NotPure {
                reasons: vec![PurityReason {
                    rule: PurityRule::BareControlFlow,
                    span: item.span(),
                    source_location: None,
                    detail: None,
                }],
            },
        },
    }
}
