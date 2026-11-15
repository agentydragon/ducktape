//! Semantic evidence produced by synchronous execution and consumed by graph proofs.
//!
//! These identities and vocabularies describe what the analysis established or
//! conservatively assumed. Reports serialize them, while runtime and graph
//! construction own their meaning here.

use crate::saved_proto::{ProtoConvert, proto, required};
use anyhow::{Result, ensure};
use js_ast::SourceLineIndex;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::{BytePos, Span};

pub use crate::saved_proto::proto::{
    ExecutionOperation, GlobalReachCause, OrderedCallAdmissionRefusal, OrderedFieldReadRefusal,
};

#[derive(Debug, Clone, Ord, PartialOrd, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceLocation {
    pub source_path: String,
    pub start_line: usize,
    pub end_line: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_column: Option<usize>,
}

/// Resolves SWC positions of one analyzed source file to [`SourceLocation`]s.
#[derive(Debug, Clone, Copy)]
pub struct SourceLocator<'a> {
    pub source_path: &'a str,
    pub lines: &'a SourceLineIndex,
}

impl SourceLocator<'_> {
    /// The span's start line and column and its end line; `None` for a dummy span.
    pub fn span(&self, span: Span) -> Option<SourceLocation> {
        let (start_line, end_line, start_column) =
            self.lines.line_range_and_start_column_for_span(span)?;
        Some(SourceLocation {
            source_path: self.source_path.to_owned(),
            start_line,
            end_line,
            start_column: Some(start_column),
        })
    }

    /// A one-line location at `pos`.
    pub fn pos(&self, pos: BytePos) -> Option<SourceLocation> {
        let (line, column) = self.lines.line_and_column_for_pos(pos)?;
        Some(SourceLocation {
            source_path: self.source_path.to_owned(),
            start_line: line,
            end_line: line,
            start_column: Some(column),
        })
    }
}

/// Exact property lookup that made accessor execution possible. The owner is
/// the source unit containing the lookup, and the span uses SWC byte positions
/// in the analyzed source program.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct PossibleGetterLookupSite {
    /// `SourceUnitOrdinal` in the analyzed chunk, not a graph node ID.
    pub lexical_owner: SourceUnitOrdinal,
    /// SWC byte positions in the analyzed source program.
    pub span_start: u32,
    pub span_end: u32,
}

/// Source of the closure promoted by a possible getter read.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PossibleGetterPromotion {
    /// The lazy body of one source owner was promoted.
    LazyOwner { source_owner: SourceUnitOrdinal },
    /// Raw callbacks reachable through the receiver were promoted without a
    /// single source owner identifying that callback set.
    RawCallbacks,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ExecutionPrecisionLoss {
    pub reason: ExecutionPrecisionLossReason,
    pub reach: HavocReach,
    /// Lexical source unit containing the operation or truncated callable.
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

impl ExecutionPrecisionLoss {
    pub fn is_unknown_call_target_at(
        &self,
        statement_ordinal: SourceUnitOrdinal,
        span: swc_common::Span,
    ) -> bool {
        self.reason == ExecutionPrecisionLossReason::UnknownCallTarget
            && self.statement_ordinal == statement_ordinal
            && self.span == span
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize, strum::AsRefStr)]
#[serde(tag = "code", rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum ExecutionPrecisionLossReason {
    UnknownCallTarget,
    /// A complete synchronous call proof supplied effects without the value
    /// transfer needed to continue an ordered body.
    MissingCallResult,
    UnsupportedOperation {
        operation: ExecutionOperation,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        ordered_field_read_refusal: Option<OrderedFieldReadRefusal>,
        #[serde(skip_serializing_if = "Option::is_none")]
        ordered_call_admission_refusal: Option<OrderedCallAdmissionRefusal>,
    },
    /// `new` of a local class whose construction proof failed. The rejection
    /// names the first failed check, not every check that would fail.
    UnsupportedConstruction {
        rejection: ConstructionRejection,
    },
    RecursiveInvocation,
    InvocationDepthLimit {
        limit: usize,
    },
    InvocationExpansionLimit {
        limit: usize,
    },
    /// `pool_size` counts the callbacks the opaque call could invoke.
    CallbackPoolLimit {
        limit: usize,
        pool_size: usize,
    },
}

/// What the code a precision loss may run can reach, classified per
/// SPEC.md § Synchronous execution diagnostics. A report only: the conservative
/// path promotes the same effects for both.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum HavocReach {
    /// The unmodelled part is known source code: its callables and the
    /// values it receives reach no code the interpreter cannot name.
    Bounded,
    Global {
        cause: GlobalReachCause,
    },
}

/// Why a local class construction kept the conservative path.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize, strum::AsRefStr)]
#[serde(tag = "check", rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum ConstructionRejection {
    SpreadArguments,
    /// The class has no source-checked constructor summary.
    UncheckedConstructor,
    /// An implicit constructor with checked field initializers received arguments.
    ArgumentsToDefaultFields,
    /// The class binding is used other than as a `new` callee.
    ClassUsedOutsideConstruction,
    /// Export reentry can reach the class binding.
    ExportedClass,
    /// A write through an instance of this class was observed.
    ClassMutated,
    /// The class's members escaped to code that can modify its prototype.
    ClassMembersEscaped,
    /// An assigned instance field may name an `Object.prototype` accessor or
    /// non-writable property; the census entries are in
    /// `native_prototype_property_checks`.
    FieldMayExistOnObjectPrototype {
        property: String,
    },
    /// The constructor reads a binding not declared in the chunk.
    UndeclaredConstructorRead,
    /// An argument whose properties the constructor reads is not a fresh,
    /// untainted plain-data object.
    UncheckedPropertySource {
        parameter: usize,
    },
}

impl std::fmt::Display for ExecutionPrecisionLossReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownCallTarget => f.write_str("`unknown_call_target`"),
            Self::MissingCallResult => f.write_str(
                "`missing_call_result` (call effects covered; return-value transfer unavailable)",
            ),
            Self::UnsupportedOperation {
                operation,
                ordered_field_read_refusal,
                ordered_call_admission_refusal,
            } => {
                write!(f, "`unsupported_operation` ({operation})")?;
                if let Some(refusal) = ordered_field_read_refusal {
                    write!(f, "; ordered_field_read_refusal: {refusal}")?;
                }
                if let Some(refusal) = ordered_call_admission_refusal {
                    write!(f, "; ordered_call_admission_refusal: {refusal}")?;
                }
                Ok(())
            }
            Self::UnsupportedConstruction { rejection } => {
                write!(f, "`unsupported_construction` ({})", rejection.as_ref())
            }
            Self::RecursiveInvocation => f.write_str("`recursive_invocation`"),
            Self::InvocationDepthLimit { limit } => {
                write!(f, "`invocation_depth_limit` (limit {limit})")
            }
            Self::InvocationExpansionLimit { limit } => {
                write!(f, "`invocation_expansion_limit` (limit {limit})")
            }
            Self::CallbackPoolLimit { limit, pool_size } => {
                write!(f, "`callback_pool_limit` (pool {pool_size}, limit {limit})")
            }
        }
    }
}

/// The conservative path that assumed a binding effect instead of deriving
/// it (SPEC.md § Commuting statements). Each path assumes some code may run
/// (or reach the binding) at the statement; none follows an invocation the
/// statement definitely makes.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "path", rename_all = "snake_case")]
pub enum AssumedEffectReason {
    /// The statement's bounded fallback summary: having stopped expanding
    /// callbacks, or handed a returned callback to opaque code, the
    /// interpreter assumes each may run. `loss` opened the summary.
    FallbackSummary { loss: ExecutionPrecisionLossReason },
    /// A callback opaque code may invoke (escaped earlier, exported, or
    /// reachable from the opaque operation's inputs), executed as if it ran.
    OpaqueCallbackInvocation,
    /// The synchronous read/rebind closure of a callback, class or foreign
    /// record exposed to opaque code, promoted without executing it.
    EscapedCallbackClosure,
    /// The lazy body of an accessor owner whose setter a property write or
    /// `Object.assign` may trigger on a receiver of unknown shape.
    PossibleSetter,
    /// A read on an object whose descriptors or prototype may have changed
    /// invokes an accessor installed by code the interpreter did not follow.
    PossibleGetter {
        lookup: PossibleGetterLookupSite,
        promotion: PossibleGetterPromotion,
    },
    /// The whole lazy body of a callable the interpreter executed but could
    /// not model (`callable_inputs`, `array_iteration`), beyond what that
    /// execution reached.
    WholeBodyPromotion { operation: ExecutionOperation },
    /// The omitted callback effects restored when a private-constructor
    /// result proof's execution failed.
    PrivateConstructorRestore,
    /// Graph build: the read closure of a callee at-init promotion could not
    /// resolve, assumed to run at the calling statement.
    UnresolvedCallClosure,
}

impl std::fmt::Display for AssumedEffectReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::FallbackSummary { loss } => write!(f, "the {loss} fallback summary"),
            Self::OpaqueCallbackInvocation => {
                f.write_str("a callback opaque code may invoke (`opaque_callback_invocation`)")
            }
            Self::EscapedCallbackClosure => {
                f.write_str("an escaped callback's closure (`escaped_callback_closure`)")
            }
            Self::PossibleSetter => f.write_str("a possible setter (`possible_setter`)"),
            Self::PossibleGetter { lookup, promotion } => {
                write!(
                    f,
                    "a possible getter (`possible_getter`, lookup unit:{} span {}..{}",
                    lookup.lexical_owner, lookup.span_start, lookup.span_end
                )?;
                match promotion {
                    PossibleGetterPromotion::LazyOwner { source_owner } => {
                        write!(f, ", promoted source owner unit:{source_owner})")
                    }
                    PossibleGetterPromotion::RawCallbacks => {
                        f.write_str(", raw callback fallback without one owner)")
                    }
                }
            }
            Self::WholeBodyPromotion { operation } => {
                write!(
                    f,
                    "whole-body promotion (`whole_body_promotion`, {operation})"
                )
            }
            Self::PrivateConstructorRestore => f.write_str(
                "the failed private-constructor proof's restore (`private_constructor_restore`)",
            ),
            Self::UnresolvedCallClosure => {
                f.write_str("an unresolved at-init call's closure (`unresolved_call_closure`)")
            }
        }
    }
}

impl AssumedEffectReason {
    /// Whether two reasons belong to the same report category. Detailed
    /// possible-getter witnesses intentionally share one category so summary
    /// evidence can retain one deterministic witness without growing with
    /// every getter lookup and promoted owner.
    pub(crate) fn same_report_category(&self, other: &Self) -> bool {
        matches!(
            (self, other),
            (Self::PossibleGetter { .. }, Self::PossibleGetter { .. })
        ) || self == other
    }

    /// Whether this detailed witness sorts before `other` within the same
    /// report category. Possible-getter witnesses use source coordinates so
    /// merge order cannot select a different representative.
    pub(crate) fn is_canonical_predecessor(&self, other: &Self) -> bool {
        match (self, other) {
            (
                Self::PossibleGetter {
                    lookup: left_lookup,
                    promotion: left_promotion,
                },
                Self::PossibleGetter {
                    lookup: right_lookup,
                    promotion: right_promotion,
                },
            ) => (left_lookup, left_promotion) < (right_lookup, right_promotion),
            _ => false,
        }
    }
}

impl ProtoConvert<proto::SourceLocation> for SourceLocation {
    fn to_proto(&self) -> Result<proto::SourceLocation> {
        Ok(proto::SourceLocation {
            source_path: Some(self.source_path.clone()),
            start_line: Some(self.start_line.to_proto()?),
            end_line: Some(self.end_line.to_proto()?),
            start_column: self
                .start_column
                .map(|column| column.to_proto())
                .transpose()?,
        })
    }
    fn from_proto(value: proto::SourceLocation) -> Result<Self> {
        Ok(Self {
            source_path: required(value.source_path, "SourceLocation.source_path")?,
            start_line: ProtoConvert::from_proto(required(
                value.start_line,
                "SourceLocation.start_line",
            )?)?,
            end_line: ProtoConvert::from_proto(required(
                value.end_line,
                "SourceLocation.end_line",
            )?)?,
            start_column: value.start_column.map(usize::from_proto).transpose()?,
        })
    }
}
impl ProtoConvert<proto::PossibleGetterLookupSite> for PossibleGetterLookupSite {
    fn to_proto(&self) -> Result<proto::PossibleGetterLookupSite> {
        ensure!(
            self.span_start <= self.span_end,
            "inverted possible-getter lookup span"
        );
        Ok(proto::PossibleGetterLookupSite {
            lexical_owner: Some(self.lexical_owner.to_proto()?),
            span_start: Some(self.span_start),
            span_end: Some(self.span_end),
        })
    }
    fn from_proto(value: proto::PossibleGetterLookupSite) -> Result<Self> {
        let lookup = Self {
            lexical_owner: ProtoConvert::from_proto(required(
                value.lexical_owner,
                "PossibleGetterLookupSite.lexical_owner",
            )?)?,
            span_start: required(value.span_start, "PossibleGetterLookupSite.span_start")?,
            span_end: required(value.span_end, "PossibleGetterLookupSite.span_end")?,
        };
        ensure!(
            lookup.span_start <= lookup.span_end,
            "inverted possible-getter lookup span"
        );
        Ok(lookup)
    }
}
impl ProtoConvert<proto::ExecutionPrecisionLoss> for ExecutionPrecisionLoss {
    fn to_proto(&self) -> Result<proto::ExecutionPrecisionLoss> {
        Ok(proto::ExecutionPrecisionLoss {
            reason: Some(self.reason.to_proto()?),
            reach: Some(self.reach.to_proto()?),
            statement_ordinal: Some(self.statement_ordinal.to_proto()?),
            source_location: self
                .source_location
                .as_ref()
                .map(SourceLocation::to_proto)
                .transpose()?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::ExecutionPrecisionLoss) -> Result<Self> {
        Ok(Self {
            reason: ProtoConvert::from_proto(required(
                value.reason,
                "ExecutionPrecisionLoss.reason",
            )?)?,
            reach: ProtoConvert::from_proto(required(
                value.reach,
                "ExecutionPrecisionLoss.reach",
            )?)?,
            statement_ordinal: ProtoConvert::from_proto(required(
                value.statement_ordinal,
                "ExecutionPrecisionLoss.statement_ordinal",
            )?)?,
            source_location: value
                .source_location
                .map(SourceLocation::from_proto)
                .transpose()?,
            span: ProtoConvert::from_proto(required(value.span, "ExecutionPrecisionLoss.span")?)?,
        })
    }
}
impl ProtoConvert<proto::PossibleGetterPromotion> for PossibleGetterPromotion {
    fn to_proto(&self) -> Result<proto::PossibleGetterPromotion> {
        use proto::possible_getter_promotion::Value;
        Ok(proto::PossibleGetterPromotion {
            value: Some(match self {
                Self::LazyOwner { source_owner } => Value::LazyOwner(source_owner.to_proto()?),
                Self::RawCallbacks => Value::RawCallbacks(Default::default()),
            }),
        })
    }
    fn from_proto(value: proto::PossibleGetterPromotion) -> Result<Self> {
        use proto::possible_getter_promotion::Value;
        Ok(
            match required(value.value, "PossibleGetterPromotion.value")? {
                Value::LazyOwner(source_owner) => Self::LazyOwner {
                    source_owner: ProtoConvert::from_proto(source_owner)?,
                },
                Value::RawCallbacks(_) => Self::RawCallbacks,
            },
        )
    }
}
impl ProtoConvert<proto::HavocReach> for HavocReach {
    fn to_proto(&self) -> Result<proto::HavocReach> {
        use proto::havoc_reach::Value;
        Ok(proto::HavocReach {
            value: Some(match self {
                Self::Bounded => Value::Bounded(Default::default()),
                Self::Global { cause } => Value::Global(*cause as i32),
            }),
        })
    }
    fn from_proto(value: proto::HavocReach) -> Result<Self> {
        use proto::havoc_reach::Value;
        Ok(match required(value.value, "HavocReach.value")? {
            Value::Bounded(_) => Self::Bounded,
            Value::Global(cause) => {
                let cause = GlobalReachCause::try_from(cause)?;
                Self::Global { cause }
            }
        })
    }
}
impl ProtoConvert<proto::ExecutionPrecisionLossReason> for ExecutionPrecisionLossReason {
    fn to_proto(&self) -> Result<proto::ExecutionPrecisionLossReason> {
        use proto::execution_precision_loss_reason::{
            CallbackPoolLimit, UnsupportedOperation, Value,
        };
        Ok(proto::ExecutionPrecisionLossReason {
            value: Some(match self {
                Self::UnknownCallTarget => Value::UnknownCallTarget(Default::default()),
                Self::MissingCallResult => Value::MissingCallResult(Default::default()),
                Self::UnsupportedOperation {
                    operation,
                    ordered_field_read_refusal,
                    ordered_call_admission_refusal,
                } => Value::UnsupportedOperation(UnsupportedOperation {
                    operation: Some(*operation as i32),
                    ordered_field_read_refusal: ordered_field_read_refusal
                        .as_ref()
                        .map(|value| *value as i32),
                    ordered_call_admission_refusal: ordered_call_admission_refusal
                        .as_ref()
                        .map(|value| *value as i32),
                }),
                Self::UnsupportedConstruction { rejection } => {
                    Value::UnsupportedConstruction(rejection.to_proto()?)
                }
                Self::RecursiveInvocation => Value::RecursiveInvocation(Default::default()),
                Self::InvocationDepthLimit { limit } => {
                    Value::InvocationDepthLimit(limit.to_proto()?)
                }
                Self::InvocationExpansionLimit { limit } => {
                    Value::InvocationExpansionLimit(limit.to_proto()?)
                }
                Self::CallbackPoolLimit { limit, pool_size } => {
                    Value::CallbackPoolLimit(CallbackPoolLimit {
                        limit: Some(limit.to_proto()?),
                        pool_size: Some(pool_size.to_proto()?),
                    })
                }
            }),
        })
    }
    fn from_proto(value: proto::ExecutionPrecisionLossReason) -> Result<Self> {
        use proto::execution_precision_loss_reason::Value;
        Ok(
            match required(value.value, "ExecutionPrecisionLossReason.value")? {
                Value::UnknownCallTarget(_) => Self::UnknownCallTarget,
                Value::MissingCallResult(_) => Self::MissingCallResult,
                Value::UnsupportedOperation(value) => {
                    let operation = ExecutionOperation::try_from(required(
                        value.operation,
                        "ExecutionPrecisionLossReason.unsupported_operation.operation",
                    )?)?;
                    let ordered_field_read_refusal = value
                        .ordered_field_read_refusal
                        .map(OrderedFieldReadRefusal::try_from)
                        .transpose()?;
                    let ordered_call_admission_refusal = value
                        .ordered_call_admission_refusal
                        .map(OrderedCallAdmissionRefusal::try_from)
                        .transpose()?;
                    Self::UnsupportedOperation {
                        operation,
                        ordered_field_read_refusal,
                        ordered_call_admission_refusal,
                    }
                }
                Value::UnsupportedConstruction(value) => Self::UnsupportedConstruction {
                    rejection: ConstructionRejection::from_proto(value)?,
                },
                Value::RecursiveInvocation(_) => Self::RecursiveInvocation,
                Value::InvocationDepthLimit(value) => Self::InvocationDepthLimit {
                    limit: usize::from_proto(value)?,
                },
                Value::InvocationExpansionLimit(value) => Self::InvocationExpansionLimit {
                    limit: usize::from_proto(value)?,
                },
                Value::CallbackPoolLimit(value) => Self::CallbackPoolLimit {
                    limit: usize::from_proto(required(
                        value.limit,
                        "ExecutionPrecisionLossReason.callback_pool_limit.limit",
                    )?)?,
                    pool_size: usize::from_proto(required(
                        value.pool_size,
                        "ExecutionPrecisionLossReason.callback_pool_limit.pool_size",
                    )?)?,
                },
            },
        )
    }
}
impl ProtoConvert<proto::ConstructionRejection> for ConstructionRejection {
    fn to_proto(&self) -> Result<proto::ConstructionRejection> {
        use proto::construction_rejection::Value;
        Ok(proto::ConstructionRejection {
            value: Some(match self {
                Self::SpreadArguments => Value::SpreadArguments(Default::default()),
                Self::UncheckedConstructor => Value::UncheckedConstructor(Default::default()),
                Self::ArgumentsToDefaultFields => {
                    Value::ArgumentsToDefaultFields(Default::default())
                }
                Self::ClassUsedOutsideConstruction => {
                    Value::ClassUsedOutsideConstruction(Default::default())
                }
                Self::ExportedClass => Value::ExportedClass(Default::default()),
                Self::ClassMutated => Value::ClassMutated(Default::default()),
                Self::ClassMembersEscaped => Value::ClassMembersEscaped(Default::default()),
                Self::FieldMayExistOnObjectPrototype { property } => {
                    Value::FieldMayExistOnObjectPrototype(property.clone())
                }
                Self::UndeclaredConstructorRead => {
                    Value::UndeclaredConstructorRead(Default::default())
                }
                Self::UncheckedPropertySource { parameter } => {
                    Value::UncheckedPropertySource(parameter.to_proto()?)
                }
            }),
        })
    }
    fn from_proto(value: proto::ConstructionRejection) -> Result<Self> {
        use proto::construction_rejection::Value;
        Ok(
            match required(value.value, "ConstructionRejection.value")? {
                Value::SpreadArguments(_) => Self::SpreadArguments,
                Value::UncheckedConstructor(_) => Self::UncheckedConstructor,
                Value::ArgumentsToDefaultFields(_) => Self::ArgumentsToDefaultFields,
                Value::ClassUsedOutsideConstruction(_) => Self::ClassUsedOutsideConstruction,
                Value::ExportedClass(_) => Self::ExportedClass,
                Value::ClassMutated(_) => Self::ClassMutated,
                Value::ClassMembersEscaped(_) => Self::ClassMembersEscaped,
                Value::FieldMayExistOnObjectPrototype(property) => {
                    Self::FieldMayExistOnObjectPrototype { property }
                }
                Value::UndeclaredConstructorRead(_) => Self::UndeclaredConstructorRead,
                Value::UncheckedPropertySource(parameter) => Self::UncheckedPropertySource {
                    parameter: ProtoConvert::from_proto(parameter)?,
                },
            },
        )
    }
}
impl ProtoConvert<proto::AssumedEffectReason> for AssumedEffectReason {
    fn to_proto(&self) -> Result<proto::AssumedEffectReason> {
        use proto::assumed_effect_reason::{PossibleGetter, Value};
        Ok(proto::AssumedEffectReason {
            value: Some(match self {
                Self::FallbackSummary { loss } => Value::FallbackSummary(loss.to_proto()?),
                Self::OpaqueCallbackInvocation => {
                    Value::OpaqueCallbackInvocation(Default::default())
                }
                Self::EscapedCallbackClosure => Value::EscapedCallbackClosure(Default::default()),
                Self::PossibleSetter => Value::PossibleSetter(Default::default()),
                Self::PossibleGetter { lookup, promotion } => {
                    Value::PossibleGetter(PossibleGetter {
                        lookup: Some(lookup.to_proto()?),
                        promotion: Some(promotion.to_proto()?),
                    })
                }
                Self::WholeBodyPromotion { operation } => {
                    Value::WholeBodyPromotion(*operation as i32)
                }
                Self::PrivateConstructorRestore => {
                    Value::PrivateConstructorRestore(Default::default())
                }
                Self::UnresolvedCallClosure => Value::UnresolvedCallClosure(Default::default()),
            }),
        })
    }
    fn from_proto(value: proto::AssumedEffectReason) -> Result<Self> {
        use proto::assumed_effect_reason::Value;
        Ok(match required(value.value, "AssumedEffectReason.value")? {
            Value::FallbackSummary(loss) => Self::FallbackSummary {
                loss: ExecutionPrecisionLossReason::from_proto(loss)?,
            },
            Value::OpaqueCallbackInvocation(_) => Self::OpaqueCallbackInvocation,
            Value::EscapedCallbackClosure(_) => Self::EscapedCallbackClosure,
            Value::PossibleSetter(_) => Self::PossibleSetter,
            Value::PossibleGetter(value) => Self::PossibleGetter {
                lookup: PossibleGetterLookupSite::from_proto(required(
                    value.lookup,
                    "AssumedEffectReason.possible_getter.lookup",
                )?)?,
                promotion: PossibleGetterPromotion::from_proto(required(
                    value.promotion,
                    "AssumedEffectReason.possible_getter.promotion",
                )?)?,
            },
            Value::WholeBodyPromotion(operation) => {
                let operation = ExecutionOperation::try_from(operation)?;
                Self::WholeBodyPromotion { operation }
            }
            Value::PrivateConstructorRestore(_) => Self::PrivateConstructorRestore,
            Value::UnresolvedCallClosure(_) => Self::UnresolvedCallClosure,
        })
    }
}

#[cfg(test)]
mod protobuf_tests {
    use super::*;

    #[test]
    fn construction_rejection_preserves_zero_and_empty_payloads() {
        for reason in [
            ConstructionRejection::UncheckedPropertySource { parameter: 0 },
            ConstructionRejection::FieldMayExistOnObjectPrototype {
                property: String::new(),
            },
            ConstructionRejection::ExportedClass,
        ] {
            assert_eq!(
                ConstructionRejection::from_proto(reason.to_proto().unwrap()).unwrap(),
                reason
            );
        }
    }

    #[test]
    fn saved_precision_loss_keeps_nested_refusals_optional_locations_and_hidden_spans() {
        let original = ExecutionPrecisionLoss {
            reason: ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::MemberRead,
                ordered_field_read_refusal: Some(OrderedFieldReadRefusal::AllocationNotFresh),
                ordered_call_admission_refusal: None,
            },
            reach: HavocReach::Global {
                cause: GlobalReachCause::BorrowedReceiver,
            },
            statement_ordinal: SourceUnitOrdinal(7),
            source_location: Some(SourceLocation {
                source_path: "source.js".into(),
                start_line: 2,
                end_line: 4,
                start_column: Some(0),
            }),
            span: Span::new(BytePos(17), BytePos(29)),
        };
        let bytes = analysis_codec::encode_protobuf(&original.to_proto().unwrap()).unwrap();
        let restored =
            ExecutionPrecisionLoss::from_proto(analysis_codec::decode_protobuf(&bytes).unwrap())
                .unwrap();
        assert_eq!(restored, original);
        let mut malformed = original.to_proto().unwrap();
        malformed.source_location = None;
        assert_eq!(
            ExecutionPrecisionLoss::from_proto(malformed)
                .unwrap()
                .source_location,
            None
        );
        for field in 0..4 {
            let mut malformed = original.to_proto().unwrap();
            match field {
                0 => malformed.reason = None,
                1 => malformed.reach = None,
                2 => malformed.statement_ordinal = None,
                3 => malformed.span = None,
                _ => unreachable!(),
            }
            assert!(ExecutionPrecisionLoss::from_proto(malformed).is_err());
        }
        let mut malformed = original.to_proto().unwrap();
        malformed.span.as_mut().unwrap().lo = Some(30);
        assert!(ExecutionPrecisionLoss::from_proto(malformed).is_err());
    }

    #[test]
    fn saved_evidence_rejects_false_markers_and_missing_nested_fields() {
        assert!(HavocReach::from_proto(proto::HavocReach { value: None }).is_err());
        assert!(
            PossibleGetterPromotion::from_proto(proto::PossibleGetterPromotion { value: None })
                .is_err()
        );
        assert!(
            ConstructionRejection::from_proto(proto::ConstructionRejection { value: None })
                .is_err()
        );
        assert!(
            AssumedEffectReason::from_proto(proto::AssumedEffectReason { value: None }).is_err()
        );
        assert!(
            ExecutionPrecisionLossReason::from_proto(proto::ExecutionPrecisionLossReason {
                value: None
            })
            .is_err()
        );
        let reach = HavocReach::Global {
            cause: GlobalReachCause::UnknownCallee,
        };
        assert_eq!(
            HavocReach::from_proto(reach.to_proto().unwrap()).unwrap(),
            reach
        );
        let promotion = AssumedEffectReason::WholeBodyPromotion {
            operation: ExecutionOperation::MembershipProtocol,
        };
        assert_eq!(
            AssumedEffectReason::from_proto(promotion.to_proto().unwrap()).unwrap(),
            promotion
        );
        let valid = ExecutionPrecisionLossReason::UnsupportedOperation {
            operation: ExecutionOperation::ObjectSpread,
            ordered_field_read_refusal: None,
            ordered_call_admission_refusal: Some(OrderedCallAdmissionRefusal::UnsupportedReceiver),
        };
        for operation in [-1, i32::MAX] {
            let mut malformed = valid.to_proto().unwrap();
            let Some(proto::execution_precision_loss_reason::Value::UnsupportedOperation(value)) =
                &mut malformed.value
            else {
                unreachable!()
            };
            value.operation = Some(operation);
            assert!(ExecutionPrecisionLossReason::from_proto(malformed).is_err());
        }
        for invalid in [-1, i32::MAX] {
            for field in 0..2 {
                let mut malformed = valid.to_proto().unwrap();
                let Some(proto::execution_precision_loss_reason::Value::UnsupportedOperation(
                    value,
                )) = &mut malformed.value
                else {
                    unreachable!()
                };
                if field == 0 {
                    value.ordered_field_read_refusal = Some(invalid);
                } else {
                    value.ordered_call_admission_refusal = Some(invalid);
                }
                assert!(ExecutionPrecisionLossReason::from_proto(malformed).is_err());
            }
        }
    }

    #[test]
    fn precision_loss_reason_variants_preserve_zero_limits_and_optional_refusals() {
        let mut reasons = vec![
            ExecutionPrecisionLossReason::UnknownCallTarget,
            ExecutionPrecisionLossReason::MissingCallResult,
            ExecutionPrecisionLossReason::RecursiveInvocation,
            ExecutionPrecisionLossReason::UnsupportedConstruction {
                rejection: ConstructionRejection::ExportedClass,
            },
        ];
        for limit in [0, 17, usize::MAX] {
            reasons.push(ExecutionPrecisionLossReason::InvocationDepthLimit { limit });
            reasons.push(ExecutionPrecisionLossReason::InvocationExpansionLimit { limit });
            reasons.push(ExecutionPrecisionLossReason::CallbackPoolLimit {
                limit,
                pool_size: 0,
            });
        }
        for operation in [
            ExecutionOperation::MembershipProtocol,
            ExecutionOperation::MemberRead,
        ] {
            for ordered_field_read_refusal in [
                None,
                Some(OrderedFieldReadRefusal::ReceiverNotSingleton),
                Some(OrderedFieldReadRefusal::MissingField),
            ] {
                for ordered_call_admission_refusal in
                    [None, Some(OrderedCallAdmissionRefusal::UnknownTarget)]
                {
                    reasons.push(ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation,
                        ordered_field_read_refusal,
                        ordered_call_admission_refusal,
                    });
                }
            }
        }
        for reason in reasons {
            let bytes = analysis_codec::encode_protobuf(&reason.to_proto().unwrap()).unwrap();
            let restored = ExecutionPrecisionLossReason::from_proto(
                analysis_codec::decode_protobuf(&bytes).unwrap(),
            )
            .unwrap();
            assert_eq!(restored, reason);
        }
    }

    #[test]
    fn precision_loss_reason_requires_nested_evidence_fields() {
        use proto::execution_precision_loss_reason::{
            CallbackPoolLimit, UnsupportedOperation, Value,
        };
        for value in [
            Value::UnsupportedOperation(UnsupportedOperation::default()),
            Value::UnsupportedConstruction(proto::ConstructionRejection::default()),
            Value::CallbackPoolLimit(CallbackPoolLimit {
                limit: None,
                pool_size: Some(0),
            }),
            Value::CallbackPoolLimit(CallbackPoolLimit {
                limit: Some(0),
                pool_size: None,
            }),
        ] {
            assert!(
                ExecutionPrecisionLossReason::from_proto(proto::ExecutionPrecisionLossReason {
                    value: Some(value)
                })
                .is_err()
            );
        }
    }

    #[test]
    fn source_location_preserves_absent_and_zero_columns() {
        for start_column in [None, Some(0), Some(27)] {
            let location = SourceLocation {
                source_path: "source.js".into(),
                start_line: 1,
                end_line: 2,
                start_column,
            };
            let bytes = analysis_codec::encode_protobuf(&location.to_proto().unwrap()).unwrap();
            assert_eq!(
                SourceLocation::from_proto(analysis_codec::decode_protobuf(&bytes).unwrap(),)
                    .unwrap(),
                location
            );
            for field in 0..3 {
                let mut malformed = location.to_proto().unwrap();
                match field {
                    0 => malformed.source_path = None,
                    1 => malformed.start_line = None,
                    2 => malformed.end_line = None,
                    _ => unreachable!(),
                }
                assert!(SourceLocation::from_proto(malformed).is_err());
            }
        }
    }

    #[test]
    fn possible_getter_lookup_preserves_owner_and_checks_span() {
        for (owner, start, end) in [(0, 0, 0), (7, 17, 29)] {
            let lookup = PossibleGetterLookupSite {
                lexical_owner: SourceUnitOrdinal(owner),
                span_start: start,
                span_end: end,
            };
            let bytes = analysis_codec::encode_protobuf(&lookup.to_proto().unwrap()).unwrap();
            assert_eq!(
                PossibleGetterLookupSite::from_proto(
                    analysis_codec::decode_protobuf(&bytes).unwrap(),
                )
                .unwrap(),
                lookup
            );
            for field in 0..3 {
                let mut malformed = lookup.to_proto().unwrap();
                match field {
                    0 => malformed.lexical_owner = None,
                    1 => malformed.span_start = None,
                    2 => malformed.span_end = None,
                    _ => unreachable!(),
                }
                assert!(PossibleGetterLookupSite::from_proto(malformed).is_err());
            }
            let mut malformed = lookup.to_proto().unwrap();
            malformed.span_start = Some(end + 1);
            assert!(PossibleGetterLookupSite::from_proto(malformed).is_err());
        }
        assert!(
            PossibleGetterLookupSite {
                lexical_owner: SourceUnitOrdinal(0),
                span_start: 1,
                span_end: 0,
            }
            .to_proto()
            .is_err()
        );
    }
}
