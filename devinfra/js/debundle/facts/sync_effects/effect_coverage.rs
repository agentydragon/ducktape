//! Nonterminal receipts from operations executed by the ordered local kernel.

use super::local_operations::{ObjectBindingPattern, ObjectBindingProperty};
use super::value_model::CallSite;
use crate::analysis_inputs::{
    NativeIntrinsic, NativeOperationInputEvidence, ParameterHeapRead, ProofDependencies,
};
use crate::execution_evidence::{ExecutionPrecisionLoss, SkippedCallReason};
use crate::facts::EffectCell;
use crate::facts::abstract_domain::builtins::Row;
use crate::facts::abstract_domain::value::HeapAddress;
use crate::facts::saved::SavedHeapAddress;
use crate::facts::semantic_inference::NativePremise;
use crate::reports::saved::SavedExecutionPrecisionLoss;
use crate::reports::schema::BindingEffectAuthorityReport;
use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::{Result, bail, ensure};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
pub(super) enum DomainCoverage {
    #[default]
    Unknown,
    Covered(Box<ProofDependencies>),
}

impl DomainCoverage {
    fn compose(&mut self, next: &Self) {
        match (&mut *self, next) {
            (Self::Covered(current), Self::Covered(next)) => {
                current.as_mut().extend(next.as_ref());
            }
            _ => *self = Self::Unknown,
        }
    }
}

/// Exact source root whose ordered execution may seal lexical binding effects.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct RootEffectIdentity {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) source: RootEffectSource,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) enum RootEffectSource {
    BindingInitializer {
        binding: Id,
        initializer_span: Span,
    },
    ObjectBindingInitializer {
        pattern: ObjectBindingPattern,
        declarator_span: Span,
        initializer_span: Span,
    },
    ClassDeclaration {
        binding: Id,
        declaration_span: Span,
    },
    ExpressionStatement {
        expression_span: Span,
    },
}

impl RootEffectIdentity {
    pub(super) fn binding_initialization(&self) -> Option<(&Id, Span)> {
        match &self.source {
            RootEffectSource::BindingInitializer {
                binding,
                initializer_span,
            } => Some((binding, *initializer_span)),
            RootEffectSource::ClassDeclaration {
                binding,
                declaration_span,
            } => Some((binding, *declaration_span)),
            RootEffectSource::ExpressionStatement { .. }
            | RootEffectSource::ObjectBindingInitializer { .. } => None,
        }
    }

    pub(super) fn targets_initialized(&self, initialized: &std::collections::BTreeSet<Id>) -> bool {
        match &self.source {
            RootEffectSource::ObjectBindingInitializer { pattern, .. } => pattern
                .properties
                .iter()
                .all(|property| initialized.contains(&property.binding)),
            _ => self
                .binding_initialization()
                .is_none_or(|(binding, _)| initialized.contains(binding)),
        }
    }

    pub(super) fn body_identity(&self) -> LocalBodyIdentity {
        match self.source {
            RootEffectSource::BindingInitializer {
                initializer_span, ..
            }
            | RootEffectSource::ObjectBindingInitializer {
                initializer_span, ..
            } => LocalBodyIdentity::RootInitializer {
                owner: self.owner,
                initializer_span,
            },
            RootEffectSource::ClassDeclaration {
                declaration_span, ..
            } => LocalBodyIdentity::RootClassDeclaration {
                owner: self.owner,
                declaration_span,
            },
            RootEffectSource::ExpressionStatement { expression_span } => {
                LocalBodyIdentity::RootExpression {
                    owner: self.owner,
                    expression_span,
                }
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) enum LocalBodyIdentity {
    RootInitializer {
        owner: SourceUnitOrdinal,
        initializer_span: Span,
    },
    RootClassDeclaration {
        owner: SourceUnitOrdinal,
        declaration_span: Span,
    },
    RootExpression {
        owner: SourceUnitOrdinal,
        expression_span: Span,
    },
    Callable {
        key: u32,
        owner: SourceUnitOrdinal,
    },
    Construction {
        class: u32,
        owner: SourceUnitOrdinal,
    },
    ImportedCallable(Box<ImportedCallableIdentity>),
    ProviderCallable {
        defining_module: String,
        source_sha256: String,
        source_origin: u32,
    },

    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
    },
    NativeConstruction {
        intrinsic: NativeIntrinsic,
    },
}

impl LocalBodyIdentity {
    pub(super) fn source_owner(&self) -> Option<SourceUnitOrdinal> {
        match self {
            Self::RootInitializer { owner, .. }
            | Self::RootClassDeclaration { owner, .. }
            | Self::RootExpression { owner, .. }
            | Self::Callable { owner, .. }
            | Self::Construction { owner, .. } => Some(*owner),
            Self::ImportedCallable(_)
            | Self::ProviderCallable { .. }
            | Self::Native { .. }
            | Self::NativeConstruction { .. } => None,
        }
    }

    /// Rebase the source-coordinate-bearing portion of one callable identity.
    /// Root/callable coordinates belong to `default_module`; imported call
    /// targets use their explicitly recorded defining module. Provider
    /// callable origins are already relative and are only checked.
    pub(crate) fn map_provider_source_coordinates(
        &self,
        default_module: &str,
        mut map_position: impl FnMut(&str, u32) -> Result<u32>,
        mut map_span: impl FnMut(&str, Span) -> Result<Span>,
        mut check_relative: impl FnMut(&str, &str, u32) -> Result<()>,
    ) -> Result<Self> {
        Ok(match self {
            Self::RootInitializer {
                owner,
                initializer_span,
            } => Self::RootInitializer {
                owner: *owner,
                initializer_span: map_span(default_module, *initializer_span)?,
            },
            Self::RootClassDeclaration {
                owner,
                declaration_span,
            } => Self::RootClassDeclaration {
                owner: *owner,
                declaration_span: map_span(default_module, *declaration_span)?,
            },
            Self::RootExpression {
                owner,
                expression_span,
            } => Self::RootExpression {
                owner: *owner,
                expression_span: map_span(default_module, *expression_span)?,
            },
            Self::Callable { key, owner } => Self::Callable {
                key: map_position(default_module, *key)?,
                owner: *owner,
            },
            Self::Construction { class, owner } => Self::Construction {
                class: map_position(default_module, *class)?,
                owner: *owner,
            },
            Self::ImportedCallable(identity) => {
                let mut identity = identity.as_ref().clone();
                identity.key = map_position(&identity.defining_module, identity.key)?;
                Self::ImportedCallable(Box::new(identity))
            }
            Self::ProviderCallable {
                defining_module,
                source_sha256,
                source_origin,
            } => {
                check_relative(defining_module, source_sha256, *source_origin)?;
                Self::ProviderCallable {
                    defining_module: defining_module.clone(),
                    source_sha256: source_sha256.clone(),
                    source_origin: *source_origin,
                }
            }
            Self::Native {
                intrinsic,
                property,
            } => Self::Native {
                intrinsic: *intrinsic,
                property: property.clone(),
            },
            Self::NativeConstruction { intrinsic } => Self::NativeConstruction {
                intrinsic: *intrinsic,
            },
        })
    }

    fn matches_current_provider_callable(
        &self,
        current: &mut impl FnMut(&str, &str, u32) -> bool,
    ) -> bool {
        match self {
            Self::ProviderCallable {
                defining_module,
                source_sha256,
                source_origin,
            } => current(defining_module, source_sha256, *source_origin),
            _ => true,
        }
    }
}

/// One source operation node after canonical local-body lowering. Ordinals
/// count source nodes within one owner/body; `step` distinguishes semantic
/// operations that share one syntax node (for example callee GetValue and
/// invocation).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceOperationSite {
    pub(crate) owner: SourceUnitOrdinal,
    pub(crate) body: LocalBodyIdentity,
    pub(crate) span: Span,
    pub(crate) ordinal: u32,
    pub(crate) step: OperationStep,
    /// For a statement `if` arm, the ordinal of the exact `IfTest` site that
    /// selected it. Canonical lowering retains this relation in saved identity.
    #[serde(default)]
    pub(crate) control_test_ordinal: Option<u32>,
}

/// A call edge that is reachable on at least one control-flow alternative.
/// This remains distinct from [`LocalCallReceipt`], which proves a selected
/// invocation occurred on the represented execution.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct PossibleLocalCallObligation {
    pub(crate) site: SourceOperationSite,
    pub(crate) possible_targets: Vec<LocalBodyIdentity>,
    pub(crate) unresolved_target: bool,
    pub(crate) dependencies: Box<ProofDependencies>,
}

impl PossibleLocalCallObligation {
    pub(crate) fn new(
        site: SourceOperationSite,
        target: Option<LocalBodyIdentity>,
        dependencies: &ProofDependencies,
    ) -> Self {
        let mut obligation = Self {
            site,
            possible_targets: Vec::new(),
            unresolved_target: false,
            dependencies: Box::default(),
        };
        obligation.include_target(target);
        obligation.dependencies.extend(dependencies);
        obligation
    }

    pub(crate) fn include_target(&mut self, target: Option<LocalBodyIdentity>) {
        match target {
            Some(target) if !self.possible_targets.contains(&target) => {
                self.possible_targets.push(target);
            }
            Some(_) => {}
            None => self.unresolved_target = true,
        }
    }

    pub(crate) fn merge(&mut self, other: &Self) {
        assert_eq!(self.site, other.site);
        for target in &other.possible_targets {
            self.include_target(Some(target.clone()));
        }
        self.unresolved_target |= other.unresolved_target;
        self.dependencies.extend(other.dependencies.as_ref());
    }
}

/// A source site skipped in some paths but not necessarily every path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct PossibleSkippedCallObligation {
    pub(crate) caller: LocalBodyIdentity,
    pub(crate) site: SourceOperationSite,
    pub(crate) region_site: SourceOperationSite,
    pub(crate) frontier_site: SourceOperationSite,
    pub(crate) reason: SkippedCallReason,
    pub(crate) dependencies: Box<ProofDependencies>,
}

impl PossibleSkippedCallObligation {
    pub(crate) fn from_receipt(receipt: &SkippedCallReceipt) -> Self {
        Self {
            caller: receipt.caller.clone(),
            site: receipt.site.clone(),
            region_site: receipt.region_site.clone(),
            frontier_site: receipt.frontier_site.clone(),
            reason: receipt.reason,
            dependencies: receipt.dependencies.clone(),
        }
    }

    pub(crate) fn same_key(&self, other: &Self) -> bool {
        self.caller == other.caller
            && self.site == other.site
            && self.region_site == other.region_site
            && self.frontier_site == other.frontier_site
            && self.reason == other.reason
    }

    pub(crate) fn merge(&mut self, other: &Self) {
        assert!(self.same_key(other));
        self.dependencies.extend(other.dependencies.as_ref());
    }
}

/// The semantic step represented by a source operation node. Indexed children
/// such as call arguments have their own source node and therefore do not need
/// to encode an AST-derived offset here.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum OperationStep {
    Statement,
    Expression,
    InitializeBinding,
    InitializeObjectBinding,
    Return,
    Throw,
    TryBody,
    CatchHandler,
    IfTest,
    IfConsequent,
    IfAlternate,
    ConditionalTest,
    ConditionalConsequent,
    ConditionalAlternate,
    LogicalTest,
    LogicalRight,
    BodyRegion,
    BinaryCoercion,
    BinaryOperator,
    TemplateCoercion,
    CalleeReceiver,
    OptionalReceiverCheck,
    CallCalleeGetValue,
    OptionalCalleeCheck,
    CallInvocation,
    BindingGetValue,
    FieldGetValue,
    BindingPutValue,
    FieldPutValue,
    UpdateCoercion,
    UpdatePutValue,
    Allocate,
    DefineField,
    IterateSpread,
}

impl SourceOperationSite {
    /// Source owner for this canonical operation site.
    pub fn owner(&self) -> SourceUnitOrdinal {
        self.owner
    }

    /// Exact source span used by report projections and diagnostics.
    pub fn span(&self) -> Span {
        self.span
    }

    fn key(&self) -> SourceOperationSiteKey {
        SourceOperationSiteKey {
            owner: self.owner.0,
            body: local_body_key(&self.body),
            span_lo: self.span.lo.0,
            span_hi: self.span.hi.0,
            ordinal: self.ordinal,
            step: self.step,
            control_test_ordinal: self.control_test_ordinal,
        }
    }
}

/// Compact ordered identity because SWC `Span` itself is not `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct SourceOperationSiteKey {
    owner: usize,
    body: LocalBodyKey,
    span_lo: u32,
    span_hi: u32,
    ordinal: u32,
    step: OperationStep,
    control_test_ordinal: Option<u32>,
}

impl PartialOrd for SourceOperationSite {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for SourceOperationSite {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.key().cmp(&other.key())
    }
}

/// The exact intrinsic selected by an executed callee GetValue. The ordered
/// caller transports this snapshot across operand evaluation to the row transfer.
#[derive(Debug, Clone)]
pub(super) struct NativeCalleeSelection {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) site: CallSite,
    pub(super) row: &'static Row,
    pub(super) premise: NativePremise,
    /// Exact-site author input when census integrity is unavailable. It is
    /// carried across operand evaluation so the row transfer consumes the
    /// same assertion that selected the original intrinsic.
    pub(super) native_operation_input: Option<Box<NativeOperationInputEvidence>>,
}

/// Kept out of every ordinary local edge's inline layout.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub(crate) struct ImportedCallableIdentity {
    pub(super) defining_module: String,
    pub(super) export: String,
    pub(super) local: String,
    pub(super) member: Option<String>,
    pub(super) key: u32,
}

impl ImportedCallableIdentity {
    pub(crate) fn defining_module(&self) -> &str {
        &self.defining_module
    }

    pub(crate) fn body_key(&self) -> u32 {
        self.key
    }
}

/// One selected direct local invocation. The flat edge set avoids expanding
/// every root-to-leaf path; each edge retains prerequisites merged from every
/// invocation context that selected it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct LocalCallReceipt {
    pub(super) caller: LocalBodyIdentity,
    pub(super) site: SourceOperationSite,
    pub(super) target: LocalBodyIdentity,
    pub(super) dependencies: Box<ProofDependencies>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct SkippedCallReceipt {
    pub(super) caller: LocalBodyIdentity,
    pub(super) site: SourceOperationSite,
    pub(super) region_site: SourceOperationSite,
    pub(super) frontier_site: SourceOperationSite,
    pub(super) reason: SkippedCallReason,
    pub(super) dependencies: Box<ProofDependencies>,
}

pub(super) fn skipped_reason_matches_sites(
    reason: SkippedCallReason,
    region: &SourceOperationSite,
    frontier: &SourceOperationSite,
) -> bool {
    match reason {
        SkippedCallReason::KnownThrow => matches!(
            frontier.step,
            OperationStep::Throw
                | OperationStep::BinaryOperator
                | OperationStep::CallInvocation
                | OperationStep::TemplateCoercion
                | OperationStep::UpdateCoercion
        ),
        SkippedCallReason::KnownReturn => frontier.step == OperationStep::Return,
        SkippedCallReason::HandlerNotEntered => {
            region.step == OperationStep::CatchHandler
                && matches!(
                    frontier.step,
                    OperationStep::TryBody | OperationStep::Return
                )
        }
        SkippedCallReason::IfConsequentNotSelected => {
            region.step == OperationStep::IfConsequent
                && frontier.step == OperationStep::IfTest
                && region.control_test_ordinal == Some(frontier.ordinal)
        }
        SkippedCallReason::IfAlternateNotSelected => {
            region.step == OperationStep::IfAlternate
                && frontier.step == OperationStep::IfTest
                && region.control_test_ordinal == Some(frontier.ordinal)
        }
    }
}

pub(super) fn skipped_call_site_is_within_region(
    site: &SourceOperationSite,
    region: &SourceOperationSite,
) -> bool {
    site.owner == region.owner
        && site.body == region.body
        && region.span.lo <= site.span.lo
        && site.span.hi <= region.span.hi
}

fn call_receipt_references_source_sites(
    call: &LocalCallReceipt,
    source_sites: &std::collections::BTreeSet<SourceOperationSite>,
) -> bool {
    call.site.step == OperationStep::CallInvocation
        && call.caller.source_owner() == Some(call.site.owner)
        && call.site.body == call.caller
        && source_sites.contains(&call.site)
}

fn skipped_receipt_references_source_sites(
    call: &SkippedCallReceipt,
    source_sites: &std::collections::BTreeSet<SourceOperationSite>,
) -> bool {
    call.site.step == OperationStep::CallInvocation
        && matches!(
            call.region_site.step,
            OperationStep::BodyRegion
                | OperationStep::TryBody
                | OperationStep::CatchHandler
                | OperationStep::IfConsequent
                | OperationStep::IfAlternate
        )
        && skipped_reason_matches_sites(call.reason, &call.region_site, &call.frontier_site)
        && call.site.body == call.caller
        && call.region_site.body == call.caller
        && call.frontier_site.body == call.caller
        && call.site.owner == call.region_site.owner
        && call.site.owner == call.frontier_site.owner
        && skipped_call_site_is_within_region(&call.site, &call.region_site)
        && call.caller.source_owner() == Some(call.site.owner)
        && source_sites.contains(&call.site)
        && source_sites.contains(&call.region_site)
        && source_sites.contains(&call.frontier_site)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct LexicalBindingEffectReceipt {
    pub(super) identity: RootEffectIdentity,
    pub(super) read_dependencies: ProofDependencies,
    pub(super) rebind_dependencies: ProofDependencies,
    pub(super) calls: Vec<LocalCallReceipt>,
    pub(super) skipped_calls: Vec<SkippedCallReceipt>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum BindingEffectCoverage {
    #[default]
    Unclassified,
    LegacyExecution,
    OrderedRoot {
        identity: RootEffectIdentity,
        receipt: Option<Box<LexicalBindingEffectReceipt>>,
    },
    RetainedEffectClosure {
        receipt: Box<LexicalBindingEffectReceipt>,
        dependencies: Box<ProofDependencies>,
        precision_losses: Vec<ExecutionPrecisionLoss>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BindingEffectAuthority {
    Unclassified,
    LegacyExecution { complete: bool },
    AuthorSourceOnly,
    OrderedRootUncovered,
    OperationReceipt,
    RetainedEffectClosure,
}

impl BindingEffectCoverage {
    pub(crate) fn belongs_to_source_owner(&self, owner: SourceUnitOrdinal) -> bool {
        match self {
            Self::OrderedRoot { identity, .. } => identity.owner == owner,
            Self::RetainedEffectClosure { receipt, .. } => receipt.identity.owner == owner,
            Self::Unclassified | Self::LegacyExecution => true,
        }
    }

    /// Saved execution receipts may reference sites from the independently
    /// prepared source table, but cannot add or authenticate those sites.
    pub(crate) fn references_source_sites(
        &self,
        source_sites: &std::collections::BTreeSet<SourceOperationSite>,
    ) -> bool {
        let receipt = match self {
            Self::OrderedRoot {
                receipt: Some(receipt),
                ..
            }
            | Self::RetainedEffectClosure { receipt, .. } => Some(receipt.as_ref()),
            Self::Unclassified | Self::LegacyExecution | Self::OrderedRoot { .. } => None,
        };
        let Some(receipt) = receipt else {
            return true;
        };
        receipt.calls.iter().all(|call| {
            call.site.owner == receipt.identity.owner
                && call_receipt_references_source_sites(call, source_sites)
        }) && receipt.skipped_calls.iter().all(|call| {
            call.site.owner == receipt.identity.owner
                && call.region_site.owner == receipt.identity.owner
                && call.frontier_site.owner == receipt.identity.owner
                && skipped_receipt_references_source_sites(call, source_sites)
        })
    }

    pub(crate) fn provider_callables_are_current(
        &self,
        current: &mut impl FnMut(&str, &str, u32) -> bool,
    ) -> bool {
        let receipt = match self {
            Self::OrderedRoot {
                receipt: Some(receipt),
                ..
            }
            | Self::RetainedEffectClosure { receipt, .. } => Some(receipt.as_ref()),
            Self::Unclassified | Self::LegacyExecution | Self::OrderedRoot { .. } => None,
        };
        receipt.is_none_or(|receipt| {
            receipt.calls.iter().all(|call| {
                call.caller.matches_current_provider_callable(current)
                    && call.target.matches_current_provider_callable(current)
                    && source_site_provider_callable_is_current(&call.site, current)
            }) && receipt.skipped_calls.iter().all(|call| {
                call.caller.matches_current_provider_callable(current)
                    && source_site_provider_callable_is_current(&call.site, current)
                    && source_site_provider_callable_is_current(&call.region_site, current)
                    && source_site_provider_callable_is_current(&call.frontier_site, current)
            })
        })
    }

    /// Whether this saved execution authority contains a selected edge to a
    /// provider callable. The artifact's target is not authenticated by its
    /// endpoint identity alone; callers use this only to select the existing
    /// current-source execution path that regenerates the edge.
    pub(crate) fn has_provider_callable_target(&self) -> bool {
        let receipt = match self {
            Self::OrderedRoot {
                receipt: Some(receipt),
                ..
            }
            | Self::RetainedEffectClosure { receipt, .. } => Some(receipt.as_ref()),
            Self::Unclassified | Self::LegacyExecution | Self::OrderedRoot { .. } => None,
        };
        receipt.is_some_and(|receipt| {
            receipt
                .calls
                .iter()
                .any(|call| matches!(call.target, LocalBodyIdentity::ProviderCallable { .. }))
        })
    }

    pub(crate) fn snapshot(
        &self,
        ordinals: &ContextOrdinals,
    ) -> Result<SavedBindingEffectCoverage<'_>> {
        Ok(match self {
            Self::Unclassified => SavedBindingEffectCoverage::Unclassified,
            Self::LegacyExecution => SavedBindingEffectCoverage::LegacyExecution,
            Self::OrderedRoot { identity, receipt } => SavedBindingEffectCoverage::OrderedRoot {
                identity: SavedRootEffectIdentity::snapshot(identity, ordinals)?,
                receipt: receipt
                    .as_deref()
                    .map(|receipt| SavedLexicalBindingEffectReceipt::snapshot(receipt, ordinals))
                    .transpose()?
                    .map(Box::new),
            },
            Self::RetainedEffectClosure {
                receipt,
                dependencies,
                precision_losses,
            } => SavedBindingEffectCoverage::RetainedEffectClosure {
                receipt: Box::new(SavedLexicalBindingEffectReceipt::snapshot(
                    receipt, ordinals,
                )?),
                dependencies: dependencies.clone(),
                precision_losses: precision_losses
                    .iter()
                    .map(SavedExecutionPrecisionLoss::snapshot)
                    .collect(),
            },
        })
    }

    /// Actual lexical operation coverage for this exact root. Legacy
    /// completion and author source-only assertions supply no such proof.
    pub(crate) fn operation_dependencies(
        &self,
        owner: SourceUnitOrdinal,
    ) -> Option<ProofDependencies> {
        let (identity, receipt) = match self {
            Self::OrderedRoot {
                identity,
                receipt: Some(receipt),
            } => (identity, receipt),
            Self::RetainedEffectClosure { receipt, .. } => (&receipt.identity, receipt),
            _ => return None,
        };
        if identity.owner != owner || receipt.identity != *identity {
            return None;
        }
        let mut dependencies = receipt.read_dependencies.clone();
        dependencies.extend(&receipt.rebind_dependencies);
        for call in &receipt.calls {
            dependencies.extend(&call.dependencies);
        }
        for call in &receipt.skipped_calls {
            dependencies.extend(&call.dependencies);
        }
        Some(dependencies)
    }

    pub(crate) fn retained_effect_dependencies(
        &self,
        owner: SourceUnitOrdinal,
        losses: &[ExecutionPrecisionLoss],
    ) -> Option<&ProofDependencies> {
        let Self::RetainedEffectClosure {
            receipt,
            dependencies,
            precision_losses,
        } = self
        else {
            return None;
        };
        (receipt.identity.owner == owner
            && !losses.is_empty()
            && losses.len() == precision_losses.len()
            && losses
                .iter()
                .all(|loss| precision_losses.iter().any(|held| same_loss(held, loss))))
        .then_some(dependencies)
    }

    pub(crate) fn authority(
        &self,
        complete: bool,
        author_source_only: bool,
    ) -> BindingEffectAuthority {
        if author_source_only {
            return BindingEffectAuthority::AuthorSourceOnly;
        }
        match self {
            Self::Unclassified => BindingEffectAuthority::Unclassified,
            Self::LegacyExecution => BindingEffectAuthority::LegacyExecution { complete },
            Self::OrderedRoot {
                receipt: Some(_), ..
            } => BindingEffectAuthority::OperationReceipt,
            Self::OrderedRoot { receipt: None, .. } => BindingEffectAuthority::OrderedRootUncovered,
            Self::RetainedEffectClosure { .. } => BindingEffectAuthority::RetainedEffectClosure,
        }
    }

    pub(crate) fn authority_report(
        &self,
        complete: bool,
        author_source_only: bool,
    ) -> BindingEffectAuthorityReport {
        match self.authority(complete, author_source_only) {
            BindingEffectAuthority::Unclassified => BindingEffectAuthorityReport::Unclassified,
            BindingEffectAuthority::AuthorSourceOnly => {
                BindingEffectAuthorityReport::AuthorSourceOnly
            }
            BindingEffectAuthority::LegacyExecution { complete } => {
                BindingEffectAuthorityReport::LegacyExecution { complete }
            }
            BindingEffectAuthority::OperationReceipt => {
                let Self::OrderedRoot {
                    receipt: Some(receipt),
                    ..
                } = self
                else {
                    unreachable!("typed authority comes from this receipt");
                };
                BindingEffectAuthorityReport::OperationReceipt {
                    receipt: Box::new(receipt.as_ref().to_report()),
                }
            }
            BindingEffectAuthority::OrderedRootUncovered => {
                let Self::OrderedRoot { identity, .. } = self else {
                    unreachable!("typed authority comes from this root attempt");
                };
                match &identity.source {
                    RootEffectSource::BindingInitializer {
                        binding,
                        initializer_span,
                    } => BindingEffectAuthorityReport::OrderedRootInitializerUncovered {
                        owner: identity.owner,
                        binding: binding.0.to_string(),
                        initializer_span: Some(*initializer_span),
                    },
                    RootEffectSource::ObjectBindingInitializer {
                        pattern,
                        declarator_span,
                        initializer_span,
                    } => BindingEffectAuthorityReport::OrderedRootObjectBindingUncovered {
                        owner: identity.owner,
                        pattern_span: pattern.span,
                        declarator_span: *declarator_span,
                        initializer_span: *initializer_span,
                        targets: pattern.to_report(),
                    },
                    RootEffectSource::ClassDeclaration {
                        binding,
                        declaration_span,
                    } => BindingEffectAuthorityReport::OrderedRootClassDeclarationUncovered {
                        owner: identity.owner,
                        binding: binding.0.to_string(),
                        declaration_span: *declaration_span,
                    },
                    RootEffectSource::ExpressionStatement { expression_span } => {
                        BindingEffectAuthorityReport::OrderedRootExpressionUncovered {
                            owner: identity.owner,
                            expression_span: *expression_span,
                        }
                    }
                }
            }
            BindingEffectAuthority::RetainedEffectClosure => {
                let Self::RetainedEffectClosure {
                    receipt,
                    dependencies,
                    precision_losses,
                } = self
                else {
                    unreachable!("retained authority comes from its complete closure");
                };
                BindingEffectAuthorityReport::RetainedEffectClosure {
                    receipt: Box::new(receipt.to_report()),
                    dependencies: dependencies.clone(),
                    precision_losses: precision_losses.clone(),
                }
            }
        }
    }
}

/// Process-independent binding-effect authority retained by staged analysis.
#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SavedBindingEffectCoverage<'a> {
    Unclassified,
    LegacyExecution,
    OrderedRoot {
        identity: SavedRootEffectIdentity,
        receipt: Option<Box<SavedLexicalBindingEffectReceipt>>,
    },
    RetainedEffectClosure {
        receipt: Box<SavedLexicalBindingEffectReceipt>,
        dependencies: Box<ProofDependencies>,
        precision_losses: Vec<SavedExecutionPrecisionLoss<'a>>,
    },
}

impl SavedBindingEffectCoverage<'_> {
    pub(crate) fn restore(self, ordinals: &ContextOrdinals) -> Result<BindingEffectCoverage> {
        Ok(match self {
            Self::Unclassified => BindingEffectCoverage::Unclassified,
            Self::LegacyExecution => BindingEffectCoverage::LegacyExecution,
            Self::OrderedRoot { identity, receipt } => BindingEffectCoverage::OrderedRoot {
                identity: identity.restore(ordinals)?,
                receipt: receipt
                    .map(|receipt| receipt.restore(ordinals))
                    .transpose()?
                    .map(Box::new),
            },
            Self::RetainedEffectClosure {
                receipt,
                dependencies,
                precision_losses,
            } => BindingEffectCoverage::RetainedEffectClosure {
                receipt: Box::new(receipt.restore(ordinals)?),
                dependencies,
                precision_losses: precision_losses
                    .into_iter()
                    .map(SavedExecutionPrecisionLoss::restore)
                    .collect(),
            },
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedRootEffectIdentity {
    owner: SourceUnitOrdinal,
    source: SavedRootEffectSource,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
enum SavedRootEffectSource {
    ObjectBindingInitializer {
        pattern_span: Span,
        properties: Vec<SavedObjectBindingProperty>,
        declarator_span: Span,
        initializer_span: Span,
    },
    BindingInitializer {
        binding: SavedBinding,
        initializer_span: Span,
    },
    ClassDeclaration {
        binding: SavedBinding,
        declaration_span: Span,
    },
    ExpressionStatement {
        expression_span: Span,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SavedObjectBindingProperty {
    property: String,
    binding: SavedBinding,
    span: Span,
}

impl SavedRootEffectIdentity {
    fn snapshot(identity: &RootEffectIdentity, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            owner: identity.owner,
            source: match &identity.source {
                RootEffectSource::BindingInitializer {
                    binding,
                    initializer_span,
                } => SavedRootEffectSource::BindingInitializer {
                    binding: ordinals.snapshot_binding(binding)?,
                    initializer_span: *initializer_span,
                },
                RootEffectSource::ObjectBindingInitializer {
                    pattern,
                    declarator_span,
                    initializer_span,
                } => SavedRootEffectSource::ObjectBindingInitializer {
                    pattern_span: pattern.span,
                    properties: pattern
                        .properties
                        .iter()
                        .map(|property| {
                            Ok(SavedObjectBindingProperty {
                                property: property.property.clone(),
                                binding: ordinals.snapshot_binding(&property.binding)?,
                                span: property.span,
                            })
                        })
                        .collect::<Result<_>>()?,
                    declarator_span: *declarator_span,
                    initializer_span: *initializer_span,
                },
                RootEffectSource::ClassDeclaration {
                    binding,
                    declaration_span,
                } => SavedRootEffectSource::ClassDeclaration {
                    binding: ordinals.snapshot_binding(binding)?,
                    declaration_span: *declaration_span,
                },
                RootEffectSource::ExpressionStatement { expression_span } => {
                    SavedRootEffectSource::ExpressionStatement {
                        expression_span: *expression_span,
                    }
                }
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<RootEffectIdentity> {
        Ok(RootEffectIdentity {
            owner: self.owner,
            source: match self.source {
                SavedRootEffectSource::BindingInitializer {
                    binding,
                    initializer_span,
                } => RootEffectSource::BindingInitializer {
                    binding: ordinals.restore_binding(binding)?,
                    initializer_span,
                },
                SavedRootEffectSource::ObjectBindingInitializer {
                    pattern_span,
                    properties,
                    declarator_span,
                    initializer_span,
                } => RootEffectSource::ObjectBindingInitializer {
                    pattern: ObjectBindingPattern {
                        span: pattern_span,
                        properties: properties
                            .into_iter()
                            .map(|property| {
                                Ok(ObjectBindingProperty {
                                    property: property.property,
                                    binding: ordinals.restore_binding(property.binding)?,
                                    span: property.span,
                                })
                            })
                            .collect::<Result<_>>()?,
                    },
                    declarator_span,
                    initializer_span,
                },
                SavedRootEffectSource::ClassDeclaration {
                    binding,
                    declaration_span,
                } => RootEffectSource::ClassDeclaration {
                    binding: ordinals.restore_binding(binding)?,
                    declaration_span,
                },
                SavedRootEffectSource::ExpressionStatement { expression_span } => {
                    RootEffectSource::ExpressionStatement { expression_span }
                }
            },
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedLexicalBindingEffectReceipt {
    identity: SavedRootEffectIdentity,
    read_dependencies: ProofDependencies,
    rebind_dependencies: ProofDependencies,
    calls: Vec<LocalCallReceipt>,
    skipped_calls: Vec<SkippedCallReceipt>,
}

impl SavedLexicalBindingEffectReceipt {
    fn snapshot(receipt: &LexicalBindingEffectReceipt, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            identity: SavedRootEffectIdentity::snapshot(&receipt.identity, ordinals)?,
            read_dependencies: receipt.read_dependencies.clone(),
            rebind_dependencies: receipt.rebind_dependencies.clone(),
            calls: receipt.calls.clone(),
            skipped_calls: receipt.skipped_calls.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<LexicalBindingEffectReceipt> {
        Ok(LexicalBindingEffectReceipt {
            identity: self.identity.restore(ordinals)?,
            read_dependencies: self.read_dependencies,
            rebind_dependencies: self.rebind_dependencies,
            calls: self.calls,
            skipped_calls: self.skipped_calls,
        })
    }
}

impl LexicalBindingEffectReceipt {
    pub(super) fn to_report(&self) -> crate::reports::schema::LexicalBindingEffectReceipt {
        use crate::reports::schema::{
            LexicalBindingEffectCall, LexicalBindingEffectReceipt, LexicalBindingEffectSkippedCall,
            LexicalEffectRoot,
        };

        LexicalBindingEffectReceipt {
            owner: self.identity.owner,
            root: match &self.identity.source {
                RootEffectSource::BindingInitializer {
                    binding,
                    initializer_span,
                } => LexicalEffectRoot::BindingInitializer {
                    binding: binding.0.to_string(),
                    initializer_span: *initializer_span,
                },
                RootEffectSource::ObjectBindingInitializer {
                    pattern,
                    declarator_span,
                    initializer_span,
                } => LexicalEffectRoot::ObjectBindingInitializer {
                    pattern_span: pattern.span,
                    declarator_span: *declarator_span,
                    initializer_span: *initializer_span,
                    targets: pattern.to_report(),
                },
                RootEffectSource::ClassDeclaration {
                    binding,
                    declaration_span,
                } => LexicalEffectRoot::ClassDeclaration {
                    binding: binding.0.to_string(),
                    declaration_span: *declaration_span,
                },
                RootEffectSource::ExpressionStatement { expression_span } => {
                    LexicalEffectRoot::ExpressionStatement {
                        expression_span: *expression_span,
                    }
                }
            },
            read_dependencies: self.read_dependencies.clone(),
            rebind_dependencies: self.rebind_dependencies.clone(),
            calls: self
                .calls
                .iter()
                .map(|call| LexicalBindingEffectCall {
                    caller: call.caller.to_report(),
                    call_span: call.site.span,
                    target: call.target.to_report(),
                    dependencies: call.dependencies.as_ref().clone(),
                })
                .collect(),
            skipped_calls: self
                .skipped_calls
                .iter()
                .map(|call| LexicalBindingEffectSkippedCall {
                    caller: call.caller.to_report(),
                    call_span: call.site.span,
                    region_span: call.region_site.span,
                    frontier_span: call.frontier_site.span,
                    reason: call.reason,
                    dependencies: call.dependencies.as_ref().clone(),
                })
                .collect(),
        }
    }
}

impl LocalBodyIdentity {
    fn to_report(&self) -> crate::reports::schema::LexicalBindingEffectBody {
        use crate::reports::schema::LexicalBindingEffectBody;
        match self {
            Self::RootInitializer {
                owner,
                initializer_span,
            } => LexicalBindingEffectBody::RootInitializer {
                owner: *owner,
                initializer_span: *initializer_span,
            },
            Self::RootClassDeclaration {
                owner,
                declaration_span,
            } => LexicalBindingEffectBody::RootClassDeclaration {
                owner: *owner,
                declaration_span: *declaration_span,
            },
            Self::RootExpression {
                owner,
                expression_span,
            } => LexicalBindingEffectBody::RootExpression {
                owner: *owner,
                expression_span: *expression_span,
            },
            Self::Callable { key, owner } => LexicalBindingEffectBody::Callable {
                key: *key,
                owner: *owner,
            },
            Self::Construction { class, owner } => LexicalBindingEffectBody::Construction {
                class: *class,
                owner: *owner,
            },
            Self::ImportedCallable(identity) => LexicalBindingEffectBody::ImportedCallable {
                defining_module: identity.defining_module.clone(),
                export: identity.export.clone(),
                local: identity.local.clone(),
                member: identity.member.clone(),
                key: identity.key,
            },
            Self::ProviderCallable {
                defining_module,
                source_sha256,
                source_origin,
            } => LexicalBindingEffectBody::ProviderCallable {
                defining_module: defining_module.clone(),
                source_sha256: source_sha256.clone(),
                source_origin: *source_origin,
            },
            Self::Native {
                intrinsic,
                property,
            } => LexicalBindingEffectBody::Native {
                intrinsic: *intrinsic,
                property: property.clone(),
            },
            Self::NativeConstruction { intrinsic } => {
                LexicalBindingEffectBody::NativeConstruction {
                    intrinsic: *intrinsic,
                }
            }
        }
    }
}

/// This describes only the operations of one execution. It never identifies
/// or certifies a whole source owner, and may travel through invocation memos.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub(crate) struct EffectCoverageFragment {
    initialization: Option<Box<InitializationEffectCoverage>>,
    lexical: Option<Box<LexicalEffectCoverage>>,
    call_observations: Option<Box<CallObservationCoverage>>,
    /// Loss sites whose retained suffix was covered through the same kernel.
    /// This is not an execution-completion or returned-value certificate.
    retained_losses: Option<Box<RetainedLossCoverage>>,
    /// Typed prerequisites for the successful own-data operations in this
    /// fragment. These receipts never close an effect domain themselves.
    operation_support: Option<Box<[OperationSupportReceipt]>>,
}

/// Which descriptor/shape proof an already admitted own-data operation read.
/// Provider arguments stay parameterized until the caller instantiates them;
/// pooled allocation ordinals are never compared across provider worlds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum OperationSupportLocation {
    PooledObjectShape {
        origin: HeapAddress,
    },
    /// Address from an isolated provider-body execution. Birth and escape are
    /// not established by its ordinal, so callers must keep it unresolved.
    ProviderWorldObjectShape {
        origin: HeapAddress,
    },
    /// This exact access observed a frame-born, nonborrowed object. It remains
    /// unresolved until the complete normal provider transfer seals the body.
    ProviderLocalBirthCandidate {
        origin: HeapAddress,
    },
    /// A completed provider-local birth proof. No provider allocation address
    /// survives sealing, so this cannot be compared with another provider or
    /// projected into a caller heap cell.
    ProviderLocalBirth,
    ParameterHeapRead {
        read: ParameterHeapRead,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationSupportOperation {
    OwnDataRead,
    WritableOwnDataStore,
    ObjectSpreadCopy,
}

/// Evidence that a selected present own-data operation consumed descriptor
/// or shape state. It is diagnostic and a read-footprint projection only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperationSupportReceipt {
    pub(crate) operation: OperationSupportOperation,
    pub(crate) support: OperationSupportLocation,
    pub(crate) dependencies: Box<ProofDependencies>,
}

impl OperationSupportReceipt {
    pub(crate) fn pooled_object_shape(
        operation: OperationSupportOperation,
        origin: HeapAddress,
        dependencies: &ProofDependencies,
    ) -> Self {
        Self {
            operation,
            support: OperationSupportLocation::PooledObjectShape { origin },
            dependencies: Box::new(dependencies.clone()),
        }
    }

    pub(crate) fn parameter_object_shape(
        operation: OperationSupportOperation,
        argument: usize,
        dependencies: &ProofDependencies,
    ) -> Self {
        Self {
            operation,
            support: OperationSupportLocation::ParameterHeapRead {
                read: ParameterHeapRead::ObjectShape { argument },
            },
            dependencies: Box::new(dependencies.clone()),
        }
    }

    pub(crate) fn provider_world_object_shape(
        operation: OperationSupportOperation,
        origin: HeapAddress,
        dependencies: &ProofDependencies,
    ) -> Self {
        Self {
            operation,
            support: OperationSupportLocation::ProviderWorldObjectShape { origin },
            dependencies: Box::new(dependencies.clone()),
        }
    }

    pub(crate) fn provider_local_birth_candidate(
        operation: OperationSupportOperation,
        origin: HeapAddress,
        dependencies: &ProofDependencies,
    ) -> Self {
        Self {
            operation,
            support: OperationSupportLocation::ProviderLocalBirthCandidate { origin },
            dependencies: Box::new(dependencies.clone()),
        }
    }

    pub(crate) fn projected_cell(&self) -> Option<EffectCell> {
        match &self.support {
            OperationSupportLocation::PooledObjectShape { origin } => {
                Some(EffectCell::ObjectShape(*origin))
            }
            OperationSupportLocation::ProviderWorldObjectShape { .. }
            | OperationSupportLocation::ProviderLocalBirthCandidate { .. }
            | OperationSupportLocation::ParameterHeapRead { .. } => None,
            OperationSupportLocation::ProviderLocalBirth => None,
        }
    }

    fn instantiate_parameter(&mut self, arguments: &[HeapAddress]) -> bool {
        if matches!(
            &self.support,
            OperationSupportLocation::ProviderWorldObjectShape { .. }
                | OperationSupportLocation::ProviderLocalBirthCandidate { .. }
        ) {
            return false;
        }
        let OperationSupportLocation::ParameterHeapRead { read } = &self.support else {
            return true;
        };
        let ParameterHeapRead::ObjectShape { argument } = read else {
            return false;
        };
        let Some(origin) = arguments.get(*argument) else {
            return false;
        };
        if origin.kind() != Some(crate::facts::abstract_domain::value::Kind::Object) {
            return false;
        }
        self.support = OperationSupportLocation::PooledObjectShape { origin: *origin };
        true
    }
}

#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
struct RetainedLossCoverage {
    sites: Vec<ExecutionPrecisionLoss>,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct SavedEffectCoverageFragment<'a> {
    initialization: Cow<'a, Option<Box<InitializationEffectCoverage>>>,
    lexical: Cow<'a, Option<Box<LexicalEffectCoverage>>>,
    call_observations: Cow<'a, Option<Box<CallObservationCoverage>>>,
    retained_losses: Option<Vec<SavedExecutionPrecisionLoss<'a>>>,
    operation_support: Vec<SavedOperationSupportReceipt>,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct SavedOperationSupportReceipt {
    operation: OperationSupportOperation,
    support: SavedOperationSupportLocation,
    dependencies: Box<ProofDependencies>,
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
enum SavedOperationSupportLocation {
    PooledObjectShape { origin: SavedHeapAddress },
    ProviderWorldObjectShape { origin: SavedHeapAddress },
    ProviderLocalBirthCandidate { origin: SavedHeapAddress },
    ProviderLocalBirth,
    ParameterHeapRead { read: ParameterHeapRead },
}

fn snapshot_operation_support_address(
    address: HeapAddress,
    ordinals: &ContextOrdinals<'_>,
) -> Result<SavedHeapAddress> {
    ensure!(
        address.kind() == Some(crate::facts::abstract_domain::value::Kind::Object),
        "object-shape operation support has a non-object heap address"
    );
    SavedHeapAddress::snapshot(address, ordinals)
}

fn restore_operation_support_address(
    address: SavedHeapAddress,
    ordinals: &ContextOrdinals<'_>,
) -> Result<HeapAddress> {
    let address = address.restore(ordinals)?;
    ensure!(
        address.kind() == Some(crate::facts::abstract_domain::value::Kind::Object),
        "saved object-shape operation support has a non-object heap address"
    );
    Ok(address)
}

impl SavedOperationSupportReceipt {
    pub(crate) fn snapshot(
        receipt: &OperationSupportReceipt,
        ordinals: &ContextOrdinals<'_>,
    ) -> Result<Self> {
        let support = match &receipt.support {
            OperationSupportLocation::PooledObjectShape { origin } => {
                SavedOperationSupportLocation::PooledObjectShape {
                    origin: snapshot_operation_support_address(*origin, ordinals)?,
                }
            }
            OperationSupportLocation::ProviderWorldObjectShape { origin } => {
                SavedOperationSupportLocation::ProviderWorldObjectShape {
                    origin: snapshot_operation_support_address(*origin, ordinals)?,
                }
            }
            OperationSupportLocation::ProviderLocalBirthCandidate { origin } => {
                SavedOperationSupportLocation::ProviderLocalBirthCandidate {
                    origin: snapshot_operation_support_address(*origin, ordinals)?,
                }
            }
            OperationSupportLocation::ProviderLocalBirth => {
                SavedOperationSupportLocation::ProviderLocalBirth
            }
            OperationSupportLocation::ParameterHeapRead { read } => {
                SavedOperationSupportLocation::ParameterHeapRead { read: read.clone() }
            }
        };
        Ok(Self {
            operation: receipt.operation,
            support,
            dependencies: receipt.dependencies.clone(),
        })
    }

    pub(crate) fn restore(self, ordinals: &ContextOrdinals<'_>) -> Result<OperationSupportReceipt> {
        let support = match self.support {
            SavedOperationSupportLocation::PooledObjectShape { origin } => {
                OperationSupportLocation::PooledObjectShape {
                    origin: restore_operation_support_address(origin, ordinals)?,
                }
            }
            SavedOperationSupportLocation::ProviderWorldObjectShape { origin } => {
                OperationSupportLocation::ProviderWorldObjectShape {
                    origin: restore_operation_support_address(origin, ordinals)?,
                }
            }
            SavedOperationSupportLocation::ProviderLocalBirthCandidate { origin } => {
                OperationSupportLocation::ProviderLocalBirthCandidate {
                    origin: restore_operation_support_address(origin, ordinals)?,
                }
            }
            SavedOperationSupportLocation::ProviderLocalBirth => {
                OperationSupportLocation::ProviderLocalBirth
            }
            SavedOperationSupportLocation::ParameterHeapRead { read } => {
                OperationSupportLocation::ParameterHeapRead { read }
            }
        };
        Ok(OperationSupportReceipt {
            operation: self.operation,
            support,
            dependencies: self.dependencies,
        })
    }
}

impl<'a> SavedEffectCoverageFragment<'a> {
    pub(crate) fn snapshot(
        fragment: &'a EffectCoverageFragment,
        ordinals: &ContextOrdinals<'_>,
    ) -> Result<Self> {
        Ok(Self {
            initialization: Cow::Borrowed(&fragment.initialization),
            lexical: Cow::Borrowed(&fragment.lexical),
            call_observations: Cow::Borrowed(&fragment.call_observations),
            retained_losses: fragment.retained_losses.as_ref().map(|losses| {
                losses
                    .sites
                    .iter()
                    .map(SavedExecutionPrecisionLoss::snapshot)
                    .collect()
            }),
            operation_support: fragment
                .operation_support_receipts()
                .iter()
                .map(|receipt| SavedOperationSupportReceipt::snapshot(receipt, ordinals))
                .collect::<Result<_>>()?,
        })
    }

    /// Take ownership when source coordinates were first checked and rebased
    /// into a stable saved frame. This avoids borrowing a temporary mapped
    /// fragment from the caller.
    pub(crate) fn snapshot_owned(
        fragment: EffectCoverageFragment,
        ordinals: &ContextOrdinals<'_>,
    ) -> Result<SavedEffectCoverageFragment<'static>> {
        let operation_support = fragment
            .operation_support_receipts()
            .iter()
            .map(|receipt| SavedOperationSupportReceipt::snapshot(receipt, ordinals))
            .collect::<Result<_>>()?;
        let retained_losses = fragment.retained_losses.map(|losses| {
            losses
                .sites
                .into_iter()
                .map(SavedExecutionPrecisionLoss::snapshot_owned)
                .collect()
        });
        Ok(SavedEffectCoverageFragment {
            initialization: Cow::Owned(fragment.initialization),
            lexical: Cow::Owned(fragment.lexical),
            call_observations: Cow::Owned(fragment.call_observations),
            retained_losses,
            operation_support,
        })
    }

    pub(crate) fn restore(self, ordinals: &ContextOrdinals<'_>) -> Result<EffectCoverageFragment> {
        let operation_support = if self.operation_support.is_empty() {
            None
        } else {
            Some(
                self.operation_support
                    .into_iter()
                    .map(|receipt| receipt.restore(ordinals))
                    .collect::<Result<Vec<_>>>()?
                    .into_boxed_slice(),
            )
        };
        Ok(EffectCoverageFragment {
            initialization: self.initialization.into_owned(),
            lexical: self.lexical.into_owned(),
            call_observations: self.call_observations.into_owned(),
            retained_losses: self.retained_losses.map(|losses| {
                Box::new(RetainedLossCoverage {
                    sites: losses
                        .into_iter()
                        .map(SavedExecutionPrecisionLoss::restore)
                        .collect(),
                })
            }),
            operation_support,
        })
    }
}

/// The three existing initialization domains remain independent, but are
/// grouped behind one pointer so every recursive fragment does not carry
/// three inline proof-dependency payloads.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
struct InitializationEffectCoverage {
    global_properties: DomainCoverage,
    general_heap: DomainCoverage,
    host_effects: DomainCoverage,
}

/// Kept behind one pointer because `ProofDependencies` contains several
/// inline set headers. This data is absent on the conservative default path,
/// which is common in deep ordinary call execution.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
struct LexicalEffectCoverage {
    reads: DomainCoverage,
    rebinds: DomainCoverage,
}

/// Call-site observations outlive lexical effect authority: composing an
/// unknown effect fragment must not erase already observed source sites.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
struct CallObservationCoverage {
    #[serde(
        serialize_with = "serialize_calls",
        deserialize_with = "deserialize_calls"
    )]
    calls: BTreeMap<LocalCallKey, LocalCallReceipt>,
    #[serde(
        serialize_with = "serialize_skipped_calls",
        deserialize_with = "deserialize_skipped_calls"
    )]
    skipped_calls: BTreeMap<LocalSkippedCallKey, SkippedCallReceipt>,
    /// May-call obligations are serialized separately from exact receipts and
    /// survive composition without granting selected-invocation authority.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    possible_calls: Vec<PossibleLocalCallObligation>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    possible_skipped_calls: Vec<PossibleSkippedCallObligation>,
    /// Sites accounted for on every alternative represented by this joined
    /// fragment, whether invoked or skipped on a particular path.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    alternative_accounted_sites: BTreeSet<SourceOperationSite>,
}

fn call_observations_reference_source_sites(
    observations: &CallObservationCoverage,
    source_sites: &std::collections::BTreeSet<SourceOperationSite>,
) -> bool {
    observations
        .calls
        .values()
        .all(|call| call_receipt_references_source_sites(call, source_sites))
        && observations
            .skipped_calls
            .values()
            .all(|call| skipped_receipt_references_source_sites(call, source_sites))
        && observations
            .possible_calls
            .iter()
            .all(|call| source_sites.contains(&call.site))
        && observations
            .possible_skipped_calls
            .iter()
            .all(|call| possible_skipped_call_references_source_sites(call, source_sites))
        && observations
            .alternative_accounted_sites
            .iter()
            .all(|site| source_sites.contains(site))
}

fn possible_skipped_call_references_source_sites(
    call: &PossibleSkippedCallObligation,
    source_sites: &std::collections::BTreeSet<SourceOperationSite>,
) -> bool {
    call.site.step == OperationStep::CallInvocation
        && call.site.body == call.caller
        && call.region_site.body == call.caller
        && call.frontier_site.body == call.caller
        && call.site.owner == call.region_site.owner
        && call.site.owner == call.frontier_site.owner
        && skipped_call_site_is_within_region(&call.site, &call.region_site)
        && call.caller.source_owner() == Some(call.site.owner)
        && skipped_reason_matches_sites(call.reason, &call.region_site, &call.frontier_site)
        && source_sites.contains(&call.site)
        && source_sites.contains(&call.region_site)
        && source_sites.contains(&call.frontier_site)
}

fn source_site_provider_callable_is_current(
    site: &SourceOperationSite,
    current: &mut impl FnMut(&str, &str, u32) -> bool,
) -> bool {
    site.body.matches_current_provider_callable(current)
}

fn call_observations_reference_current_provider_callables(
    observations: &CallObservationCoverage,
    current: &mut impl FnMut(&str, &str, u32) -> bool,
) -> bool {
    observations.calls.values().all(|call| {
        call.caller.matches_current_provider_callable(current)
            && call.target.matches_current_provider_callable(current)
            && source_site_provider_callable_is_current(&call.site, current)
    }) && observations.skipped_calls.values().all(|call| {
        call.caller.matches_current_provider_callable(current)
            && source_site_provider_callable_is_current(&call.site, current)
            && source_site_provider_callable_is_current(&call.region_site, current)
            && source_site_provider_callable_is_current(&call.frontier_site, current)
    }) && observations.possible_calls.iter().all(|call| {
        source_site_provider_callable_is_current(&call.site, current)
            && call
                .possible_targets
                .iter()
                .all(|target| target.matches_current_provider_callable(current))
    }) && observations.possible_skipped_calls.iter().all(|call| {
        call.caller.matches_current_provider_callable(current)
            && skipped_reason_matches_sites(call.reason, &call.region_site, &call.frontier_site)
            && source_site_provider_callable_is_current(&call.site, current)
            && source_site_provider_callable_is_current(&call.region_site, current)
            && source_site_provider_callable_is_current(&call.frontier_site, current)
    }) && observations
        .alternative_accounted_sites
        .iter()
        .all(|site| source_site_provider_callable_is_current(site, current))
}

fn serialize_calls<S: serde::Serializer>(
    calls: &BTreeMap<LocalCallKey, LocalCallReceipt>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    calls.values().collect::<Vec<_>>().serialize(serializer)
}

fn deserialize_calls<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<BTreeMap<LocalCallKey, LocalCallReceipt>, D::Error> {
    let mut calls = BTreeMap::<LocalCallKey, LocalCallReceipt>::new();
    for call in Vec::<LocalCallReceipt>::deserialize(deserializer)? {
        calls
            .entry(local_call_key(&call))
            .and_modify(|held| held.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }
    Ok(calls)
}

fn serialize_skipped_calls<S: serde::Serializer>(
    calls: &BTreeMap<LocalSkippedCallKey, SkippedCallReceipt>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    calls.values().collect::<Vec<_>>().serialize(serializer)
}

fn deserialize_skipped_calls<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<BTreeMap<LocalSkippedCallKey, SkippedCallReceipt>, D::Error> {
    let mut calls = BTreeMap::<LocalSkippedCallKey, SkippedCallReceipt>::new();
    for call in Vec::<SkippedCallReceipt>::deserialize(deserializer)? {
        calls
            .entry(local_skipped_call_key(&call))
            .and_modify(|held| held.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }
    Ok(calls)
}

fn accounted_call_sites(fragment: &EffectCoverageFragment) -> BTreeSet<SourceOperationSite> {
    let Some(observations) = fragment.call_observations.as_deref() else {
        return BTreeSet::new();
    };
    observations
        .alternative_accounted_sites
        .iter()
        .cloned()
        .chain(observations.calls.values().map(|call| call.site.clone()))
        .chain(
            observations
                .skipped_calls
                .values()
                .map(|call| call.site.clone()),
        )
        .collect()
}

fn merge_possible_call(
    observations: &mut CallObservationCoverage,
    obligation: PossibleLocalCallObligation,
) {
    if let Some(existing) = observations
        .possible_calls
        .iter_mut()
        .find(|existing| existing.site == obligation.site)
    {
        existing.merge(&obligation);
    } else {
        observations.possible_calls.push(obligation);
    }
}

fn merge_possible_skipped_call(
    observations: &mut CallObservationCoverage,
    obligation: PossibleSkippedCallObligation,
) {
    if let Some(existing) = observations
        .possible_skipped_calls
        .iter_mut()
        .find(|existing| existing.same_key(&obligation))
    {
        existing.merge(&obligation);
    } else {
        observations.possible_skipped_calls.push(obligation);
    }
}

impl EffectCoverageFragment {
    /// Rewrite every source-coordinate-bearing member of an execution fragment
    /// through one checked coordinate frame. Saved provider receipts use this
    /// to encode offsets relative to their source digest and to restore them
    /// against the current canonical inventory.
    pub(crate) fn map_source_coordinates(
        &mut self,
        mut map_body: impl FnMut(&LocalBodyIdentity) -> Result<LocalBodyIdentity>,
        mut map_site: impl FnMut(&SourceOperationSite) -> Result<SourceOperationSite>,
        mut map_heap: impl FnMut(HeapAddress) -> Result<HeapAddress>,
        mut map_span: impl FnMut(Span) -> Result<Span>,
    ) -> Result<()> {
        if let Some(observations) = self.call_observations.as_deref_mut() {
            let mut calls = BTreeMap::new();
            for (_, mut call) in std::mem::take(&mut observations.calls) {
                call.caller = map_body(&call.caller)?;
                call.site = map_site(&call.site)?;
                call.target = map_body(&call.target)?;
                ensure!(
                    calls.insert(local_call_key(&call), call).is_none(),
                    "source-coordinate rebinding merged distinct provider call receipts"
                );
            }
            observations.calls = calls;

            let mut skipped_calls = BTreeMap::new();
            for (_, mut call) in std::mem::take(&mut observations.skipped_calls) {
                call.caller = map_body(&call.caller)?;
                call.site = map_site(&call.site)?;
                call.region_site = map_site(&call.region_site)?;
                call.frontier_site = map_site(&call.frontier_site)?;
                ensure!(
                    skipped_calls
                        .insert(local_skipped_call_key(&call), call)
                        .is_none(),
                    "source-coordinate rebinding merged distinct skipped-call receipts"
                );
            }
            observations.skipped_calls = skipped_calls;

            for call in &mut observations.possible_calls {
                call.site = map_site(&call.site)?;
                let mut targets = Vec::with_capacity(call.possible_targets.len());
                for target in &call.possible_targets {
                    let target = map_body(target)?;
                    ensure!(
                        !targets.contains(&target),
                        "source-coordinate rebinding merged possible call targets"
                    );
                    targets.push(target);
                }
                call.possible_targets = targets;
            }

            for call in &mut observations.possible_skipped_calls {
                call.caller = map_body(&call.caller)?;
                call.site = map_site(&call.site)?;
                call.region_site = map_site(&call.region_site)?;
                call.frontier_site = map_site(&call.frontier_site)?;
            }

            let mut accounted = BTreeSet::new();
            for site in &observations.alternative_accounted_sites {
                ensure!(
                    accounted.insert(map_site(site)?),
                    "source-coordinate rebinding merged accounted operation sites"
                );
            }
            observations.alternative_accounted_sites = accounted;
        }

        if let Some(losses) = self.retained_losses.as_deref_mut() {
            for loss in &mut losses.sites {
                loss.span = map_span(loss.span)?;
            }
        }

        if let Some(receipts) = self.operation_support.as_deref_mut() {
            for receipt in receipts {
                match &mut receipt.support {
                    OperationSupportLocation::PooledObjectShape { origin }
                    | OperationSupportLocation::ProviderWorldObjectShape { origin }
                    | OperationSupportLocation::ProviderLocalBirthCandidate { origin } => {
                        *origin = map_heap(*origin)?;
                    }
                    OperationSupportLocation::ProviderLocalBirth
                    | OperationSupportLocation::ParameterHeapRead { .. } => {}
                }
            }
        }

        // Keep this exhaustive check close to the mapper: every supported
        // receipt family must either participate above or have no source data.
        if self
            .call_observations
            .as_deref()
            .is_some_and(|observations| {
                observations
                    .calls
                    .values()
                    .any(|call| call.site.body != call.caller)
            })
        {
            bail!("rebased provider call site no longer names its caller body");
        }
        Ok(())
    }

    /// Join effect facts from feasible alternative paths. Covered domains
    /// survive only when every path covers that domain; exact call receipts
    /// survive only when the same edge is present on every path. Branch-only
    /// calls are handled by the transfer-alternative ledger, never promoted
    /// to this definite receipt map.
    pub(super) fn join_alternatives(fragments: &[&Self]) -> Option<Self> {
        let first = *fragments.first()?;

        let join_domain = |domains: Vec<&DomainCoverage>| {
            let mut dependencies = ProofDependencies::default();
            for domain in domains {
                let DomainCoverage::Covered(known) = domain else {
                    return DomainCoverage::Unknown;
                };
                dependencies.extend(known.as_ref());
            }
            DomainCoverage::Covered(Box::new(dependencies))
        };

        let initialization = if fragments
            .iter()
            .all(|fragment| fragment.initialization.is_some())
        {
            Some(Box::new(InitializationEffectCoverage {
                global_properties: join_domain(
                    fragments
                        .iter()
                        .map(|fragment| {
                            &fragment.initialization.as_ref().unwrap().global_properties
                        })
                        .collect(),
                ),
                general_heap: join_domain(
                    fragments
                        .iter()
                        .map(|fragment| &fragment.initialization.as_ref().unwrap().general_heap)
                        .collect(),
                ),
                host_effects: join_domain(
                    fragments
                        .iter()
                        .map(|fragment| &fragment.initialization.as_ref().unwrap().host_effects)
                        .collect(),
                ),
            }))
        } else {
            None
        };
        let lexical = if fragments.iter().all(|fragment| fragment.lexical.is_some()) {
            Some(Box::new(LexicalEffectCoverage {
                reads: join_domain(
                    fragments
                        .iter()
                        .map(|fragment| &fragment.lexical.as_ref().unwrap().reads)
                        .collect(),
                ),
                rebinds: join_domain(
                    fragments
                        .iter()
                        .map(|fragment| &fragment.lexical.as_ref().unwrap().rebinds)
                        .collect(),
                ),
            }))
        } else {
            None
        };

        let mut common_observations = first
            .call_observations
            .as_deref()
            .cloned()
            .unwrap_or_default();
        let mut accounted_on_every_path = fragments
            .iter()
            .map(|fragment| accounted_call_sites(fragment))
            .collect::<Vec<_>>();
        let mut common_accounted_sites = accounted_on_every_path.first().cloned()?;
        for accounted in accounted_on_every_path.drain(1..) {
            common_accounted_sites.retain(|site| accounted.contains(site));
        }

        for fragment in &fragments[1..] {
            if let Some(observations) = &fragment.call_observations {
                common_observations.calls.retain(|key, call| {
                    let Some(other) = observations.calls.get(key) else {
                        return false;
                    };
                    call.dependencies.extend(other.dependencies.as_ref());
                    true
                });
                common_observations.skipped_calls.retain(|key, call| {
                    let Some(other) = observations.skipped_calls.get(key) else {
                        return false;
                    };
                    call.dependencies.extend(other.dependencies.as_ref());
                    true
                });
            } else {
                common_observations.calls.clear();
                common_observations.skipped_calls.clear();
            }
        }
        common_observations.alternative_accounted_sites = common_accounted_sites;

        let definite_calls = common_observations.calls.clone();
        let definite_skips = common_observations.skipped_calls.clone();
        common_observations.possible_calls.clear();
        common_observations.possible_skipped_calls.clear();
        for fragment in fragments {
            for possible in fragment.possible_local_calls() {
                if let Some(existing) = common_observations
                    .possible_calls
                    .iter_mut()
                    .find(|existing| existing.site == possible.site)
                {
                    existing.merge(possible);
                } else {
                    common_observations.possible_calls.push(possible.clone());
                }
            }
            for possible in fragment.possible_skipped_local_calls() {
                if let Some(existing) = common_observations
                    .possible_skipped_calls
                    .iter_mut()
                    .find(|existing| existing.same_key(possible))
                {
                    existing.merge(possible);
                } else {
                    common_observations
                        .possible_skipped_calls
                        .push(possible.clone());
                }
            }

            if let Some(observations) = fragment.call_observations.as_deref() {
                for call in observations.calls.values() {
                    if !definite_calls.contains_key(&local_call_key(call)) {
                        let obligation = PossibleLocalCallObligation::new(
                            call.site.clone(),
                            Some(call.target.clone()),
                            call.dependencies.as_ref(),
                        );
                        merge_possible_call(&mut common_observations, obligation);
                    }
                }
                for skipped in observations.skipped_calls.values() {
                    if !definite_skips.contains_key(&local_skipped_call_key(skipped)) {
                        let obligation = PossibleSkippedCallObligation::from_receipt(skipped);
                        merge_possible_skipped_call(&mut common_observations, obligation);
                    }
                }
            }
        }
        let call_observations = (fragments
            .iter()
            .any(|fragment| fragment.call_observations.is_some()))
        .then(|| Box::new(common_observations));

        let mut joined = Self {
            initialization,
            lexical,
            call_observations,
            retained_losses: None,
            operation_support: None,
        };
        for fragment in fragments {
            if let Some(losses) = &fragment.retained_losses {
                for loss in &losses.sites {
                    joined.retain_loss(loss.clone());
                }
            }
            for support in fragment.operation_support_receipts() {
                joined.operation_support(support.clone());
            }
        }
        Some(joined)
    }

    /// Whether all execution-site receipts are members of an independently
    /// prepared source table. An empty observation ledger asserts nothing.
    pub(crate) fn references_source_sites(
        &self,
        source_sites: &std::collections::BTreeSet<SourceOperationSite>,
    ) -> bool {
        self.call_observations
            .as_deref()
            .is_none_or(|observations| {
                call_observations_reference_source_sites(observations, source_sites)
            })
    }

    /// Resolve every serialized provider body identity against the current
    /// provider registry. Caller source sites are checked separately against
    /// the prepared caller table; this check never inspects caller SWC nodes.
    pub(crate) fn provider_callables_are_current(
        &self,
        current: &mut impl FnMut(&str, &str, u32) -> bool,
    ) -> bool {
        self.call_observations
            .as_deref()
            .is_none_or(|observations| {
                call_observations_reference_current_provider_callables(observations, current)
            })
    }

    /// Discharge a completed body's source observations against its own current
    /// program before carrying its concrete effects into another program.
    pub(super) fn project_checked_body(
        &self,
        body: &LocalBodyIdentity,
        expected_calls: &std::collections::BTreeSet<SourceOperationSite>,
        source_sites: &std::collections::BTreeSet<SourceOperationSite>,
    ) -> Option<Self> {
        let dependencies = self.closed_domain_dependencies()?;
        if !self.references_source_sites(source_sites)
            || !self.has_call_inventory(body, expected_calls)
            || self.has_uninstantiated_operation_support()
            || self.retained_losses.is_some()
        {
            return None;
        }
        let mut projected = self.clone();
        projected.call_observations = None;
        projected.depend_on(&dependencies);
        Some(projected)
    }

    /// No unaccounted effect in these domains. Concrete cell effects, such as
    /// an initialized declarative read, belong in the caller's ledger.
    /// Source identity and actual binding effects remain separate evidence.
    pub(super) fn no_effects() -> Self {
        Self {
            initialization: Some(Box::new(InitializationEffectCoverage {
                global_properties: DomainCoverage::Covered(Box::default()),
                general_heap: DomainCoverage::Covered(Box::default()),
                host_effects: DomainCoverage::Covered(Box::default()),
            })),
            lexical: Some(Box::new(LexicalEffectCoverage {
                reads: DomainCoverage::Covered(Box::default()),
                rebinds: DomainCoverage::Covered(Box::default()),
            })),
            call_observations: None,
            retained_losses: None,
            operation_support: None,
        }
    }

    /// Complete initialization-domain coverage for a checked native body with
    /// no application callback or accessor. Concrete shared-cell effects are
    /// recorded by the runtime transfer. Callee and operand evaluation remain
    /// with the caller; this receipt grants no lexical binding-effect
    /// authority.
    pub(super) fn native_operation(dependencies: &ProofDependencies) -> Self {
        Self {
            initialization: Some(Box::new(InitializationEffectCoverage {
                global_properties: DomainCoverage::Covered(Box::new(dependencies.clone())),
                general_heap: DomainCoverage::Covered(Box::new(dependencies.clone())),
                host_effects: DomainCoverage::Covered(Box::new(dependencies.clone())),
            })),
            // Native operands are outside the bounded lexical migration.
            lexical: None,
            call_observations: None,
            retained_losses: None,
            operation_support: None,
        }
    }

    /// Add the audited nonlexical domains of one selected initializer call.
    /// This author input does not change completion or lexical coverage.
    pub(super) fn audited_initializer_call_domains(
        mut self,
        dependencies: &ProofDependencies,
    ) -> Self {
        let merged = |prior: Option<&DomainCoverage>| {
            let mut covered = dependencies.clone();
            if let Some(DomainCoverage::Covered(prior)) = prior {
                covered.extend(prior.as_ref());
            }
            DomainCoverage::Covered(Box::new(covered))
        };
        let prior = self.initialization.as_deref();
        self.initialization = Some(Box::new(InitializationEffectCoverage {
            global_properties: merged(prior.map(|coverage| &coverage.global_properties)),
            general_heap: merged(prior.map(|coverage| &coverage.general_heap)),
            host_effects: merged(prior.map(|coverage| &coverage.host_effects)),
        }));
        self
    }

    /// Actual ordinary native iteration over a proved primitive source.
    /// Its protocol/contents reads and fresh internal allocation are modeled;
    /// this is body coverage, not completion of operand evaluation.
    pub(super) fn native_iterable_protocol(dependencies: &ProofDependencies) -> Self {
        Self::native_operation(dependencies)
    }

    /// Ordered local execution also owns the operand reads and exact lexical
    /// call edge. Preserve that coverage while certifying the native protocol
    /// body with the same dependencies as the ordinary iterable receipt.
    pub(super) fn ordered_native_iterable_protocol(dependencies: &ProofDependencies) -> Self {
        let mut fragment = Self::no_effects();
        fragment.depend_on(dependencies);
        fragment
    }

    /// A top-level module-cell read has no global/heap/host effect, and its
    /// lexical read/rebind behavior is accounted for by the caller.
    pub(super) fn initialized_module_read() -> Self {
        let mut fragment = Self::no_effects();
        fragment
            .lexical
            .as_mut()
            .expect("no-effects fragment carries known lexical domains")
            .reads = DomainCoverage::Covered(Box::default());
        fragment
    }

    /// A checked present own-data access has no implicit accessor, prototype,
    /// global or host operation. Its actual property cell must be recorded in
    /// the heap ledger separately; receiver/RHS evaluation composes separately.
    /// Writes additionally require the current-prefix writable witness.
    pub(super) fn modeled_own_data_access() -> Self {
        Self::no_effects()
    }

    pub(super) fn operation_support(&mut self, receipt: OperationSupportReceipt) {
        if self
            .operation_support
            .as_deref()
            .is_some_and(|receipts| receipts.contains(&receipt))
        {
            return;
        }
        let mut receipts = self
            .operation_support
            .take()
            .map(|receipts| receipts.into_vec())
            .unwrap_or_default();
        receipts.push(receipt);
        self.operation_support = Some(receipts.into_boxed_slice());
    }

    pub(crate) fn operation_support_receipts(&self) -> &[OperationSupportReceipt] {
        self.operation_support.as_deref().unwrap_or(&[])
    }

    pub(crate) fn has_uninstantiated_operation_support(&self) -> bool {
        self.operation_support_receipts().iter().any(|receipt| {
            matches!(
                &receipt.support,
                OperationSupportLocation::ProviderWorldObjectShape { .. }
                    | OperationSupportLocation::ProviderLocalBirthCandidate { .. }
                    | OperationSupportLocation::ParameterHeapRead { .. }
            )
        })
    }

    pub(crate) fn has_unresolved_provider_operation_support(&self) -> bool {
        self.operation_support_receipts().iter().any(|receipt| {
            matches!(
                &receipt.support,
                OperationSupportLocation::ProviderWorldObjectShape { .. }
                    | OperationSupportLocation::ProviderLocalBirthCandidate { .. }
            )
        })
    }

    /// Seal only per-access candidates from a complete normal transfer. The
    /// caller additionally checks that this was the admitted straight-line
    /// LocalBody and that allocation transfer itself completed.
    pub(super) fn seal_provider_local_birth_support_domains(
        &mut self,
        fresh_records: &std::collections::BTreeSet<HeapAddress>,
        borrowed_records: &std::collections::BTreeSet<HeapAddress>,
    ) -> bool {
        let mut receipts = self.operation_support_receipts().to_vec();
        for receipt in &mut receipts {
            match &receipt.support {
                OperationSupportLocation::ProviderLocalBirthCandidate { origin } => {
                    let origin = *origin;
                    if !fresh_records.contains(&origin) || borrowed_records.contains(&origin) {
                        return false;
                    }
                    receipt.support = OperationSupportLocation::ProviderLocalBirth;
                }
                OperationSupportLocation::ParameterHeapRead { .. } => {}
                _ => return false,
            }
        }
        self.operation_support = (!receipts.is_empty()).then(|| receipts.into_boxed_slice());
        true
    }

    /// Bind provider-parameter support to the caller's actual pooled object
    /// regions. Any unsupported or unavailable correspondence refuses the
    /// entire binding rather than comparing provider ordinals to caller ones.
    pub(crate) fn instantiate_operation_support(&mut self, arguments: &[HeapAddress]) -> bool {
        let mut instantiated = self.operation_support_receipts().to_vec();
        if instantiated
            .iter_mut()
            .any(|receipt| !receipt.instantiate_parameter(arguments))
        {
            return false;
        }
        self.operation_support =
            (!instantiated.is_empty()).then(|| instantiated.into_boxed_slice());
        true
    }

    /// Proof prerequisites for a successfully consumed own-data selection.
    /// Unknown lexical coverage never becomes positive through dependencies.
    pub(super) fn lexical_selection_domain_dependencies(&self) -> Option<Box<ProofDependencies>> {
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(reads) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebinds) = &lexical.rebinds else {
            return None;
        };
        let mut dependencies = Box::<ProofDependencies>::default();
        dependencies.extend(reads.as_ref());
        dependencies.extend(rebinds.as_ref());
        Some(dependencies)
    }

    pub(super) fn compose(&mut self, next: &Self) {
        if let Some(losses) = &next.retained_losses {
            for loss in &losses.sites {
                self.retain_loss(loss.clone());
            }
        }
        for receipt in next.operation_support_receipts() {
            self.operation_support(receipt.clone());
        }
        match (&mut self.initialization, &next.initialization) {
            (Some(current), Some(next)) => {
                current.global_properties.compose(&next.global_properties);
                current.general_heap.compose(&next.general_heap);
                current.host_effects.compose(&next.host_effects);
            }
            _ => self.initialization = None,
        }
        match (&mut self.lexical, &next.lexical) {
            (Some(current), Some(next)) => {
                current.reads.compose(&next.reads);
                current.rebinds.compose(&next.rebinds);
            }
            _ => self.lexical = None,
        }
        if let Some(next_observations) = &next.call_observations {
            let current = self
                .call_observations
                .get_or_insert_with(|| Box::new(CallObservationCoverage::default()));
            for (key, next_call) in &next_observations.calls {
                current
                    .calls
                    .entry(key.clone())
                    .and_modify(|call| call.dependencies.extend(&next_call.dependencies))
                    .or_insert_with(|| next_call.clone());
            }
            for (key, next_call) in &next_observations.skipped_calls {
                current
                    .skipped_calls
                    .entry(key.clone())
                    .and_modify(|call| call.dependencies.extend(&next_call.dependencies))
                    .or_insert_with(|| next_call.clone());
            }
            for next_call in &next_observations.possible_calls {
                if let Some(call) = current
                    .possible_calls
                    .iter_mut()
                    .find(|call| call.site == next_call.site)
                {
                    call.merge(next_call);
                } else {
                    current.possible_calls.push(next_call.clone());
                }
            }
            for next_call in &next_observations.possible_skipped_calls {
                merge_possible_skipped_call(current, next_call.clone());
            }
            current.alternative_accounted_sites.extend(
                next_observations
                    .alternative_accounted_sites
                    .iter()
                    .cloned(),
            );
        }
    }

    /// Prerequisites of selecting an already covered operation. Conditions
    /// never turn an unknown domain into a covered one.
    pub(super) fn depend_on(&mut self, dependencies: &ProofDependencies) {
        if let Some(initialization) = &mut self.initialization {
            for domain in [
                &mut initialization.global_properties,
                &mut initialization.general_heap,
                &mut initialization.host_effects,
            ] {
                if let DomainCoverage::Covered(current) = domain {
                    current.as_mut().extend(dependencies);
                }
            }
        }
        if let Some(lexical) = &mut self.lexical {
            for domain in [&mut lexical.reads, &mut lexical.rebinds] {
                if let DomainCoverage::Covered(current) = domain {
                    current.as_mut().extend(dependencies);
                }
            }
        }
        if let Some(observations) = &mut self.call_observations {
            for call in observations.calls.values_mut() {
                call.dependencies.extend(dependencies);
            }
            for call in observations.skipped_calls.values_mut() {
                call.dependencies.extend(dependencies);
            }
            for call in &mut observations.possible_calls {
                call.dependencies.extend(dependencies);
            }
            for call in &mut observations.possible_skipped_calls {
                call.dependencies.extend(dependencies);
            }
        }
        for receipt in self.operation_support.as_deref_mut().into_iter().flatten() {
            receipt.dependencies.extend(dependencies);
        }
    }

    /// Add one selected call edge without expanding child paths.
    pub(super) fn selected_local_call(&mut self, call: LocalCallReceipt) {
        let observations = self
            .call_observations
            .get_or_insert_with(|| Box::new(CallObservationCoverage::default()));
        observations
            .calls
            .entry(local_call_key(&call))
            .and_modify(|existing| existing.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }

    pub(super) fn skipped_local_call(&mut self, call: SkippedCallReceipt) {
        let observations = self
            .call_observations
            .get_or_insert_with(|| Box::new(CallObservationCoverage::default()));
        observations
            .skipped_calls
            .entry(local_skipped_call_key(&call))
            .and_modify(|existing| existing.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }

    /// May-call obligations survive alternative projection and parent
    /// composition, but do not satisfy the exact source call inventory.
    pub(super) fn possible_local_calls(
        &self,
    ) -> impl Iterator<Item = &PossibleLocalCallObligation> {
        self.call_observations
            .iter()
            .flat_map(|observations| observations.possible_calls.iter())
    }

    pub(super) fn possible_skipped_local_calls(
        &self,
    ) -> impl Iterator<Item = &PossibleSkippedCallObligation> {
        self.call_observations
            .iter()
            .flat_map(|observations| observations.possible_skipped_calls.iter())
    }

    /// Whether this exact source site has either invocation or skip evidence.
    pub(super) fn has_local_call(
        &self,
        caller: &LocalBodyIdentity,
        site: &SourceOperationSite,
    ) -> bool {
        let Some(observations) = &self.call_observations else {
            return false;
        };
        observations
            .calls
            .values()
            .any(|call| call.caller == *caller && call.site == *site)
            || observations
                .skipped_calls
                .values()
                .any(|call| call.caller == *caller && call.site == *site)
    }

    /// Exact union of invoked and skipped sites for one source body.
    pub(super) fn has_call_inventory(
        &self,
        caller: &LocalBodyIdentity,
        expected: &std::collections::BTreeSet<SourceOperationSite>,
    ) -> bool {
        let Some(observations) = &self.call_observations else {
            return expected.is_empty();
        };
        let observed: std::collections::BTreeSet<_> = observations
            .calls
            .values()
            .filter(|call| call.caller == *caller)
            .map(|call| call.site.clone())
            .chain(
                observations
                    .skipped_calls
                    .values()
                    .filter(|call| call.caller == *caller)
                    .map(|call| call.site.clone()),
            )
            .chain(
                observations
                    .alternative_accounted_sites
                    .iter()
                    .filter(|site| site.body == *caller)
                    .cloned(),
            )
            .collect();
        &observed == expected
    }

    pub(super) fn lexical_domain_dependencies(
        &self,
    ) -> Option<(ProofDependencies, ProofDependencies)> {
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(read_dependencies) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebind_dependencies) = &lexical.rebinds else {
            return None;
        };
        Some((
            read_dependencies.as_ref().clone(),
            rebind_dependencies.as_ref().clone(),
        ))
    }

    pub(super) fn closed_domain_dependencies(&self) -> Option<ProofDependencies> {
        let mut dependencies = self.initialization_domain_dependencies()?;
        let (reads, rebinds) = self.lexical_domain_dependencies()?;
        dependencies.extend(&reads);
        dependencies.extend(&rebinds);
        if let Some(observations) = &self.call_observations {
            for call in observations.calls.values() {
                dependencies.extend(&call.dependencies);
            }
            for call in observations.skipped_calls.values() {
                dependencies.extend(&call.dependencies);
            }
            for call in &observations.possible_calls {
                dependencies.extend(call.dependencies.as_ref());
            }
            for call in &observations.possible_skipped_calls {
                dependencies.extend(call.dependencies.as_ref());
            }
        }
        Some(dependencies)
    }

    pub(super) fn has_closed_domains(&self) -> bool {
        let (Some(initialization), Some(lexical)) = (&self.initialization, &self.lexical) else {
            return false;
        };
        [
            &initialization.global_properties,
            &initialization.general_heap,
            &initialization.host_effects,
            &lexical.reads,
            &lexical.rebinds,
        ]
        .into_iter()
        .all(|domain| matches!(domain, DomainCoverage::Covered(_)))
    }

    pub(super) fn retain_loss(&mut self, loss: ExecutionPrecisionLoss) {
        let losses = self.retained_losses.get_or_insert_with(Box::default);
        if !losses.sites.iter().any(|held| same_loss(held, &loss)) {
            losses.sites.push(loss);
        }
    }

    pub(super) fn covers_retained_loss_domains(&self, losses: &[ExecutionPrecisionLoss]) -> bool {
        !losses.is_empty()
            && self.has_closed_domains()
            && self.retained_losses.as_ref().is_some_and(|covered| {
                losses
                    .iter()
                    .all(|loss| covered.sites.iter().any(|held| same_loss(held, loss)))
            })
    }

    pub(super) fn seal_lexical_binding_effect_domains(
        &self,
        identity: RootEffectIdentity,
        expected_root_calls: &std::collections::BTreeSet<SourceOperationSite>,
    ) -> Option<LexicalBindingEffectReceipt> {
        let (read_dependencies, rebind_dependencies) = self.lexical_domain_dependencies()?;
        let root_body = identity.body_identity();
        if !self.has_call_inventory(&root_body, expected_root_calls) {
            return None;
        }
        let observations = self.call_observations.as_deref();

        // A receipt can contain only edges reached while executing this root.
        // Check the flattened caller/target relation iteratively, without
        // reconstructing exponentially many invocation paths or repeatedly
        // scanning every edge for each newly reached body.
        let mut adjacency = std::collections::BTreeMap::<
            LocalBodyKey,
            std::collections::BTreeSet<LocalBodyKey>,
        >::new();
        for call in observations
            .into_iter()
            .flat_map(|calls| calls.calls.values())
        {
            adjacency
                .entry(local_body_key(&call.caller))
                .or_default()
                .insert(local_body_key(&call.target));
        }
        let root_key = local_body_key(&root_body);
        let mut reachable_bodies = std::collections::BTreeSet::from([root_key.clone()]);
        let mut pending = vec![root_key];
        while let Some(body) = pending.pop() {
            if let Some(targets) = adjacency.get(&body) {
                for target in targets {
                    if reachable_bodies.insert(target.clone()) {
                        pending.push(target.clone());
                    }
                }
            }
        }
        if observations.into_iter().any(|observations| {
            observations
                .calls
                .values()
                .any(|call| !reachable_bodies.contains(&local_body_key(&call.caller)))
                || observations
                    .skipped_calls
                    .values()
                    .any(|call| !reachable_bodies.contains(&local_body_key(&call.caller)))
        }) {
            return None;
        }
        Some(LexicalBindingEffectReceipt {
            identity,
            read_dependencies,
            rebind_dependencies,
            calls: observations
                .into_iter()
                .flat_map(|observations| observations.calls.values().cloned())
                .collect(),
            skipped_calls: observations
                .into_iter()
                .flat_map(|observations| observations.skipped_calls.values().cloned())
                .collect(),
        })
    }

    pub(super) fn initialization_domain_dependencies(&self) -> Option<ProofDependencies> {
        let initialization = self.initialization.as_ref()?;
        let mut dependencies = ProofDependencies::default();
        for domain in [
            &initialization.global_properties,
            &initialization.general_heap,
            &initialization.host_effects,
        ] {
            let DomainCoverage::Covered(domain_dependencies) = domain else {
                return None;
            };
            dependencies.extend(domain_dependencies.as_ref());
        }
        Some(dependencies)
    }
}

fn same_loss(first: &ExecutionPrecisionLoss, later: &ExecutionPrecisionLoss) -> bool {
    first.statement_ordinal == later.statement_ordinal
        && first.span == later.span
        && first.reason == later.reason
        && first.reach == later.reach
}

/// Compact ordered identity for the edge map and iterative connectivity check.
/// Spans are represented by their exact byte coordinates because SWC `Span`
/// itself does not implement `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
enum LocalBodyKey {
    RootInitializer {
        owner: usize,
        lo: u32,
        hi: u32,
    },
    RootClassDeclaration {
        owner: usize,
        lo: u32,
        hi: u32,
    },
    RootExpression {
        owner: usize,
        lo: u32,
        hi: u32,
    },
    Callable {
        key: u32,
        owner: usize,
    },
    Construction {
        class: u32,
        owner: usize,
    },
    ImportedCallable(Box<ImportedCallableIdentity>),
    ProviderCallable {
        defining_module: String,
        source_sha256: String,
        source_origin: u32,
    },

    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
    },
    NativeConstruction {
        intrinsic: NativeIntrinsic,
    },
}

/// Exact identity for a flat local call edge. Span coordinates are used
/// because SWC `Span` itself does not implement `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
struct LocalCallKey {
    caller: LocalBodyKey,
    site: SourceOperationSite,
    target: LocalBodyKey,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
struct LocalSkippedCallKey {
    caller: LocalBodyKey,
    site: SourceOperationSite,
    region_site: SourceOperationSite,
    frontier_site: SourceOperationSite,
    reason: SkippedCallReason,
}

fn local_body_key(body: &LocalBodyIdentity) -> LocalBodyKey {
    match body {
        LocalBodyIdentity::RootInitializer {
            owner,
            initializer_span,
        } => LocalBodyKey::RootInitializer {
            owner: owner.0,
            lo: initializer_span.lo.0,
            hi: initializer_span.hi.0,
        },
        LocalBodyIdentity::RootClassDeclaration {
            owner,
            declaration_span,
        } => LocalBodyKey::RootClassDeclaration {
            owner: owner.0,
            lo: declaration_span.lo.0,
            hi: declaration_span.hi.0,
        },
        LocalBodyIdentity::RootExpression {
            owner,
            expression_span,
        } => LocalBodyKey::RootExpression {
            owner: owner.0,
            lo: expression_span.lo.0,
            hi: expression_span.hi.0,
        },
        LocalBodyIdentity::Callable { key, owner } => LocalBodyKey::Callable {
            key: *key,
            owner: owner.0,
        },
        LocalBodyIdentity::Construction { class, owner } => LocalBodyKey::Construction {
            class: *class,
            owner: owner.0,
        },
        LocalBodyIdentity::ImportedCallable(identity) => {
            LocalBodyKey::ImportedCallable(identity.clone())
        }
        LocalBodyIdentity::ProviderCallable {
            defining_module,
            source_sha256,
            source_origin,
        } => LocalBodyKey::ProviderCallable {
            defining_module: defining_module.clone(),
            source_sha256: source_sha256.clone(),
            source_origin: *source_origin,
        },
        LocalBodyIdentity::Native {
            intrinsic,
            property,
        } => LocalBodyKey::Native {
            intrinsic: *intrinsic,
            property: property.clone(),
        },
        LocalBodyIdentity::NativeConstruction { intrinsic } => LocalBodyKey::NativeConstruction {
            intrinsic: *intrinsic,
        },
    }
}

fn local_call_key(call: &LocalCallReceipt) -> LocalCallKey {
    LocalCallKey {
        caller: local_body_key(&call.caller),
        site: call.site.clone(),
        target: local_body_key(&call.target),
    }
}

fn local_skipped_call_key(call: &SkippedCallReceipt) -> LocalSkippedCallKey {
    LocalSkippedCallKey {
        caller: local_body_key(&call.caller),
        site: call.site.clone(),
        region_site: call.region_site.clone(),
        frontier_site: call.frontier_site.clone(),
        reason: call.reason,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{ProgramPremiseKey, ValueInputRef};
    use crate::facts::sync_effects::allocation_transfer::AllocationJudgment;
    use crate::facts::sync_effects::{Completion, TransferLedger};
    use swc_common::BytePos;

    fn ledger(
        completion: Completion<(), ()>,
        effects: EffectCoverageFragment,
    ) -> TransferLedger<(), ()> {
        TransferLedger::new(
            completion,
            effects,
            Box::default(),
            AllocationJudgment::Opaque,
        )
    }

    fn object_address(origin: u32) -> HeapAddress {
        HeapAddress::module_family(
            source_identity::ChunkId(0),
            crate::facts::abstract_domain::value::Kind::Object,
            origin,
        )
    }

    fn test_callable(key: u32, owner: usize) -> LocalBodyIdentity {
        LocalBodyIdentity::Callable {
            key,
            owner: SourceUnitOrdinal(owner),
        }
    }

    fn test_site(
        body: &LocalBodyIdentity,
        lo: u32,
        hi: u32,
        step: OperationStep,
    ) -> SourceOperationSite {
        let owner = match body {
            LocalBodyIdentity::RootInitializer { owner, .. }
            | LocalBodyIdentity::RootClassDeclaration { owner, .. }
            | LocalBodyIdentity::RootExpression { owner, .. }
            | LocalBodyIdentity::Callable { owner, .. }
            | LocalBodyIdentity::Construction { owner, .. } => *owner,
            LocalBodyIdentity::ImportedCallable(_)
            | LocalBodyIdentity::ProviderCallable { .. }
            | LocalBodyIdentity::Native { .. }
            | LocalBodyIdentity::NativeConstruction { .. } => SourceUnitOrdinal(0),
        };
        SourceOperationSite {
            owner,
            body: body.clone(),
            span: Span::new(BytePos(lo), BytePos(hi)),
            ordinal: lo,
            step,
            control_test_ordinal: None,
        }
    }

    fn test_call(
        caller: LocalBodyIdentity,
        lo: u32,
        hi: u32,
        target: LocalBodyIdentity,
        dependencies: ProofDependencies,
    ) -> LocalCallReceipt {
        LocalCallReceipt {
            site: test_site(&caller, lo, hi, OperationStep::CallInvocation),
            caller,
            target,
            dependencies: Box::new(dependencies),
        }
    }

    fn test_skipped_call(
        caller: LocalBodyIdentity,
        lo: u32,
        hi: u32,
        region: (u32, u32),
        frontier: (u32, u32),
        reason: SkippedCallReason,
        dependencies: ProofDependencies,
    ) -> SkippedCallReceipt {
        let (region_step, frontier_step) = match reason {
            SkippedCallReason::KnownThrow => (OperationStep::BodyRegion, OperationStep::Throw),
            SkippedCallReason::KnownReturn => (OperationStep::BodyRegion, OperationStep::Return),
            SkippedCallReason::HandlerNotEntered => {
                (OperationStep::CatchHandler, OperationStep::TryBody)
            }
            SkippedCallReason::IfConsequentNotSelected => {
                (OperationStep::IfConsequent, OperationStep::IfTest)
            }
            SkippedCallReason::IfAlternateNotSelected => {
                (OperationStep::IfAlternate, OperationStep::IfTest)
            }
        };
        let mut region_site = test_site(&caller, region.0, region.1, region_step);
        let frontier_site = test_site(&caller, frontier.0, frontier.1, frontier_step);
        if matches!(
            reason,
            SkippedCallReason::IfConsequentNotSelected | SkippedCallReason::IfAlternateNotSelected
        ) {
            region_site.control_test_ordinal = Some(frontier_site.ordinal);
        }
        SkippedCallReceipt {
            site: test_site(&caller, lo, hi, OperationStep::CallInvocation),
            region_site,
            frontier_site,
            caller,
            reason,
            dependencies: Box::new(dependencies),
        }
    }

    fn captured_value_input(input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: std::collections::BTreeSet::from([ValueInputRef {
                defining_chunk: "static/provider".to_string(),
                input_index,
            }]),
            ..ProofDependencies::default()
        }
    }

    #[test]
    fn default_binding_effect_authority_is_unknown() {
        assert_eq!(
            BindingEffectCoverage::default().authority(true, false),
            BindingEffectAuthority::Unclassified,
            "programmatic default remains fail-closed"
        );
    }

    #[test]
    fn domains_compose_independently_and_unknown_is_absorbing() {
        let mut first = EffectCoverageFragment::no_effects();
        let initialization = first.initialization.as_mut().unwrap();
        let DomainCoverage::Covered(dependencies) = &mut initialization.global_properties else {
            unreachable!();
        };
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut second = EffectCoverageFragment::no_effects();
        second.initialization.as_mut().unwrap().general_heap = DomainCoverage::Unknown;
        first.compose(&second);
        assert!(
            matches!(&first.initialization.as_ref().unwrap().global_properties, DomainCoverage::Covered(dependencies)
            if dependencies.program_premises.contains(&ProgramPremiseKey::IntrinsicsNeverWritten))
        );
        assert!(matches!(
            &first.initialization.as_ref().unwrap().general_heap,
            DomainCoverage::Unknown
        ));
        assert!(matches!(
            &first.initialization.as_ref().unwrap().host_effects,
            DomainCoverage::Covered(_)
        ));
        assert!(first.initialization_domain_dependencies().is_none());
        first.compose(&EffectCoverageFragment::no_effects());
        assert!(matches!(
            &first.initialization.as_ref().unwrap().general_heap,
            DomainCoverage::Unknown
        ));
    }

    #[test]
    fn prerequisites_cannot_create_a_positive_receipt() {
        let mut dependencies = ProofDependencies::default();
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut unknown = EffectCoverageFragment::default();
        unknown.depend_on(&dependencies);
        assert!(unknown.initialization_domain_dependencies().is_none());
        assert!(unknown.initialization.is_none());
        let mut covered = EffectCoverageFragment::no_effects();
        covered.depend_on(&dependencies);
        assert_eq!(
            covered.initialization_domain_dependencies(),
            Some(dependencies)
        );
    }

    #[test]
    fn completion_owns_normal_and_abrupt_domain_authority() {
        assert!(
            EffectCoverageFragment::default()
                .initialization_domain_dependencies()
                .is_none()
        );
        let effects = EffectCoverageFragment::no_effects();
        assert!(effects.initialization_domain_dependencies().is_some());

        let normal = ledger(Completion::Normal(()), effects.clone());
        assert!(normal.normal_dependencies().is_some());
        let opaque = ledger(Completion::OpaqueMayNormal(()), effects.clone());
        assert!(opaque.normal_dependencies().is_none());
        let abrupt = ledger(Completion::Abrupt(()), effects);
        assert!(abrupt.normal_dependencies().is_none());
        assert!(abrupt.known_abrupt_dependencies().is_some());
    }

    #[test]
    fn repeated_diamond_composition_unions_call_dependencies_and_unknown_absorbs() {
        let root = LocalBodyIdentity::RootInitializer {
            owner: SourceUnitOrdinal(0),
            initializer_span: Span::new(BytePos(1), BytePos(100)),
        };
        let branch = test_callable(1, 0);
        let leaf = test_callable(2, 0);

        // Two root call sites reach the same branch body. Each execution then
        // contributes the same branch-to-leaf source edge under distinct
        // captured-input prerequisites.
        let mut first_path = EffectCoverageFragment::no_effects();
        first_path.selected_local_call(test_call(
            root.clone(),
            10,
            15,
            branch.clone(),
            captured_value_input(1),
        ));
        first_path.selected_local_call(test_call(
            branch.clone(),
            40,
            45,
            leaf.clone(),
            captured_value_input(1),
        ));

        let mut second_path = EffectCoverageFragment::no_effects();
        second_path.selected_local_call(test_call(
            root,
            20,
            25,
            branch.clone(),
            captured_value_input(2),
        ));
        second_path.selected_local_call(test_call(
            branch.clone(),
            40,
            45,
            leaf,
            captured_value_input(2),
        ));

        let mut composed = first_path.clone();
        composed.compose(&second_path);
        composed.compose(&first_path);
        let calls = &composed.call_observations.as_ref().unwrap().calls;
        assert_eq!(calls.len(), 3, "each exact source edge is retained once");
        let branch_to_leaf = calls
            .values()
            .find(|call| call.caller == branch && call.site.span.lo.0 == 40)
            .expect("shared branch-to-leaf edge");
        assert_eq!(
            branch_to_leaf.dependencies.value_inputs,
            std::collections::BTreeSet::from([
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 1,
                },
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 2,
                },
            ]),
            "prerequisites from both contexts are retained"
        );

        composed.compose(&EffectCoverageFragment::default());
        assert!(
            composed.lexical.is_none(),
            "unknown lexical coverage absorbs"
        );
        assert_eq!(
            composed.call_observations.as_ref().unwrap().calls.len(),
            3,
            "unknown effect coverage does not erase exact site observations"
        );
        assert!(composed.lexical_domain_dependencies().is_none());
    }

    #[test]
    fn foreign_body_projection_requires_its_own_complete_call_inventory() {
        let body = test_callable(1, 2);
        let call = test_call(
            body.clone(),
            10,
            15,
            test_callable(20, 3),
            ProofDependencies::default(),
        );
        let sites = std::collections::BTreeSet::from([call.site.clone()]);
        let mut effects = EffectCoverageFragment::no_effects();
        assert!(
            effects
                .project_checked_body(&body, &sites, &sites)
                .is_none()
        );
        effects.selected_local_call(call);
        assert!(
            effects
                .project_checked_body(&body, &sites, &Default::default())
                .is_none()
        );
        let projected = effects.project_checked_body(&body, &sites, &sites).unwrap();
        assert!(projected.has_closed_domains());
        assert!(projected.references_source_sites(&Default::default()));
        assert!(!projected.has_call_inventory(&body, &sites));
        effects.compose(&EffectCoverageFragment::default());
        assert!(
            effects
                .project_checked_body(&body, &sites, &sites)
                .is_none()
        );
    }

    #[test]
    fn call_edge_identity_distinguishes_caller_span_and_target() {
        let caller = test_callable(1, 3);
        let target = test_callable(5, 6);
        let key = local_call_key(&test_call(
            caller.clone(),
            10,
            15,
            target.clone(),
            ProofDependencies::default(),
        ));

        for distinct in [
            test_call(
                caller.clone(),
                11,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                test_callable(1, 4),
                10,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                test_callable(2, 3),
                10,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                caller.clone(),
                10,
                15,
                test_callable(5, 7),
                ProofDependencies::default(),
            ),
            test_call(
                caller,
                10,
                15,
                test_callable(8, 6),
                ProofDependencies::default(),
            ),
        ] {
            assert_ne!(
                key,
                local_call_key(&distinct),
                "a differing caller identity, span, or target keeps a separate edge"
            );
        }
    }

    #[test]
    fn serialized_fragments_preserve_module_qualified_call_identity_and_coverage() {
        let mut fragment = EffectCoverageFragment::no_effects();
        let caller = test_callable(7, 2);
        for module in ["first", "second"] {
            fragment.selected_local_call(test_call(
                caller.clone(),
                10,
                15,
                LocalBodyIdentity::ImportedCallable(Box::new(ImportedCallableIdentity {
                    defining_module: module.into(),
                    export: "make".into(),
                    local: "make".into(),
                    member: None,
                    key: 1,
                })),
                ProofDependencies::default(),
            ));
        }
        let chunks = source_identity::ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let saved = SavedEffectCoverageFragment::snapshot(&fragment, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let decoded = saved.restore(&ordinals).unwrap();
        assert_eq!(decoded, fragment);
        assert!(decoded.has_closed_domains());
        assert_eq!(decoded.call_observations.as_ref().unwrap().calls.len(), 2);
        let mut unknown = EffectCoverageFragment::default();
        unknown.compose(&decoded);
        let saved = SavedEffectCoverageFragment::snapshot(&unknown, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let decoded = saved.restore(&ordinals).unwrap();
        assert!(
            !decoded.has_closed_domains(),
            "artifact transport must not promote unknown domains"
        );
    }

    #[test]
    fn saved_provider_call_targets_still_require_current_registry_resolution() {
        let provider = LocalBodyIdentity::ProviderCallable {
            defining_module: "static/provider".into(),
            source_sha256: "a".repeat(64),
            source_origin: 41,
        };
        let caller = test_callable(7, 2);
        let mut fragment = EffectCoverageFragment::no_effects();
        fragment.selected_local_call(test_call(
            caller,
            10,
            15,
            provider,
            ProofDependencies::default(),
        ));

        let mut matches_current = |module: &str, sha256: &str, origin: u32| {
            module == "static/provider" && sha256 == "a".repeat(64) && origin == 41
        };
        assert!(fragment.provider_callables_are_current(&mut matches_current));
        let mut rejects_missing_registry_entry = |_: &str, _: &str, _: u32| false;
        assert!(!fragment.provider_callables_are_current(&mut rejects_missing_registry_entry));

        let chunks = source_identity::ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let saved = SavedEffectCoverageFragment::snapshot(&fragment, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore(&ordinals).unwrap();
        let mut current = |module: &str, sha256: &str, origin: u32| {
            module == "static/provider" && sha256 == "a".repeat(64) && origin == 41
        };
        assert!(restored.provider_callables_are_current(&mut current));

        let wrong_target = LocalBodyIdentity::ProviderCallable {
            defining_module: "static/provider".into(),
            source_sha256: "b".repeat(64),
            source_origin: 41,
        };
        let mut wrong = EffectCoverageFragment::no_effects();
        wrong.selected_local_call(test_call(
            test_callable(7, 2),
            10,
            15,
            wrong_target,
            ProofDependencies::default(),
        ));
        let saved = SavedEffectCoverageFragment::snapshot(&wrong, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore(&ordinals).unwrap();
        let mut current = |module: &str, sha256: &str, origin: u32| {
            module == "static/provider" && sha256 == "a".repeat(64) && origin == 41
        };
        assert!(
            !restored.provider_callables_are_current(&mut current),
            "serialized provider identity must not authorize an absent digest"
        );

        let caller = LocalBodyIdentity::RootExpression {
            owner: SourceUnitOrdinal(2),
            expression_span: Span::new(BytePos(1), BytePos(100)),
        };
        let target = LocalBodyIdentity::ProviderCallable {
            defining_module: "static/provider".into(),
            source_sha256: "a".repeat(64),
            source_origin: 41,
        };
        let identity = RootEffectIdentity {
            owner: SourceUnitOrdinal(2),
            source: RootEffectSource::ExpressionStatement {
                expression_span: Span::new(BytePos(1), BytePos(100)),
            },
        };
        let receipt = LexicalBindingEffectReceipt {
            identity: identity.clone(),
            read_dependencies: ProofDependencies::default(),
            rebind_dependencies: ProofDependencies::default(),
            calls: vec![test_call(
                caller,
                10,
                15,
                target,
                ProofDependencies::default(),
            )],
            skipped_calls: Vec::new(),
        };
        let coverage = BindingEffectCoverage::OrderedRoot {
            identity,
            receipt: Some(Box::new(receipt)),
        };
        let saved = coverage.snapshot(&ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedBindingEffectCoverage<'_> = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore(&ordinals).unwrap();
        let mut current = |module: &str, sha256: &str, origin: u32| {
            module == "static/provider" && sha256 == "a".repeat(64) && origin == 41
        };
        assert!(restored.provider_callables_are_current(&mut current));
        let mut reject_absent_current_callable = |_: &str, _: &str, _: u32| false;
        assert!(!restored.provider_callables_are_current(&mut reject_absent_current_callable));
    }

    #[test]
    fn operation_support_survives_unknown_composition_and_saved_roundtrip_without_authority() {
        let mut writer_chunks = source_identity::ChunkTable::default();
        let writer_module = writer_chunks.intern("writer".to_owned());
        let writer_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let receipt = OperationSupportReceipt::pooled_object_shape(
            OperationSupportOperation::OwnDataRead,
            HeapAddress::module_family(
                writer_module,
                crate::facts::abstract_domain::value::Kind::Object,
                17,
            ),
            &ProofDependencies::default(),
        );
        let mut fragment = EffectCoverageFragment::no_effects();
        fragment.operation_support(receipt.clone());
        assert!(fragment.initialization_domain_dependencies().is_some());

        fragment.compose(&EffectCoverageFragment::default());
        assert!(fragment.initialization_domain_dependencies().is_none());
        assert_eq!(
            fragment.operation_support_receipts(),
            std::slice::from_ref(&receipt)
        );

        let saved = SavedEffectCoverageFragment::snapshot(&fragment, &writer_ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let mut reader_chunks = source_identity::ChunkTable::default();
        reader_chunks.intern("other".to_owned());
        let reader_module = reader_chunks.intern("writer".to_owned());
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let restored = saved.restore(&reader_ordinals).unwrap();
        assert_eq!(
            restored.operation_support_receipts()[0].projected_cell(),
            Some(EffectCell::ObjectShape(HeapAddress::module_family(
                reader_module,
                crate::facts::abstract_domain::value::Kind::Object,
                17,
            )))
        );
        assert!(restored.initialization_domain_dependencies().is_none());

        let mut incomplete: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        incomplete
            .as_object_mut()
            .unwrap()
            .remove("operation_support");
        assert!(serde_json::from_value::<SavedEffectCoverageFragment<'_>>(incomplete).is_err());
        let mut unknown_module: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        unknown_module["operation_support"][0]["support"]["origin"]["module"] =
            serde_json::Value::String("missing".to_owned());
        let unknown_module: SavedEffectCoverageFragment<'_> =
            serde_json::from_value(unknown_module).unwrap();
        assert!(unknown_module.restore(&reader_ordinals).is_err());
        let mut mismatched_kind: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        mismatched_kind["operation_support"][0]["support"]["origin"]["kind"] =
            serde_json::to_value(crate::facts::abstract_domain::value::Kind::Function).unwrap();
        let mismatched_kind: SavedEffectCoverageFragment<'_> =
            serde_json::from_value(mismatched_kind).unwrap();
        assert!(mismatched_kind.restore(&reader_ordinals).is_err());
        let local_scope = crate::facts::abstract_domain::value::HeapModuleScope::LocalAnalysis(
            crate::facts::abstract_domain::value::LocalAnalysisScopeId::fresh(),
        );
        let mut local = EffectCoverageFragment::no_effects();
        local.operation_support(OperationSupportReceipt::pooled_object_shape(
            OperationSupportOperation::OwnDataRead,
            HeapAddress::in_scope(
                local_scope,
                crate::facts::abstract_domain::value::Kind::Object,
                17,
            ),
            &ProofDependencies::default(),
        ));
        assert!(SavedEffectCoverageFragment::snapshot(&local, &writer_ordinals).is_err());
    }

    #[test]
    fn parameter_support_instantiates_only_with_its_argument_origin() {
        let mut parameterized = EffectCoverageFragment::no_effects();
        parameterized.operation_support(OperationSupportReceipt::parameter_object_shape(
            OperationSupportOperation::OwnDataRead,
            0,
            &ProofDependencies::default(),
        ));
        assert!(parameterized.has_uninstantiated_operation_support());
        assert!(!parameterized.instantiate_operation_support(&[]));

        let mut wrong_kind = parameterized.clone();
        let function = HeapAddress::module_family(
            source_identity::ChunkId(0),
            crate::facts::abstract_domain::value::Kind::Function,
            91,
        );
        assert!(!wrong_kind.instantiate_operation_support(&[function]));
        assert!(wrong_kind.has_uninstantiated_operation_support());

        assert!(parameterized.instantiate_operation_support(&[object_address(91)]));
        assert_eq!(
            parameterized.operation_support_receipts()[0].projected_cell(),
            Some(EffectCell::ObjectShape(object_address(91)))
        );

        let mut provider_world = EffectCoverageFragment::no_effects();
        provider_world.operation_support(OperationSupportReceipt::provider_world_object_shape(
            OperationSupportOperation::OwnDataRead,
            object_address(7),
            &ProofDependencies::default(),
        ));
        assert!(provider_world.has_unresolved_provider_operation_support());
        assert!(!provider_world.instantiate_operation_support(&[object_address(91)]));
        provider_world.compose(&EffectCoverageFragment::default());
        assert!(
            provider_world
                .initialization_domain_dependencies()
                .is_none()
        );
        assert_eq!(
            provider_world.operation_support_receipts()[0].projected_cell(),
            None,
            "provider-world pooled origins never become caller heap cells"
        );
    }

    #[test]
    fn provider_local_birth_seals_without_cross_world_address_or_projection() {
        let mut effects = EffectCoverageFragment::no_effects();
        effects.operation_support(OperationSupportReceipt::provider_local_birth_candidate(
            OperationSupportOperation::WritableOwnDataStore,
            object_address(7),
            &ProofDependencies::default(),
        ));
        let mut candidate = ledger(Completion::Normal(()), effects);
        assert!(candidate.effects.has_uninstantiated_operation_support());
        assert!(
            candidate
                .effects
                .has_unresolved_provider_operation_support()
        );
        assert!(
            !candidate
                .effects
                .instantiate_operation_support(&[object_address(91)])
        );
        assert_eq!(
            candidate.effects.operation_support_receipts()[0].projected_cell(),
            None
        );

        let fresh = std::collections::BTreeSet::from([object_address(7)]);
        let borrowed = std::collections::BTreeSet::new();
        assert!(candidate.seal_provider_local_birth_support(&fresh, &borrowed));
        assert!(!candidate.effects.has_uninstantiated_operation_support());
        assert!(
            !candidate
                .effects
                .has_unresolved_provider_operation_support()
        );
        assert!(
            candidate
                .effects
                .instantiate_operation_support(&[object_address(91)])
        );
        let receipt = &candidate.effects.operation_support_receipts()[0];
        assert_eq!(
            receipt.support,
            OperationSupportLocation::ProviderLocalBirth
        );
        assert_eq!(receipt.projected_cell(), None);

        let chunks = source_identity::ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let saved = SavedEffectCoverageFragment::snapshot(&candidate.effects, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(saved.restore(&ordinals).unwrap(), candidate.effects);
        let mut incomplete: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        incomplete
            .as_object_mut()
            .unwrap()
            .remove("operation_support");
        assert!(serde_json::from_value::<SavedEffectCoverageFragment<'_>>(incomplete).is_err());
    }

    #[test]
    fn provider_local_birth_refuses_missing_or_borrowed_frame_birth() {
        let candidate = || {
            let mut fragment = EffectCoverageFragment::no_effects();
            fragment.operation_support(OperationSupportReceipt::provider_local_birth_candidate(
                OperationSupportOperation::OwnDataRead,
                object_address(7),
                &ProofDependencies::default(),
            ));
            ledger(Completion::Normal(()), fragment)
        };
        let fresh = std::collections::BTreeSet::from([object_address(7)]);
        let borrowed = std::collections::BTreeSet::from([object_address(7)]);
        assert!(!candidate().seal_provider_local_birth_support(
            &std::collections::BTreeSet::new(),
            &std::collections::BTreeSet::new(),
        ));
        assert!(!candidate().seal_provider_local_birth_support(&fresh, &borrowed));
    }

    #[test]
    fn provider_local_birth_refuses_uncertain_or_abrupt_completion() {
        let candidate = || {
            let mut fragment = EffectCoverageFragment::no_effects();
            fragment.operation_support(OperationSupportReceipt::provider_local_birth_candidate(
                OperationSupportOperation::OwnDataRead,
                object_address(7),
                &ProofDependencies::default(),
            ));
            fragment
        };
        let fresh = std::collections::BTreeSet::from([object_address(7)]);
        let borrowed = std::collections::BTreeSet::new();

        let mut uncertain_effects = candidate();
        uncertain_effects.compose(&EffectCoverageFragment::default());
        let mut uncertain = ledger(Completion::OpaqueMayNormal(()), uncertain_effects);
        assert!(!uncertain.seal_provider_local_birth_support(&fresh, &borrowed));
        assert!(
            uncertain
                .effects
                .has_unresolved_provider_operation_support()
        );

        let mut abrupt = ledger(Completion::Abrupt(()), candidate());
        assert!(!abrupt.seal_provider_local_birth_support(&fresh, &borrowed));
        assert!(abrupt.effects.has_unresolved_provider_operation_support());
    }

    #[test]
    fn call_inventory_unions_invoked_and_skipped_site_evidence_exactly() {
        let caller = test_callable(7, 2);
        let mut fragment = EffectCoverageFragment::no_effects();
        fragment.selected_local_call(test_call(
            caller.clone(),
            10,
            15,
            test_callable(8, 2),
            ProofDependencies::default(),
        ));
        fragment.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::KnownThrow,
            ProofDependencies::default(),
        ));

        let first_site = test_site(&caller, 10, 15, OperationStep::CallInvocation);
        let second_site = test_site(&caller, 20, 25, OperationStep::CallInvocation);
        let expected = std::collections::BTreeSet::from([first_site.clone(), second_site.clone()]);
        assert!(fragment.has_call_inventory(&caller, &expected));
        assert!(fragment.has_local_call(&caller, &first_site));
        assert!(fragment.has_local_call(&caller, &second_site));
        assert!(!fragment.has_local_call(
            &caller,
            &test_site(&caller, 30, 35, OperationStep::CallInvocation)
        ));
        assert!(!fragment.has_call_inventory(
            &caller,
            &std::collections::BTreeSet::from([first_site.clone()])
        ));
        assert!(!fragment.has_call_inventory(
            &caller,
            &std::collections::BTreeSet::from([
                first_site,
                second_site,
                test_site(&caller, 30, 35, OperationStep::CallInvocation),
            ])
        ));
        assert!(!fragment.has_call_inventory(&test_callable(7, 3), &expected));
    }

    #[test]
    fn branch_local_call_and_skip_join_without_a_global_single_arm_claim() {
        let caller = test_callable(7, 2);
        let call_site = test_site(&caller, 20, 25, OperationStep::CallInvocation);
        let mut selected_arm = EffectCoverageFragment::no_effects();
        selected_arm.selected_local_call(test_call(
            caller.clone(),
            20,
            25,
            test_callable(8, 2),
            ProofDependencies::default(),
        ));
        let mut other_arm = EffectCoverageFragment::no_effects();
        other_arm.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (10, 30),
            (5, 9),
            SkippedCallReason::IfConsequentNotSelected,
            ProofDependencies::default(),
        ));

        let joined = EffectCoverageFragment::join_alternatives(&[&selected_arm, &other_arm])
            .expect("both feasible arms have effect fragments");
        let expected = std::collections::BTreeSet::from([call_site.clone()]);
        assert!(joined.has_call_inventory(&caller, &expected));
        let observations = joined.call_observations.as_deref().unwrap();
        assert!(
            observations.calls.is_empty(),
            "a branch-only call is not definite"
        );
        assert!(
            observations.skipped_calls.is_empty(),
            "a branch-only skip is not a global single-arm claim"
        );
        assert!(
            observations
                .alternative_accounted_sites
                .contains(&call_site)
        );
        assert!(
            joined
                .possible_local_calls()
                .any(|call| call.site == call_site)
        );
        assert!(
            joined
                .possible_skipped_local_calls()
                .any(|call| call.site == call_site)
        );

        let mut malformed = test_skipped_call(
            caller.clone(),
            20,
            25,
            (10, 30),
            (5, 9),
            SkippedCallReason::IfConsequentNotSelected,
            ProofDependencies::default(),
        );
        malformed.region_site.step = OperationStep::IfAlternate;
        let malformed_sites = std::collections::BTreeSet::from([
            malformed.site.clone(),
            malformed.region_site.clone(),
            malformed.frontier_site.clone(),
        ]);
        assert!(!skipped_receipt_references_source_sites(
            &malformed,
            &malformed_sites
        ));

        let mut cross_if = test_skipped_call(
            caller.clone(),
            20,
            25,
            (10, 30),
            (5, 9),
            SkippedCallReason::IfConsequentNotSelected,
            ProofDependencies::default(),
        );
        cross_if.frontier_site = test_site(&caller, 40, 44, OperationStep::IfTest);
        let cross_if_sites = std::collections::BTreeSet::from([
            cross_if.site.clone(),
            cross_if.region_site.clone(),
            cross_if.frontier_site.clone(),
        ]);
        assert!(cross_if_sites.contains(&cross_if.frontier_site));
        assert!(
            !skipped_receipt_references_source_sites(&cross_if, &cross_if_sites),
            "another valid IfTest in the same body cannot authenticate this arm"
        );

        let mut outside_arm = test_skipped_call(
            caller.clone(),
            20,
            25,
            (10, 30),
            (5, 9),
            SkippedCallReason::IfConsequentNotSelected,
            ProofDependencies::default(),
        );
        outside_arm.site = test_site(&caller, 40, 45, OperationStep::CallInvocation);
        let outside_sites = std::collections::BTreeSet::from([
            outside_arm.site.clone(),
            outside_arm.region_site.clone(),
            outside_arm.frontier_site.clone(),
        ]);
        assert!(outside_sites.contains(&outside_arm.site));
        assert!(
            !skipped_receipt_references_source_sites(&outside_arm, &outside_sites),
            "a valid same-body call outside the skipped arm cannot be substituted"
        );
        let possible_outside = PossibleSkippedCallObligation::from_receipt(&outside_arm);
        assert!(
            !possible_skipped_call_references_source_sites(&possible_outside, &outside_sites),
            "saved possible-skip obligations use the same containment proof"
        );
    }

    #[test]
    fn skipped_call_composition_merges_only_equal_frontier_evidence() {
        let caller = test_callable(7, 2);
        let mut first = EffectCoverageFragment::no_effects();
        first.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::KnownThrow,
            captured_value_input(1),
        ));
        let mut second = EffectCoverageFragment::no_effects();
        second.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::KnownThrow,
            captured_value_input(2),
        ));
        second.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (12, 17),
            SkippedCallReason::KnownThrow,
            captured_value_input(3),
        ));

        first.compose(&second);
        let skipped_calls = &first.call_observations.as_ref().unwrap().skipped_calls;
        assert_eq!(skipped_calls.len(), 2);
        let same_frontier = skipped_calls
            .values()
            .find(|call| call.frontier_site.span.lo.0 == 10)
            .unwrap();
        assert_eq!(
            same_frontier.dependencies.value_inputs,
            std::collections::BTreeSet::from([
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 1,
                },
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 2,
                },
            ])
        );
        assert!(first.has_call_inventory(
            &caller,
            &std::collections::BTreeSet::from([test_site(
                &caller,
                20,
                25,
                OperationStep::CallInvocation,
            )])
        ));
    }

    #[test]
    fn propagated_known_throw_frontier_is_the_callers_exact_invocation() {
        let caller = test_callable(7, 2);
        let mut skipped = test_skipped_call(
            caller.clone(),
            40,
            45,
            (1, 100),
            (20, 25),
            SkippedCallReason::KnownThrow,
            ProofDependencies::default(),
        );
        skipped.frontier_site = test_site(&caller, 20, 25, OperationStep::CallInvocation);
        let source_sites = std::collections::BTreeSet::from([
            skipped.site.clone(),
            skipped.region_site.clone(),
            skipped.frontier_site.clone(),
        ]);
        assert!(skipped_receipt_references_source_sites(
            &skipped,
            &source_sites
        ));

        skipped.frontier_site =
            test_site(&test_callable(8, 2), 20, 25, OperationStep::CallInvocation);
        assert!(!skipped_receipt_references_source_sites(
            &skipped,
            &source_sites
        ));
    }

    #[test]
    fn known_throw_dependencies_are_required_before_completion_is_consumed() {
        let mut abrupt = ledger(Completion::Abrupt(()), EffectCoverageFragment::no_effects());
        assert_eq!(
            abrupt.known_abrupt_dependencies(),
            Some(ProofDependencies::default())
        );
        assert!(abrupt.consume_known_abrupt(()));
        assert!(matches!(abrupt.completion, Completion::Normal(())));

        let mut unknown = ledger(
            Completion::OpaqueMayNormal(()),
            EffectCoverageFragment::no_effects(),
        );
        assert!(unknown.known_abrupt_dependencies().is_none());
        assert!(!unknown.consume_known_abrupt(()));
        assert!(matches!(
            unknown.completion,
            Completion::OpaqueMayNormal(())
        ));

        let mut uncovered_effects = EffectCoverageFragment::no_effects();
        uncovered_effects
            .initialization
            .as_mut()
            .unwrap()
            .host_effects = DomainCoverage::Unknown;
        let mut uncovered = ledger(Completion::Abrupt(()), uncovered_effects);
        assert!(uncovered.known_abrupt_dependencies().is_none());
        assert!(!uncovered.consume_known_abrupt(()));
        assert!(matches!(uncovered.completion, Completion::Abrupt(())));
    }

    #[test]
    fn skipped_call_receipts_survive_saved_fragment_and_root_receipt_roundtrips() {
        let caller = test_callable(7, 2);
        let skipped_call = test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::HandlerNotEntered,
            captured_value_input(4),
        );
        let mut fragment = EffectCoverageFragment::no_effects();
        fragment.skipped_local_call(skipped_call.clone());

        let chunks = source_identity::ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let saved = SavedEffectCoverageFragment::snapshot(&fragment, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore(&ordinals).unwrap();
        assert_eq!(restored, fragment);
        assert!(restored.has_local_call(&caller, &skipped_call.site));

        let receipt = LexicalBindingEffectReceipt {
            identity: RootEffectIdentity {
                owner: SourceUnitOrdinal(2),
                source: RootEffectSource::ExpressionStatement {
                    expression_span: Span::new(BytePos(1), BytePos(100)),
                },
            },
            read_dependencies: ProofDependencies::default(),
            rebind_dependencies: ProofDependencies::default(),
            calls: Vec::new(),
            skipped_calls: vec![skipped_call.clone()],
        };
        let chunks = source_identity::ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let saved = SavedLexicalBindingEffectReceipt::snapshot(&receipt, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedLexicalBindingEffectReceipt = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore(&ordinals).unwrap();
        assert_eq!(restored, receipt);
        let coverage = BindingEffectCoverage::OrderedRoot {
            identity: receipt.identity.clone(),
            receipt: Some(Box::new(receipt.clone())),
        };
        let mut source_sites = std::collections::BTreeSet::from([
            skipped_call.site.clone(),
            skipped_call.region_site.clone(),
            skipped_call.frontier_site.clone(),
        ]);
        assert!(coverage.references_source_sites(&source_sites));

        let saved = coverage.snapshot(&ordinals).unwrap();
        let mut encoded = serde_json::to_value(&saved).unwrap();
        let receipt_json = encoded["ordered_root"]["receipt"]
            .as_object_mut()
            .expect("ordered root receipt is a struct");
        receipt_json["skipped_calls"][0]
            .as_object_mut()
            .expect("skipped call receipt is a struct")
            .remove("site");
        assert!(serde_json::from_value::<SavedBindingEffectCoverage<'_>>(encoded).is_err());

        let saved = coverage.snapshot(&ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedBindingEffectCoverage<'_> = serde_json::from_slice(&bytes).unwrap();
        let restored_coverage = saved.restore(&ordinals).unwrap();
        assert!(restored_coverage.references_source_sites(&source_sites));
        source_sites.remove(&skipped_call.frontier_site);
        assert!(!restored_coverage.references_source_sites(&source_sites));

        let report = restored.to_report();
        assert_eq!(report.skipped_calls.len(), 1);
        assert_eq!(
            report.skipped_calls[0].reason,
            SkippedCallReason::HandlerNotEntered
        );
        assert_eq!(report.skipped_calls[0].call_span, skipped_call.site.span);
        assert_eq!(report.skipped_calls[0].caller, caller.to_report());
    }
}
