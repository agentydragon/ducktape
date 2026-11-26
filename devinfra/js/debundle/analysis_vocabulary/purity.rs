use crate::effect::{CoerceOperand, CoerceOperands, CoerceSite, Effect, HavocCause, InvokeSite};
use crate::execution_evidence::SourceLocation;
use crate::inputs::ProofDependencies;
use analysis_model::PrimKinds;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::Id;

pub use saved_purity_proto::debundle::saved_purity::PurityRule;

/// Two-state expression-level purity with structured reasons.
///
/// `Pure` means the expression is statically provably free of
/// observable side effects; `NotPure { reasons }` carries the
/// classifier rules that fired directly in the expression and its
/// sub-expressions (in source order). Known impure callees are linked
/// through shared cause nodes rather than flattened into this list.
/// Downstream consumers (dependency graph `has_side_effect`) treat every
/// non-`Pure` verdict alike, so this type matches that contract and
/// carries the full rationale via those links instead of a bool.
///
/// `Effects::project` keeps one reason per effect, so a
/// composite like `f() + g()` records both `UnknownCall` reasons
/// (with their respective spans), rather than only the first. A call
/// to a known impure function contributes one `ImpureFunctionCall`
/// reason at its own call site; copying the callee's transitive list
/// would duplicate reasons along branching call graphs.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Purity {
    Pure {
        #[serde(default, skip_serializing_if = "ProofDependencies::is_empty")]
        dependencies: ProofDependencies,
    },
    NotPure {
        reasons: Vec<PurityReason>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PurityReason {
    pub rule: PurityRule,
    /// Resolved from `span` where the per-chunk `SourceLocator` is in scope
    /// (`facts/analyze.rs`, `purity/code_graph.rs`). The classifier itself
    /// only fills `span` — the wire-emitted reason has `source_location`
    /// populated and `span` skipped.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author_guidance: Option<String>,
    /// Report-local ID of the callee's shared cause node. Filled when a
    /// report is assembled; older reports without this field still load.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub cause_ref: Option<String>,
    /// The immutable callee verdict, shared by every call site. Reports use
    /// `cause_ref`; the saved representation keeps this edge explicitly.
    #[serde(skip)]
    pub cause: Option<Arc<Purity>>,
    #[serde(skip)]
    pub span: Span,
}

impl PartialEq for PurityReason {
    fn eq(&self, other: &Self) -> bool {
        self.rule == other.rule
            && self.source_location == other.source_location
            && self.detail == other.detail
            && self.author_guidance == other.author_guidance
            && self.cause_ref == other.cause_ref
            && self.span == other.span
            && match (&self.cause, &other.cause) {
                (None, None) => true,
                (Some(a), Some(b)) => Arc::ptr_eq(a, b),
                _ => false,
            }
    }
}

impl Eq for PurityReason {}

impl PurityReason {
    pub(crate) fn with_cause(mut self, cause: Arc<Purity>) -> Self {
        self.cause = Some(cause);
        self
    }
}

/// The effects of evaluating source, in evaluation order, with the proof
/// dependencies of the discharges that kept other steps effect-free. `Purity`
/// is its projection: pure exactly when no event projects to a reason.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Effects {
    events: Vec<EffectEvent>,
    dependencies: ProofDependencies,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum EffectEvent {
    Effect {
        effect: Effect,
        span: Span,
        detail: Option<String>,
        /// For a `Coerce`, the bindings whose settled values can resolve it.
        operands: Option<CoerceOperands>,
    },
    /// A call of a function whose own effects are already projected. Pure
    /// callees contribute their dependencies; an impure callee is one
    /// `ImpureFunctionCall` reason at this site, linked to its verdict.
    Call {
        callee: Arc<Purity>,
        name: String,
        span: Span,
    },
}

impl Effects {
    pub fn none() -> Self {
        Self::default()
    }

    pub fn of(effect: Effect, span: Span) -> Self {
        Self::of_opt_detail(effect, span, None)
    }

    pub fn of_detail(effect: Effect, span: Span, detail: String) -> Self {
        Self::of_opt_detail(effect, span, Some(detail))
    }

    pub fn of_opt_detail(effect: Effect, span: Span, detail: Option<String>) -> Self {
        Self {
            events: vec![EffectEvent::Effect {
                effect,
                span,
                detail,
                operands: None,
            }],
            dependencies: ProofDependencies::default(),
        }
    }

    /// Add context to an existing member-read reason at `span`, without
    /// changing its effect or position in evaluation order.
    pub fn with_member_read_detail(mut self, span: Span, detail: String) -> Self {
        for event in &mut self.events {
            if let EffectEvent::Effect {
                effect: Effect::Invoke(InvokeSite::MemberRead),
                span: event_span,
                detail: event_detail,
                ..
            } = event
                && *event_span == span
            {
                *event_detail = Some(detail.clone());
            }
        }
        self
    }

    /// A coercion at `span` that `operands`, when present, can resolve
    /// against the values settled at the statement's program point.
    pub fn coerce(
        site: CoerceSite,
        span: Span,
        detail: String,
        operands: Option<CoerceOperands>,
    ) -> Self {
        Self {
            events: vec![EffectEvent::Effect {
                effect: Effect::Coerce(site),
                span,
                detail: Some(detail),
                operands,
            }],
            dependencies: ProofDependencies::default(),
        }
    }

    pub fn call(callee: &Arc<Purity>, name: &str, span: Span) -> Self {
        Self {
            events: vec![EffectEvent::Call {
                callee: Arc::clone(callee),
                name: name.to_string(),
                span,
            }],
            dependencies: ProofDependencies::default(),
        }
    }

    /// Evaluate `self`, then `next`.
    pub fn then(mut self, next: Self) -> Self {
        self.events.extend(next.events);
        self.dependencies.extend(&next.dependencies);
        self
    }

    pub fn with_dependencies(mut self, additional: &ProofDependencies) -> Self {
        self.dependencies.extend(additional);
        self
    }

    pub fn proof_dependencies(&self) -> ProofDependencies {
        let mut dependencies = self.dependencies.clone();
        for event in &self.events {
            if let EffectEvent::Call { callee, .. } = event
                && let Some(callee_dependencies) = callee.dependencies()
            {
                dependencies.extend(callee_dependencies);
            }
        }
        dependencies
    }

    /// Discharge the call of an impure function at `span` when it is the only
    /// such call there: a certificate proved that call's execution adds no
    /// observable effect. Every other effect stays.
    pub fn discharge_call(mut self, span: Span, dependencies: &ProofDependencies) -> Self {
        let discharged = |event: &EffectEvent| {
            matches!(event, EffectEvent::Call { callee, span: site, .. }
                if *site == span && !callee.is_pure())
        };
        if self.events.iter().filter(|event| discharged(event)).count() == 1 {
            self.events.retain(|event| !discharged(event));
            self.dependencies.extend(dependencies);
        }
        self
    }

    /// Remove only the classifier's member-read obligation at an exact
    /// runtime-certified site. Ambiguous duplicate events retain the reason.
    pub fn discharge_private_own_field_reads(
        mut self,
        spans: &[Span],
        dependencies: &ProofDependencies,
    ) -> Self {
        for span in spans {
            let matches = |event: &EffectEvent| {
                matches!(event, EffectEvent::Effect {
                    effect: Effect::Invoke(InvokeSite::MemberRead),
                    span: site, ..
                } if site == span)
            };
            if self.events.iter().filter(|event| matches(event)).count() == 1 {
                self.events.retain(|event| !matches(event));
                self.dependencies.extend(dependencies);
            }
        }
        self
    }

    /// Every binding a coercion's resolution reads.
    pub fn coerce_bindings(&self, bindings: &mut BTreeSet<Id>) {
        for event in &self.events {
            if let EffectEvent::Effect {
                operands: Some(operands),
                ..
            } = event
            {
                bindings.extend(
                    operands
                        .operands
                        .iter()
                        .filter_map(CoerceOperand::binding)
                        .cloned(),
                );
            }
        }
    }

    /// Resolve each coercion whose operands `settled` proves are primitives
    /// of types its check admits: the step runs no user code and cannot
    /// throw, so it is no effect. `settled` answers for the program point
    /// these effects start at, and only for operands the evaluation itself
    /// cannot change before the coercion without another effect.
    pub fn resolve_coercions(
        mut self,
        settled: impl Fn(&CoerceOperand) -> Option<PrimKinds>,
    ) -> Self {
        self.events.retain(|event| match event {
            EffectEvent::Effect {
                operands: Some(operands),
                ..
            } => !operands
                .operands
                .iter()
                .map(|operand| match operand {
                    CoerceOperand::Known(kinds) => Some(*kinds),
                    _ => settled(operand),
                })
                .collect::<Option<Vec<_>>>()
                .is_some_and(|kinds| operands.resolved_by(&kinds)),
            _ => true,
        });
        self
    }

    pub fn is_pure(&self) -> bool {
        self.events.iter().all(|event| match event {
            EffectEvent::Effect { .. } => false,
            EffectEvent::Call { callee, .. } => callee.is_pure(),
        })
    }

    pub fn project(self) -> Purity {
        let mut dependencies = self.dependencies;
        let mut reasons = Vec::new();
        for event in self.events {
            match event {
                EffectEvent::Effect {
                    effect,
                    span,
                    detail,
                    ..
                } => reasons.push(PurityReason::new(rule(effect), span, detail)),
                EffectEvent::Call { callee, name, span } => match callee.as_ref() {
                    Purity::Pure {
                        dependencies: callee_dependencies,
                    } => dependencies.extend(callee_dependencies),
                    Purity::NotPure { .. } => reasons.push(
                        PurityReason::new(
                            PurityRule::ImpureFunctionCall,
                            span,
                            Some(format!("function `{name}` has an impure body")),
                        )
                        .with_cause(callee),
                    ),
                },
            }
        }
        if reasons.is_empty() {
            Purity::Pure { dependencies }
        } else {
            Purity::NotPure { reasons }
        }
    }
}

impl Effects {
    /// The effects in evaluation order, as unit tests assert them; a call of an
    /// impure function appears as `Invoke(Call)` and a call of a pure one not at all.
    pub fn kinds(&self) -> Vec<Effect> {
        self.events
            .iter()
            .filter_map(|event| match event {
                EffectEvent::Effect { effect, .. } => Some(*effect),
                EffectEvent::Call { callee, .. } => {
                    (!callee.is_pure()).then_some(Effect::Invoke(InvokeSite::Call))
                }
            })
            .collect()
    }
}

/// The reason rule reporting `effect`.
fn rule(effect: Effect) -> PurityRule {
    match effect {
        Effect::Write => PurityRule::AssignOrUpdate,
        Effect::Delete => PurityRule::DeleteOperator,
        Effect::Coerce(CoerceSite::Operator) => PurityRule::CoercingOperator,
        Effect::Coerce(CoerceSite::PropertyKey) => PurityRule::ToPropertyKeyCoercion,
        Effect::Coerce(CoerceSite::Iteration) => PurityRule::IterationProtocol,
        Effect::Coerce(CoerceSite::Destructuring) => PurityRule::DestructuringPattern,
        Effect::Coerce(CoerceSite::ArraySpread) => PurityRule::ArraySpread,
        Effect::Coerce(CoerceSite::ObjectSpread) => PurityRule::ObjectSpread,
        Effect::Invoke(InvokeSite::Call) => PurityRule::UnknownCall,
        Effect::Invoke(InvokeSite::Construct) => PurityRule::UnknownNew,
        Effect::Invoke(InvokeSite::TaggedTemplate) => PurityRule::TaggedTpl,
        Effect::Invoke(InvokeSite::MemberRead) => PurityRule::UnknownMember,
        Effect::Invoke(InvokeSite::SuperProperty) => PurityRule::SuperProp,
        Effect::Invoke(InvokeSite::ClassDefinition) => PurityRule::ClassStaticObservable,
        Effect::Throws | Effect::Havoc(HavocCause::Unmodeled) => PurityRule::Other,
        Effect::Havoc(HavocCause::Suspension) => PurityRule::AwaitOrYield,
        Effect::Havoc(HavocCause::ThrowStatement) => PurityRule::ThrowStmt,
        Effect::Havoc(HavocCause::Debugger) => PurityRule::DebuggerStmt,
        Effect::Havoc(HavocCause::ControlFlow) => PurityRule::BareControlFlow,
        Effect::Havoc(HavocCause::CoverInitializedName) => PurityRule::ObjectAssignProp,
        Effect::ObservesForeignCell => PurityRule::OrderDependentRead,
    }
}

impl Purity {
    pub fn pure() -> Self {
        Self::Pure {
            dependencies: ProofDependencies::default(),
        }
    }

    pub fn is_pure(&self) -> bool {
        matches!(self, Purity::Pure { .. })
    }

    pub fn dependencies(&self) -> Option<&ProofDependencies> {
        match self {
            Self::Pure { dependencies } => Some(dependencies),
            Self::NotPure { .. } => None,
        }
    }

    pub fn with_dependencies(mut self, additional: &ProofDependencies) -> Self {
        if let Self::Pure { dependencies } = &mut self {
            dependencies.extend(additional);
        }
        self
    }
}

impl PurityReason {
    /// A reason with `source_location` left unresolved: the classifier fills only
    /// `span`; a caller holding the `SourceLocator` populates `source_location`
    /// later.
    pub fn new(rule: PurityRule, span: Span, detail: Option<String>) -> Self {
        Self {
            rule,
            span,
            source_location: None,
            detail,
            cause_ref: None,
            cause: None,
            author_guidance: match rule {
                PurityRule::UnknownCall => Some(
                    "Inspect `synchronous_execution.precision_losses` and the callee's source before choosing an audited initialization contract. `purity: pure` supplies neither a missing call result nor heap identity.".to_string(),
                ),
                PurityRule::UnknownNew => Some(
                    "Inspect `synchronous_execution.precision_losses` and the constructor's source before choosing an audited initialization contract. `purity: pure_new` supplies neither instance shape nor heap identity.".to_string(),
                ),
                _ => None,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_pure_dependencies_preserve_existing_wire_shape() {
        let serialized = serde_json::to_value(Purity::pure()).unwrap();
        assert_eq!(serialized, serde_json::json!({ "kind": "pure" }));
        assert_eq!(
            serde_json::from_value::<Purity>(serde_json::json!({ "kind": "pure" })).unwrap(),
            Purity::pure()
        );
    }
}
