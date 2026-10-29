use super::*;

pub(crate) struct ItemPurityProofs<'a> {
    pub(crate) native_operations: &'a ValidatedNativeOperationFacts,
    pub(crate) fresh_record_destructuring: &'a crate::purity::FreshRecordDestructuringContext<'a>,
}

pub(crate) fn item_purity(
    item: &ModuleItem,
    kind: StatementKind,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    graph: &ChunkCodeGraph,
    proofs: &ItemPurityProofs<'_>,
    has_local_effect: bool,
) -> Purity {
    match kind {
        StatementKind::Import | StatementKind::FnDecl => Purity::pure(),
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
                Some(class) => classify_class_static_purity(
                    class,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.assumptions.declared_pure,
                    graph,
                ),
                None => Purity::pure(),
            },
            _ => Purity::pure(),
        },
        StatementKind::VarDecl if has_trusted_initializer_purity(item, hints) => Purity::pure(),
        StatementKind::VarDecl if has_local_effect => Purity::pure(),
        // Top-level (non-function-body) scope: a chunk-top read of a
        // PlainData const is legitimately pure, so no PlainData name is
        // lexically shadowed here.
        StatementKind::VarDecl => {
            if let Some(input) = symbol_for_literal_initializer(item, proofs.native_operations) {
                return Purity::pure().with_dependencies(
                    &crate::analysis_inputs::ProofDependencies {
                        native_operation_inputs: BTreeSet::from([input.evidence.reference.clone()]),
                        ..Default::default()
                    },
                );
            }
            var_decl_of_item(item)
                .map(|var| {
                    classify_var_decl_purity(
                        var,
                        shadowed,
                        &BTreeSet::new(),
                        &hints.assumptions.declared_pure,
                        graph,
                        proofs.fresh_record_destructuring,
                    )
                })
                .unwrap_or(Purity::pure())
        }
        StatementKind::ClassDecl => match class_of_item(item) {
            Some(class) => classify_class_static_purity(
                class,
                shadowed,
                &BTreeSet::new(),
                &hints.assumptions.declared_pure,
                graph,
            ),
            None => Purity::pure(),
        },
        StatementKind::SideEffect if has_local_effect => Purity::pure(),
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

/// The native-site proof applies to the complete initializer only when the
/// selected call is the entire simple binding. Other expressions retain their
/// independently classified reads, calls, and throws.
fn symbol_for_literal_initializer<'a>(
    item: &ModuleItem,
    native_facts: &'a ValidatedNativeOperationFacts,
) -> Option<&'a crate::analysis_inputs::ResolvedNativeOperationInput> {
    let var = var_decl_of_item(item)?;
    let [decl] = var.decls.as_slice() else {
        return None;
    };
    let Pat::Ident(binding) = &decl.name else {
        return None;
    };
    let Expr::Call(call) = decl.init.as_deref()?.unwrap_parens() else {
        return None;
    };
    let input = native_facts.symbol_for_literal_proof(call)?;
    (binding.id.to_id() == input.binding).then_some(input)
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
