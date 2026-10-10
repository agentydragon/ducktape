use super::*;
use std::sync::Arc;

/// Two-state expression-level purity with structured reasons.
///
/// `Pure` means the expression is statically provably free of
/// observable side effects; `NotPure { reasons }` carries the
/// classifier rules that fired directly in the expression and its
/// sub-expressions (in source order). Known impure callees are linked
/// through shared cause nodes rather than flattened into this list. The classifier
/// previously distinguished `Impure` from `Unknown` for an internal
/// soundness argument, but downstream consumers (owner-graph
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
    Pure,
    NotPure { reasons: Vec<PurityReason> },
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
            cause_ref: None,
            cause: None,
            author_guidance: match rule {
                PurityRule::UnknownCall => Some(
                    "For a safe opaque call, use the member-level `purity: pure` annotation; for an imported fluent chain, declare `chunk_export_purity.<chunk>.fluent_exports`.".to_string(),
                ),
                PurityRule::UnknownNew => Some(
                    "For a safe opaque constructor, use the member-level `purity: pure_new` annotation.".to_string(),
                ),
                _ => None,
            },
        }
    }
}
