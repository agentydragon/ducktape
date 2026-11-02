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
    /// Native-root member reads with their exact integrity proof or the
    /// diagnostic explaining why this read did not have one.
    pub(crate) native_own_data_reads:
        &'a BTreeMap<SourceUnitOrdinal, Vec<NativeOwnDataReadDiagnostic>>,
}

pub(crate) enum NativeOwnDataReadDiagnostic {
    Proven {
        span: Span,
        dependencies: ProofDependencies,
    },
    MissingProof {
        span: Span,
        detail: String,
    },
    Unrecognized {
        span: Span,
        detail: String,
    },
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
        // Each indexed eager native-reference read uses the same semantic
        // certificate for purity as for heap and synchronous timing. A known
        // replacement or unsupported read must not inherit the older
        // name-based static-reference purity verdict.
        for diagnostic in self
            .native_own_data_reads
            .get(&fact.ordinal)
            .into_iter()
            .flatten()
        {
            effects = match diagnostic {
                NativeOwnDataReadDiagnostic::Proven { dependencies, .. } => {
                    effects.with_dependencies(dependencies)
                }
                NativeOwnDataReadDiagnostic::MissingProof { span, detail } => {
                    effects.then(Effects::of_detail(
                        Effect::Invoke(InvokeSite::MemberRead),
                        *span,
                        detail.clone(),
                    ))
                }
                NativeOwnDataReadDiagnostic::Unrecognized { span, detail } => {
                    effects.with_member_read_detail(*span, detail.clone())
                }
            };
        }
        effects
    }
}

/// The unchanged classifier obligations consumed while preparing one owner.
/// Timing completeness is independent of these evaluation-effect obligations.
pub(crate) struct PreparedPurity(Effects);

/// Final evaluation purity after retaining residual classifier obligations
/// and applying only exact existing execution certificates. This is not a
/// claim that the current timing Outcome is a complete effect ledger.
pub(crate) struct CompletedPurity {
    pub(crate) verdict: Purity,
    // NotPure's projection drops dependencies; runtime ledger evidence must
    // retain them independently of the projected verdict.
    dependencies: ProofDependencies,
}

impl PreparedPurity {
    pub(crate) fn new(effects: Effects) -> Self {
        Self(effects)
    }

    pub(crate) fn is_pure(&self) -> bool {
        self.0.is_pure()
    }

    pub(crate) fn proof_dependencies(&self) -> ProofDependencies {
        self.0.proof_dependencies()
    }

    pub(crate) fn complete(
        self,
        certificate: Option<&LocalRecordFactoryPurityCertificate>,
    ) -> (
        CompletedPurity,
        Option<crate::reports::schema::PurityCompletionDisagreement>,
    ) {
        let Some(certificate) =
            certificate.filter(|certificate| !certificate.checked_owners.is_empty())
        else {
            // Both facts have exactly the same projection without a discharge.
            return (CompletedPurity::unrefined(self.0), None);
        };
        // A disagreement is possible only for a previously pure owner.
        // Preserve that exact diagnostic projection only on this rare path.
        let prepared = self.is_pure().then(|| self.0.clone().project());
        let effects = self
            .0
            .discharge_call(certificate.call_span, &certificate.dependencies);
        let dependencies = effects.proof_dependencies();
        let completed = effects.project();
        let disagreement = prepared.filter(|_| !completed.is_pure()).map(|prepared| {
            crate::reports::schema::PurityCompletionDisagreement {
                prepared,
                completed: completed.clone(),
            }
        });
        (
            CompletedPurity {
                verdict: completed,
                dependencies,
            },
            disagreement,
        )
    }
}

impl CompletedPurity {
    /// The vendor-prune path finalizes the same classifier without executing
    /// or minting a runtime refinement certificate.
    pub(crate) fn unrefined(effects: Effects) -> Self {
        Self {
            dependencies: effects.proof_dependencies(),
            verdict: effects.project(),
        }
    }

    pub(crate) fn proof_dependencies(&self) -> &ProofDependencies {
        &self.dependencies
    }

    pub(crate) fn is_pure(&self) -> bool {
        self.verdict.is_pure()
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
