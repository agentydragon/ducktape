//! Semantic evidence produced by synchronous execution and consumed by graph proofs.
//!
//! These identities and vocabularies describe what the analysis established or
//! conservatively assumed. Reports serialize them, while runtime and graph
//! construction own their meaning here.

use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;

/// Why a source call site was proved not to invoke its callee.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SkippedCallReason {
    KnownThrow,
    KnownReturn,
    HandlerNotEntered,
}

#[derive(Debug, Clone, Ord, PartialOrd, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceLocation {
    pub source_path: String,
    pub start_line: usize,
    pub end_line: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_column: Option<usize>,
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

/// The first failed condition in the ordered local own-data field guard.
/// This is diagnostic evidence only and does not affect admission.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderedFieldReadRefusal {
    ReceiverNotSingleton,
    ReceiverNotObject,
    BorrowedReceiver,
    UnsupportedRecordShape,
    /// The allocation site is not marked fresh in this frame. This may be a
    /// reused site; it does not identify a distinct runtime allocation.
    AllocationNotFresh,
    UnsettledReceiver,
    MissingField,
    NonDataField,
    MissingHeapValue,
}

/// The failed element check for one dense-array slot in an iteration.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArrayIterationElementSettlementCheck {
    EmptyValues,
    UnavailableValue,
    UnsupportedValue,
    NonPlainObject,
    ObjectTainted,
    ObjectUnsettled,
    ObjectUnknownArrayContents,
    ObjectPrimitiveArrayMarker,
    TaintedFunction,
    BorrowedReceiverInstance,
}

/// The first diagnosed condition on an ordered call. Array-iteration variants
/// distinguish pre-argument GetValue selection from post-argument body
/// admission. This describes evidence only; it does not change target
/// classification or the conservative fallback.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderedCallAdmissionRefusal {
    UnknownTarget,
    MultipleTargets,
    NonLocalTarget,
    UnsupportedReceiver,
    UnsupportedNativeOperation,
    /// First failed check after an array iteration's callee and arguments
    /// have already been evaluated. These are evidence only; they preserve
    /// the existing conservative fallback and do not describe GetValue or
    /// callback-prefix failures.
    ArrayIterationCallKindMismatch,
    ArrayIterationCallSiteMismatch,
    ArrayIterationResultShapeMismatch,
    ArrayIterationCallbackArityMismatch,
    ArrayIterationCallbackTargetUnavailable,
    ArrayIterationGeneratorCallback,
    ArrayIterationOpaqueCallback,
    ArrayIterationAsyncCallback,
    ArrayIterationCallbackParameterUnsupported,
    ArrayIterationCallbackPlanUnavailable,
    ArrayIterationCallerUnavailable,
    ArrayIterationBodyIntrinsicsUnavailable,
    ArrayIterationReceiverDescriptorUnavailable,
    ArrayIterationDenseSnapshotUnavailable,
    ArrayIterationLengthUnrepresentable,
    ArrayIterationLengthMismatch,
    ArrayIterationExceedsInvocationLimit,
    /// Legacy refusal shape retained for previously saved execution reports.
    ArrayIterationUnsettledElements,
    /// The first dense slot whose already-captured abstract values fail the
    /// existing post-argument element-settlement predicate.
    ArrayIterationUnsettledElementsDetail {
        slot_index: usize,
        check: ArrayIterationElementSettlementCheck,
    },
    /// First failed check while a recognized array-iteration method's
    /// GetValue is being selected, before argument evaluation. These are
    /// evidence only and do not replace ordinary member-read or call
    /// fallbacks.
    ArrayIterationReceiverNotSingleton,
    ArrayIterationReceiverNotObject,
    ArrayIterationRowShapeUnsupported,
    ArrayIterationMethodSlotUnavailable,
    ArrayIterationOriginalMethodSelectionUnavailable,
    ArrayIterationResultAllocationUnavailable,
}

impl OrderedCallAdmissionRefusal {
    fn array_iteration_phase(&self) -> Option<&'static str> {
        match self {
            Self::ArrayIterationReceiverNotSingleton
            | Self::ArrayIterationReceiverNotObject
            | Self::ArrayIterationRowShapeUnsupported
            | Self::ArrayIterationMethodSlotUnavailable
            | Self::ArrayIterationOriginalMethodSelectionUnavailable
            | Self::ArrayIterationResultAllocationUnavailable => {
                Some("pre-argument GetValue selection")
            }
            Self::ArrayIterationResultShapeMismatch => Some("post-argument iteration transfer"),
            Self::ArrayIterationCallKindMismatch
            | Self::ArrayIterationCallSiteMismatch
            | Self::ArrayIterationCallbackArityMismatch
            | Self::ArrayIterationCallbackTargetUnavailable
            | Self::ArrayIterationGeneratorCallback
            | Self::ArrayIterationOpaqueCallback
            | Self::ArrayIterationAsyncCallback
            | Self::ArrayIterationCallbackParameterUnsupported
            | Self::ArrayIterationCallbackPlanUnavailable
            | Self::ArrayIterationCallerUnavailable
            | Self::ArrayIterationBodyIntrinsicsUnavailable
            | Self::ArrayIterationReceiverDescriptorUnavailable
            | Self::ArrayIterationDenseSnapshotUnavailable
            | Self::ArrayIterationLengthUnrepresentable
            | Self::ArrayIterationLengthMismatch
            | Self::ArrayIterationExceedsInvocationLimit
            | Self::ArrayIterationUnsettledElements
            | Self::ArrayIterationUnsettledElementsDetail { .. } => {
                Some("post-argument body admission")
            }
            _ => None,
        }
    }
}

impl std::fmt::Display for ArrayIterationElementSettlementCheck {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let check = serde_json::to_value(self)
            .expect("array iteration settlement checks serialize as strings");
        f.write_str(check.as_str().expect("unit variant"))
    }
}

impl std::fmt::Display for OrderedCallAdmissionRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if let Self::ArrayIterationUnsettledElementsDetail { slot_index, check } = self {
            return write!(
                f,
                "array_iteration_unsettled_elements (slot {slot_index}, check {check})"
            );
        }
        let refusal = serde_json::to_value(self)
            .expect("ordered call admission refusals serialize as strings");
        f.write_str(refusal.as_str().expect("unit variant"))
    }
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

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "code", rename_all = "snake_case")]
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

/// The first collapse rule that made a reach `global`.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GlobalReachCause {
    /// The code calls, directly or through the synchronous read closure, a
    /// callee the source does not name: an import, a global, a parameter, an
    /// unknown member, or an opaque body.
    UnknownCallee,
    /// An input reaches a value opaque code may hold or have changed: an
    /// unknown value, a tainted object, an escaped callable, or a global
    /// object alias.
    TaintedInput,
    /// A class the code runs is published, escaped or tainted, so its methods
    /// may run on a borrowed receiver.
    BorrowedReceiver,
    /// Dynamic scope (`eval`, `with`) makes the read closure incomplete.
    DynamicScope,
    /// An input is a foreign deferred record.
    ForeignRecord,
    /// The operation itself is unmodelled; values the interpreter does not
    /// know choose the code it may run.
    UnmodelledOperation,
    /// The callbacks unknown code may invoke: the prefix-wide escape pool.
    EscapedCallbackPool,
}

/// Why a local class construction kept the conservative path.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "check", rename_all = "snake_case")]
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
                let operation = serde_json::to_value(operation)
                    .expect("execution operations serialize as strings");
                write!(
                    f,
                    "`unsupported_operation` ({})",
                    operation.as_str().expect("unit variant")
                )?;
                if let Some(refusal) = ordered_field_read_refusal {
                    let refusal = serde_json::to_value(refusal)
                        .expect("ordered field refusals serialize as strings");
                    write!(
                        f,
                        "; ordered_field_read_refusal: {}",
                        refusal.as_str().expect("unit variant")
                    )?;
                }
                if let Some(refusal) = ordered_call_admission_refusal {
                    write!(f, "; ordered_call_admission_refusal: {refusal}")?;
                    if let Some(phase) = ordered_call_admission_refusal
                        .as_ref()
                        .and_then(OrderedCallAdmissionRefusal::array_iteration_phase)
                    {
                        write!(f, " ({phase})")?;
                    }
                }
                Ok(())
            }
            Self::UnsupportedConstruction { rejection } => {
                let rejection = serde_json::to_value(rejection)
                    .expect("construction rejections serialize as tagged objects");
                write!(
                    f,
                    "`unsupported_construction` ({})",
                    rejection["check"].as_str().expect("tagged by check")
                )
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
                let operation = serde_json::to_value(operation)
                    .expect("execution operations serialize as strings");
                write!(
                    f,
                    "whole-body promotion (`whole_body_promotion`, {})",
                    operation.as_str().expect("unit variant")
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

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionOperation {
    MembershipProtocol,
    InstanceOfProtocol,
    DestructuringProtocol,
    MemberRead,
    ObjectSpread,
    ArraySpread,
    ImplicitCoercion,
    PropertyWrite,
    ReturnedCallback,
    SuperclassConstruction,
    ObjectAssign,
    OpaqueExecution,
    GeneratorParameters,
    CallableInputs,
    ArrayIteration,
}

#[cfg(test)]
mod tests {
    use super::{
        ArrayIterationElementSettlementCheck, ExecutionOperation, ExecutionPrecisionLossReason,
        OrderedCallAdmissionRefusal,
    };
    use serde_json::json;

    #[test]
    fn array_iteration_unsettled_refusal_keeps_legacy_and_detailed_wire_shapes() {
        let legacy = OrderedCallAdmissionRefusal::ArrayIterationUnsettledElements;
        let legacy_json = json!("array_iteration_unsettled_elements");
        assert_eq!(serde_json::to_value(legacy).unwrap(), legacy_json);
        assert_eq!(
            serde_json::from_value::<OrderedCallAdmissionRefusal>(legacy_json).unwrap(),
            legacy
        );
        assert_eq!(
            legacy.array_iteration_phase(),
            Some("post-argument body admission")
        );

        let detailed = OrderedCallAdmissionRefusal::ArrayIterationUnsettledElementsDetail {
            slot_index: 1,
            check: ArrayIterationElementSettlementCheck::ObjectUnsettled,
        };
        let detailed_json = json!({
            "array_iteration_unsettled_elements_detail": {
                "slot_index": 1,
                "check": "object_unsettled"
            }
        });
        assert_eq!(serde_json::to_value(detailed).unwrap(), detailed_json);
        assert_eq!(
            serde_json::from_value::<OrderedCallAdmissionRefusal>(detailed_json).unwrap(),
            detailed
        );
        assert_eq!(
            detailed.to_string(),
            "array_iteration_unsettled_elements (slot 1, check object_unsettled)"
        );
        assert_eq!(
            detailed.array_iteration_phase(),
            Some("post-argument body admission")
        );

        let reason = ExecutionPrecisionLossReason::UnsupportedOperation {
            operation: ExecutionOperation::ArrayIteration,
            ordered_field_read_refusal: None,
            ordered_call_admission_refusal: Some(detailed),
        };
        assert!(reason.to_string().contains(
            "ordered_call_admission_refusal: array_iteration_unsettled_elements (slot 1, check object_unsettled) (post-argument body admission)"
        ));
    }
}
