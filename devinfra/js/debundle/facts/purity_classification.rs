use super::*;
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::effect::{Effect, HavocCause, InvokeSite};
use crate::purity::Effects;
use source_identity::SourceUnitOrdinal;

/// Everything a statement's purity transfer reads besides the statement's own
/// facts. Each input is source-only: the transfer runs at the statement's
/// program point in the synchronous runtime store (or, on the vendor-prune
/// path, outside it) and produces the purity the interpreter consumes there.
pub(crate) struct StatementPurityContext<'a> {
    pub(crate) body: &'a [TopLevelItemView<'a>],
    pub(crate) shadowed: &'a BTreeSet<&'static str>,
    pub(crate) hints: &'a AnalysisInputs,
    pub(crate) graph: &'a ChunkCodeGraph,
    pub(crate) proofs: ItemPurityProofs<'a>,
    /// `var` bindings declared by more than one statement.
    pub(crate) shared_vars: &'a BTreeSet<Id>,
    /// Exact native own-data read candidates per statement, with the
    /// certificate's dependencies when the census or an author input proves
    /// the read.
    pub(crate) native_own_data_reads:
        &'a BTreeMap<SourceUnitOrdinal, Vec<(Span, Option<ProofDependencies>)>>,
}

impl StatementPurityContext<'_> {
    pub(crate) fn statement_effects(&self, fact: &StatementFacts) -> Effects {
        let item = self.body[fact.ordinal.0].as_module_item();
        let mut effects = item_purity(
            item,
            fact.kind,
            self.shadowed,
            self.hints,
            self.graph,
            &self.proofs,
            !fact.local_effects.is_empty(),
        );
        if !fact.var_initializers.is_disjoint(self.shared_vars) {
            effects = effects.then(Effects::of_detail(
                Effect::Write,
                item.span(),
                "initializer writes a shared var binding".to_string(),
            ));
        }
        // The exact native-reference initializer uses the same semantic
        // certificate for purity as for heap and synchronous timing. A known
        // replacement or unsupported read must not inherit the older
        // name-based static-reference purity verdict.
        for (span, dependencies) in self
            .native_own_data_reads
            .get(&fact.ordinal)
            .into_iter()
            .flatten()
        {
            effects = match dependencies {
                Some(dependencies) => effects.with_dependencies(dependencies),
                None => effects.then(Effects::of(Effect::Invoke(InvokeSite::MemberRead), *span)),
            };
        }
        effects
    }
}

/// A statement's purity before and after synchronous execution refines it.
pub(crate) struct SettledPurity {
    /// The verdict execution consumed at the statement's program point; it
    /// also gates the at-init fallback.
    pub(crate) executed: Purity,
    /// `executed` with a certificate execution produced applied (a checked
    /// local record factory call drops its impure-call effect); `None` when
    /// execution refined nothing.
    pub(crate) refined: Option<Purity>,
}

impl SettledPurity {
    /// The verdict of a statement no execution refines.
    pub(crate) fn unrefined(effects: Effects) -> Self {
        Self {
            executed: effects.project(),
            refined: None,
        }
    }
}

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
) -> Effects {
    match kind {
        StatementKind::Import | StatementKind::FnDecl => Effects::none(),
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
                None => Effects::none(),
            },
            _ => Effects::none(),
        },
        StatementKind::VarDecl if has_trusted_initializer_purity(item, hints) => Effects::none(),
        StatementKind::VarDecl if has_local_effect => Effects::none(),
        // Top-level (non-function-body) scope: a chunk-top read of a
        // PlainData const is legitimately pure, so no PlainData name is
        // lexically shadowed here.
        StatementKind::VarDecl => {
            if let Some(input) = symbol_for_literal_initializer(item, proofs.native_operations) {
                return Effects::none().with_dependencies(
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
                .unwrap_or(Effects::none())
        }
        StatementKind::ClassDecl => match class_of_item(item) {
            Some(class) => classify_class_static_purity(
                class,
                shadowed,
                &BTreeSet::new(),
                &hints.assumptions.declared_pure,
                graph,
            ),
            None => Effects::none(),
        },
        StatementKind::SideEffect if has_local_effect => Effects::none(),
        StatementKind::SideEffect => match item {
            ModuleItem::Stmt(Stmt::Expr(expr)) => classify_expr_purity(
                &expr.expr,
                shadowed,
                &BTreeSet::new(),
                &hints.assumptions.declared_pure,
                graph,
            ),
            // Bare blocks, control flow, loops, etc. — soundness-first.
            _ => Effects::of(Effect::Havoc(HavocCause::ControlFlow), item.span()),
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
