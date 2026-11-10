//! Semantic evidence produced by synchronous execution and consumed by graph proofs.
//!
//! These identities and vocabularies describe what the analysis established or
//! conservatively assumed. Reports serialize them, while runtime and graph
//! construction own their meaning here.

use js_ast::SourceLineIndex;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::{BytePos, Span};

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

/// The first ordered call admission condition that rejected the evaluated
/// target and receiver. This describes the refusal only; it does not change
/// target classification or the conservative fallback.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderedCallAdmissionRefusal {
    UnknownTarget,
    MultipleTargets,
    NonLocalTarget,
    UnsupportedReceiver,
    /// An `Array.prototype` iteration method without a kernel row:
    /// `flatMap`, `reduce` without an initial value, or a call whose
    /// argument count the row does not admit (a `thisArg`).
    ArrayIterationRowUnsupported,
    /// After the arguments or a callback, a slot the row reads
    /// (`Array.prototype` method, element reads, `ArraySpeciesCreate`) is no
    /// longer proved intact.
    ArrayIterationIntrinsicsChanged,
    /// The receiver's exact length, own-property shape or dense contents are
    /// not proved, or changed while the row ran.
    ArrayIterationReceiverUnproved,
    /// The callback is not one local, synchronous, non-generator callable
    /// with simple parameters.
    ArrayIterationCallbackUnsupported,
    /// The callback retains live lexical captures.
    ArrayIterationCapturingCallback,
    /// A callback invocation did not complete with an exact normal result
    /// the row can retain.
    ArrayIterationCallbackIncomplete,
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
                    let refusal = serde_json::to_value(refusal)
                        .expect("ordered call admission refusals serialize as strings");
                    write!(
                        f,
                        "; ordered_call_admission_refusal: {}",
                        refusal.as_str().expect("unit variant")
                    )?;
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
