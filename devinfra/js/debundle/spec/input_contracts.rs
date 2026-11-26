use crate::CapturedValueKind;
use crate::NativeCallIdentityKind;
use crate::NativeOperationKind;
use crate::PartialSwapKind;
use crate::ResolvedMemberSelector;
use crate::annotations::SourceMatchClaim;
use serde::{Deserialize, Serialize};

/// A source-bound value premise attached to one exact provider chunk.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ValueInputContract {
    /// Structural selector for exactly one declared local binding.
    pub source_match: SourceMatchClaim,
    pub value: CapturedValueContract,
    /// SHA-256 of the parser input bytes recorded for this provider entry.
    pub audited_source_sha256: String,
    /// Optional direct export name that must resolve to the selected local cell.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_export: Option<String>,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Author premise for one exact native operation initializer.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(
    try_from = "NativeOperationInputWire",
    into = "NativeOperationInputWire"
)]
pub struct NativeOperationInputContract {
    pub selector: SourceUnitSelector,
    /// Operation whose original native binding the author has audited.
    pub operation: NativeOperationKind,
    /// SHA-256 of the parser input bytes recorded for this provider entry.
    pub audited_source_sha256: String,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// One source-selected provider invocation, with both parser inputs pinned.
/// The author certifies that the selected call body reads no mutable
/// application/global/host state, invokes no host operation, and writes only
/// its fresh per-call wrapper/record allocations. Native metadata and
/// inherited/provider slots used by that body remain as audited through
/// every admitted synchronous initialization schedule. Callee and argument
/// evaluation, completion, lexical effects, callback timing/retention, and
/// later use of the returned value are separate obligations.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct InitializerCallEffectInputContract {
    /// Existing logical-module owner in the importer chunk. Frontend
    /// elaboration resolves its canonical claim to the actual local
    /// declaration before validating the initializer shape.
    pub resolved_member: ResolvedMemberSelector,
    pub audited_source_sha256: String,
    pub provider_chunk: String,
    pub provider_export: String,
    pub audited_provider_sha256: String,
    /// The audited provider returns a normal opaque callable for the one
    /// initialized literal-const primitive argument at this exact call site.
    #[serde(default)]
    pub returns_opaque_function_from_const_primitive: bool,
    /// Required when this exact export is emitted from a bundled partial
    /// vendor swap. Pins the effective replacement independently of the
    /// original provider parser input.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effective_replacement: Option<InitializerCallEffectiveReplacement>,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Author-audited identity and own-content lifetime promise for one selected
/// imported array spread. Every field is required and must be `true`; keeping
/// the promises explicit makes the capability reviewable in saved specs.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ImportedPrimitiveArrayLifetimeContract {
    pub identity_unchanged_through_spread: bool,
    pub dense_own_data_primitive_contents_unchanged_through_spread: bool,
    pub ordinary_array_prototype_chain_unchanged_through_spread: bool,
    pub no_own_iterator_or_accessors_through_spread: bool,
    pub no_rebinding_through_spread: bool,
}

/// One source-selected imported array operand. `selector_local` is a stable
/// placeholder from the consumer's source-match pattern, not a source local
/// name. The provider declaration selector resolves the actual export cell.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ImportedPrimitiveArrayOperandContract {
    pub selector_local: String,
    /// Configured chunk alias from the artifact/spec, not a physical filename.
    pub provider_chunk: String,
    pub provider_source_match: SourceMatchClaim,
    pub audited_provider_source_sha256: String,
}

/// Source-bound, exact consumer spread input. Its selectors, digest and import
/// edges locate source facts; only `lifetime` is the author assertion.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ImportedPrimitiveArraySpreadInputContract {
    pub source_match: SourceMatchClaim,
    pub operands: Vec<ImportedPrimitiveArrayOperandContract>,
    pub audited_source_sha256: String,
    pub lifetime: ImportedPrimitiveArrayLifetimeContract,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct InitializerCallEffectiveReplacement {
    pub package: String,
    pub bundle_export: String,
    pub symbol_kind: PartialSwapKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub upstream_export: Option<String>,
    pub audited_bundle_sha256: String,
}
/// Author premise that two source units of one chunk commute: neither
/// observes the other's synchronous initialization effect. Each endpoint
/// selects exactly one source unit of the option's chunk.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CommutingStatementsContract {
    pub a: SourceUnitSelector,
    pub b: SourceUnitSelector,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// One selected source unit of a provider chunk, shared by every author input
/// that anchors to a source unit (`native_operation_inputs`,
/// `commuting_statements`). Each consumer applies its own shape checks to the
/// selected unit; this type fixes only the selector grammar.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(try_from = "SourceUnitSelectorWire", into = "SourceUnitSelectorWire")]
pub enum SourceUnitSelector {
    /// A structural selector; `bindings` names at most one declared binding.
    /// Without `bindings` it selects one anonymous (non-declaring) statement.
    SourceMatch(SourceMatchClaim),
    ResolvedMember(ResolvedMemberSelector),
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SourceUnitSelectorWire {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    source_match: Option<SourceMatchClaim>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    resolved_member: Option<ResolvedMemberSelector>,
}

impl TryFrom<SourceUnitSelectorWire> for SourceUnitSelector {
    type Error = String;

    fn try_from(wire: SourceUnitSelectorWire) -> std::result::Result<Self, Self::Error> {
        match (wire.source_match, wire.resolved_member) {
            (Some(source), None) => Ok(Self::SourceMatch(source)),
            (None, Some(member)) => Ok(Self::ResolvedMember(member)),
            _ => Err("exactly one of source_match or resolved_member is required".into()),
        }
    }
}

impl From<SourceUnitSelector> for SourceUnitSelectorWire {
    fn from(selector: SourceUnitSelector) -> Self {
        match selector {
            SourceUnitSelector::SourceMatch(source) => Self {
                source_match: Some(source),
                resolved_member: None,
            },
            SourceUnitSelector::ResolvedMember(member) => Self {
                source_match: None,
                resolved_member: Some(member),
            },
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct NativeOperationInputWire {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    source_match: Option<SourceMatchClaim>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    resolved_member: Option<ResolvedMemberSelector>,
    operation: NativeOperationKind,
    audited_source_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    note: Option<String>,
}

impl TryFrom<NativeOperationInputWire> for NativeOperationInputContract {
    type Error = String;

    fn try_from(wire: NativeOperationInputWire) -> std::result::Result<Self, Self::Error> {
        Ok(Self {
            selector: SourceUnitSelector::try_from(SourceUnitSelectorWire {
                source_match: wire.source_match,
                resolved_member: wire.resolved_member,
            })?,
            operation: wire.operation,
            audited_source_sha256: wire.audited_source_sha256,
            note: wire.note,
        })
    }
}

impl From<NativeOperationInputContract> for NativeOperationInputWire {
    fn from(contract: NativeOperationInputContract) -> Self {
        let SourceUnitSelectorWire {
            source_match,
            resolved_member,
        } = contract.selector.into();
        Self {
            source_match,
            resolved_member,
            operation: contract.operation,
            audited_source_sha256: contract.audited_source_sha256,
            note: contract.note,
        }
    }
}

/// Exact source-selected native callee identity, scoped to every synchronous
/// at-init activation of the selected call site across permitted schedules.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NativeCallIdentityInputContract {
    pub source_match: SourceMatchClaim,
    pub callee: NativeCallIdentityKind,
    pub audited_source_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// One source-selected, SHA-bound local helper argument contract. The author
/// certifies that an argument at `parameter_index` is never mutated, retained,
/// returned, published, or exposed to callbacks or opaque callees by this
/// helper or anything it invokes. This includes later activations of retained
/// work and is independent of call purity and reentry.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ArgumentInputContract {
    pub source_match: SourceMatchClaim,
    pub parameter_index: usize,
    pub exact_arity: usize,
    pub audited_source_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// One source-selected callable whose computed-key method calls the author
/// audited as never installing an accessor.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NoAccessorInstallCallableContract {
    /// Selects one declaration whose statement contains exactly one outermost
    /// function.
    pub source_match: SourceMatchClaim,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CapturedValueContract {
    pub kind: CapturedValueKind,
}
