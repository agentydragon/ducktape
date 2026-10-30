use super::*;
use crate::analysis_inputs::ProofDependencies;
use std::sync::Arc;

/// Two-state expression-level purity with structured reasons.
///
/// `Pure` means the expression is statically provably free of
/// observable side effects; `NotPure { reasons }` carries the
/// classifier rules that fired directly in the expression and its
/// sub-expressions (in source order). Known impure callees are linked
/// through shared cause nodes rather than flattened into this list. The classifier
/// previously distinguished `Impure` from `Unknown` for an internal
/// soundness argument, but downstream consumers (dependency graph
/// `has_side_effect`) collapsed both to "not pure"; this type
/// matches that contract and replaces the bool with the full
/// rationale via those links.
///
/// Reasons collected by `Purity::worst` are concatenated, so a
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
    /// Resolved by `resolve_reason_locations` once the per-chunk
    /// source-span resolver is in scope (inside
    /// `analyze_item_facts`). The classifier itself only fills
    /// `span` — the wire-emitted reason has `source_location`
    /// populated and `span` skipped.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub author_guidance: Option<String>,
    /// Report-local ID of the callee's shared cause node. Filled when a
    /// report is assembled; older reports without this field still load.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub cause_ref: Option<String>,
    /// The immutable callee verdict, shared by every call site. Keeping it
    /// out of serde prevents transitive causes from expanding on the wire.
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

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PurityRule {
    AssignOrUpdate,
    AwaitOrYield,
    DeleteOperator,
    ThrowStmt,
    DebuggerStmt,
    UnknownCall,
    /// A direct call to a chunk-top or imported function whose body was
    /// classified as impure. Record this call site once rather than copying
    /// the callee's (possibly shared) transitive reason list.
    ImpureFunctionCall,
    UnknownNew,
    UnknownMember,
    SuperProp,
    TaggedTpl,
    ArraySpread,
    ObjectSpread,
    ObjectAssignProp,
    ClassStaticObservable,
    BareControlFlow,
    /// A coercing operator (`+`, `-`, relational, loose equality,
    /// `~`, unary `+`/`-`, template interpolation, `in`,
    /// `instanceof`) whose operand is not statically known to be a
    /// primitive. ToPrimitive / ToNumber / ToString /
    /// `[Symbol.hasInstance]` / proxy-`has` on an object operand
    /// fires user code.
    CoercingOperator,
    /// A computed property key (`obj[key]`, `{[key]: v}`,
    /// `class { [key]() {} }`) whose key expression is not
    /// statically known to be a primitive (or a whitelisted
    /// `Symbol.*` well-known symbol). ToPropertyKey on an object
    /// key fires `toString` / `[Symbol.toPrimitive]`.
    ToPropertyKeyCoercion,
    /// `for-of` / `for await-of` / `for-in` — iteration fires the
    /// iterator protocol or proxy enumeration traps on the
    /// iterated value.
    IterationProtocol,
    /// A destructuring pattern (declarator name or function
    /// parameter) — object patterns fire `[[Get]]`, array patterns
    /// fire the iterator protocol on the bound value.
    DestructuringPattern,
    Other,
}

impl PurityRule {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::AssignOrUpdate => "assign_or_update",
            Self::AwaitOrYield => "await_or_yield",
            Self::DeleteOperator => "delete_operator",
            Self::ThrowStmt => "throw_stmt",
            Self::DebuggerStmt => "debugger_stmt",
            Self::UnknownCall => "unknown_call",
            Self::ImpureFunctionCall => "impure_function_call",
            Self::UnknownNew => "unknown_new",
            Self::UnknownMember => "unknown_member",
            Self::SuperProp => "super_prop",
            Self::TaggedTpl => "tagged_tpl",
            Self::ArraySpread => "array_spread",
            Self::ObjectSpread => "object_spread",
            Self::ObjectAssignProp => "object_assign_prop",
            Self::ClassStaticObservable => "class_static_observable",
            Self::BareControlFlow => "bare_control_flow",
            Self::CoercingOperator => "coercing_operator",
            Self::ToPropertyKeyCoercion => "to_property_key_coercion",
            Self::IterationProtocol => "iteration_protocol",
            Self::DestructuringPattern => "destructuring_pattern",
            Self::Other => "other",
        }
    }
}

/// The effects of evaluating source, in evaluation order, with the proof
/// dependencies of the discharges that kept other steps effect-free. `Purity`
/// is its projection: pure exactly when no event projects to a reason.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Effects {
    events: Vec<EffectEvent>,
    dependencies: ProofDependencies,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum EffectEvent {
    Effect {
        effect: Effect,
        span: Span,
        detail: Option<String>,
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
    pub(crate) fn none() -> Self {
        Self::default()
    }

    pub(crate) fn of(effect: Effect, span: Span) -> Self {
        Self::of_opt_detail(effect, span, None)
    }

    pub(crate) fn of_detail(effect: Effect, span: Span, detail: String) -> Self {
        Self::of_opt_detail(effect, span, Some(detail))
    }

    pub(crate) fn of_opt_detail(effect: Effect, span: Span, detail: Option<String>) -> Self {
        Self {
            events: vec![EffectEvent::Effect {
                effect,
                span,
                detail,
            }],
            dependencies: ProofDependencies::default(),
        }
    }

    pub(crate) fn call(callee: &Arc<Purity>, name: &str, span: Span) -> Self {
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
    pub(crate) fn then(mut self, next: Self) -> Self {
        self.events.extend(next.events);
        self.dependencies.extend(&next.dependencies);
        self
    }

    pub(crate) fn with_dependencies(mut self, additional: &ProofDependencies) -> Self {
        self.dependencies.extend(additional);
        self
    }

    pub(crate) fn is_pure(&self) -> bool {
        self.events.iter().all(|event| match event {
            EffectEvent::Effect { .. } => false,
            EffectEvent::Call { callee, .. } => callee.is_pure(),
        })
    }

    pub(crate) fn project(self) -> Purity {
        let mut dependencies = self.dependencies;
        let mut reasons = Vec::new();
        for event in self.events {
            match event {
                EffectEvent::Effect {
                    effect,
                    span,
                    detail,
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

    /// Combine two purity verdicts. Pure results union the proof
    /// dependencies that justified them; concatenating `NotPure`
    /// reasons preserves every offending sub-expression in source order.
    pub fn worst(self, other: Self) -> Self {
        match (self, other) {
            (
                Self::Pure { mut dependencies },
                Self::Pure {
                    dependencies: other_dependencies,
                },
            ) => {
                dependencies.extend(&other_dependencies);
                Self::Pure { dependencies }
            }
            (Self::Pure { .. }, other) | (other, Self::Pure { .. }) => other,
            (Purity::NotPure { reasons: mut a }, Purity::NotPure { reasons: b }) => {
                a.extend(b);
                Purity::NotPure { reasons: a }
            }
        }
    }
}

impl PurityReason {
    /// A reason with `source_location` left unresolved: the classifier fills only
    /// `span`; `resolve_reason_locations` populates `source_location` later.
    pub(crate) fn new(rule: PurityRule, span: Span, detail: Option<String>) -> Self {
        Self {
            rule,
            span,
            source_location: None,
            detail,
            cause_ref: None,
            cause: None,
            author_guidance: match rule {
                PurityRule::UnknownCall => Some(
                    "For a safe opaque call, use the member-level `purity: pure` annotation; for an imported fluent chain, declare `chunk_export_purity.<chunk>.fluent_exports`.".to_string(),
                ),
                PurityRule::UnknownNew => Some(
                    "For a safe opaque constructor, use the member-level `purity: pure_new` annotation or `chunk_export_purity.<defining chunk>.pure_new_exports` for an import.".to_string(),
                ),
                _ => None,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ValueInputRef;

    fn dependency(chunk: &str, index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: chunk.to_string(),
                input_index: index,
            }]),
            ..Default::default()
        }
    }

    #[test]
    fn combining_pure_results_unions_dependencies() {
        let combined = Purity::pure()
            .with_dependencies(&dependency("static/provider", 0))
            .worst(Purity::pure().with_dependencies(&dependency("static/other", 1)));

        assert_eq!(
            combined.dependencies(),
            Some(&ProofDependencies {
                value_inputs: BTreeSet::from([
                    ValueInputRef {
                        defining_chunk: "static/provider".to_string(),
                        input_index: 0,
                    },
                    ValueInputRef {
                        defining_chunk: "static/other".to_string(),
                        input_index: 1,
                    },
                ]),
                ..Default::default()
            })
        );
    }

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
