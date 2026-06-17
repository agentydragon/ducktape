use super::*;

/// Two-state expression-level purity with structured reasons.
///
/// `Pure` means the expression is statically provably free of
/// observable side effects; `NotPure { reasons }` carries the
/// list of every classifier rule that fired against the expression
/// or one of its sub-expressions (in source order). The classifier
/// previously distinguished `Impure` from `Unknown` for an internal
/// soundness argument, but downstream consumers (owner-graph
/// `has_side_effect`) collapsed both to "not pure"; this type
/// matches that contract and replaces the bool with the full
/// rationale.
///
/// Reasons collected by `Purity::worst` are concatenated, so a
/// composite like `f() + g()` records both `UnknownCall` reasons
/// (with their respective spans), rather than only the first.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Purity {
    Pure,
    NotPure { reasons: Vec<PurityReason> },
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct PurityReason {
    pub rule: PurityRule,
    /// Resolved by `resolve_reason_locations` once the per-chunk
    /// `line_range_for_span` is in scope (inside
    /// `analyze_item_facts`). The classifier itself only fills
    /// `span` — the wire-emitted reason has `source_location`
    /// populated and `span` skipped.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip)]
    pub span: Span,
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

impl Purity {
    pub fn is_pure(&self) -> bool {
        matches!(self, Purity::Pure)
    }

    /// Combine two purity verdicts. `Pure` is the identity;
    /// concatenating `NotPure` reasons preserves every offending
    /// sub-expression in source order.
    pub fn worst(self, other: Self) -> Self {
        match (self, other) {
            (Purity::Pure, x) | (x, Purity::Pure) => x,
            (Purity::NotPure { reasons: mut a }, Purity::NotPure { reasons: b }) => {
                a.extend(b);
                Purity::NotPure { reasons: a }
            }
        }
    }

    pub(crate) fn from_reason(rule: PurityRule, span: Span) -> Self {
        Self::from_reason_opt_detail(rule, span, None)
    }

    pub(crate) fn from_reason_with_detail(rule: PurityRule, span: Span, detail: String) -> Self {
        Self::from_reason_opt_detail(rule, span, Some(detail))
    }

    pub(crate) fn from_reason_opt_detail(
        rule: PurityRule,
        span: Span,
        detail: Option<String>,
    ) -> Self {
        Purity::NotPure {
            reasons: vec![PurityReason::new(rule, span, detail)],
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
        }
    }
}
