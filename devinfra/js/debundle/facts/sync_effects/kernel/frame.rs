//! One activation of the kernel and the values that cross its boundary.

use super::super::allocation_transfer::{AllocationJudgment, AllocationValue};
use super::super::array_transfer::ArrayPushSelection;
use super::super::completion::{AbruptAlternative, Completion, TransferLedger};
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::value_model::{AbstractValue, Context, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::{Kind, Value};
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

pub(in crate::facts::sync_effects) struct LocalFrame {
    pub(in crate::facts::sync_effects) environment: Context,
    pub(in crate::facts::sync_effects) binding_selections: BTreeMap<Id, CallableSelection>,
    /// Present only when the caller delegates the actual parameter prologue
    /// to this kernel. Provider/compatibility frames are already bound.
    pub(in crate::facts::sync_effects) parameter_inputs: Option<Vec<SelectedValue>>,
    /// Bindings initialized by an actual operation in this activation.
    /// Prebound values alone do not establish a declarative-cell receipt.
    pub(in crate::facts::sync_effects) initialized_bindings: BTreeSet<Id>,
    /// Completion, effects and allocation judgment of the body so far. The
    /// normal path is `LocalNormal::Next` until a return or throw completes it.
    pub(in crate::facts::sync_effects) ledger: TransferLedger<LocalNormal, KnownThrow>,
    /// Entry arguments/captures and external values read while executing.
    /// Retained for a truncated remainder even after local aliases change.
    pub(in crate::facts::sync_effects) inputs: Values,
    pub(in crate::facts::sync_effects) borrowed_records: BTreeSet<u32>,
    pub(in crate::facts::sync_effects) fresh_records: BTreeSet<u32>,
    /// Set only by a checked constructor provider query. Other local calls do
    /// not acquire a receiver just because their syntax mentions `this`.
    pub(in crate::facts::sync_effects) this_values: Option<Values>,
}

impl LocalFrame {
    pub(in crate::facts::sync_effects) fn new(environment: Context) -> Self {
        Self {
            inputs: environment.values().flatten().cloned().collect(),
            environment,
            binding_selections: BTreeMap::new(),
            parameter_inputs: None,
            initialized_bindings: BTreeSet::new(),
            ledger: TransferLedger::new(
                Completion::Normal(LocalNormal::Next),
                EffectCoverageFragment::no_effects(),
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ),
            borrowed_records: BTreeSet::new(),
            fresh_records: BTreeSet::new(),
            this_values: None,
        }
    }

    /// Whether the body's next operation runs: the normal path is still
    /// `Next`, possibly beside abrupt alternatives already recorded.
    pub(super) fn continues(&self) -> bool {
        matches!(self.ledger.completion.may_normal(), Some(LocalNormal::Next))
    }

    /// An operand may have completed abruptly with an unrepresented payload.
    /// The normal path continues; the body can no longer prove completion or
    /// an allocation-only judgment.
    pub(super) fn record_unknown_abrupt(&mut self) {
        let (normal, mut abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        abrupt.push(AbruptAlternative::Unknown(None));
        self.ledger.completion = Completion::from_alternatives(normal, abrupt, non_normal);
        self.ledger.allocation = AllocationJudgment::Opaque;
    }

    /// No operand value remains on the normal path: no later operation of
    /// this body runs. Abrupt alternatives already recorded are kept.
    pub(super) fn stop_normal_path(&mut self) {
        let (_, abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        self.ledger.completion = Completion::from_alternatives(None, abrupt, non_normal);
    }

    pub(super) fn complete_throw(&mut self) {
        let (_, mut abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        abrupt.push(AbruptAlternative::Exact(KnownThrow));
        self.ledger.completion = Completion::from_alternatives(None, abrupt, non_normal);
        self.ledger.allocation = AllocationJudgment::Opaque;
    }

    pub(super) fn complete_return(&mut self, selected: SelectedValue) {
        let (_, abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        self.ledger.completion = Completion::from_alternatives(
            Some(LocalNormal::Return {
                selected: Box::new(selected),
            }),
            abrupt,
            non_normal,
        );
    }

    /// An adapter stopped the body after an admitted terminal call ran. The
    /// call's possible results are retained as a possible normal value only;
    /// neither fallthrough nor a source return is proven.
    pub(in crate::facts::sync_effects) fn complete_unknown_return(&mut self, values: Values) {
        let (_, mut abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        abrupt.push(AbruptAlternative::Unknown(None));
        self.ledger.completion = Completion::from_alternatives(
            Some(LocalNormal::Unknown(SelectedValue::raw(values))),
            abrupt,
            non_normal,
        );
        self.ledger.allocation = AllocationJudgment::Opaque;
    }

    /// The possible results of the body: every normal value, plus
    /// `Unavailable` when some path completes abruptly.
    pub(in crate::facts::sync_effects) fn returns(&self) -> Values {
        let mut returns = Values::new();
        match self.ledger.completion.may_normal() {
            Some(LocalNormal::Return { selected }) => {
                returns.extend(selected.values.iter().cloned());
            }
            Some(LocalNormal::Unknown(selected)) => {
                returns.extend(selected.values.iter().cloned());
            }
            Some(LocalNormal::Next) | None => {}
        }
        if self.ledger.completion.may_be_abrupt() {
            returns.insert(AbstractValue::Value(Value::Unavailable));
        }
        returns
    }

    /// The selected callable returned on an exactly normal completion.
    pub(in crate::facts::sync_effects) fn return_selection(&self) -> Option<CallableSelection> {
        let Completion::Normal(LocalNormal::Return { selected }) = &self.ledger.completion else {
            return None;
        };
        selected
            .selection
            .clone()
            .filter(|selection| selection.matches(&selected.values))
    }
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) enum LocalNormal {
    Next,
    Return {
        selected: Box<SelectedValue>,
    },
    /// The body boundary was refused after some prefix ran. This is a possible
    /// normal value only, never a proven fallthrough or source return.
    Unknown(SelectedValue),
}

/// An exact source throw of this body, as opposed to an operand that may have
/// thrown. The adapter publishes the thrown value at the throw; the payload
/// joins this marker when a handler construct can consume it.
#[derive(Clone)]
pub(in crate::facts::sync_effects) struct KnownThrow;

/// Nonterminal provenance of an actually selected abstract callable. This
/// proves neither fresh allocation nor complete invocation effects.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(in crate::facts::sync_effects) struct CallableSelection {
    pub(in crate::facts::sync_effects) target: AbstractValue,
    pub(in crate::facts::sync_effects) dependencies: Box<ProofDependencies>,
}

impl CallableSelection {
    pub(in crate::facts::sync_effects) fn observed(
        values: &Values,
        dependencies: Box<ProofDependencies>,
    ) -> Option<Self> {
        if values.len() != 1 {
            return None;
        }
        let target = values.first()?.clone();
        matches!(
            &target,
            AbstractValue::Closure(..) | AbstractValue::Value(Value::Alloc(Kind::Function, _))
        )
        .then_some(Self {
            target,
            dependencies,
        })
    }

    pub(in crate::facts::sync_effects) fn matches(&self, values: &Values) -> bool {
        values.len() == 1 && values.first() == Some(&self.target)
    }
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) struct SelectedValue {
    pub(in crate::facts::sync_effects) values: Values,
    pub(in crate::facts::sync_effects) selection: Option<CallableSelection>,
}

impl SelectedValue {
    pub(in crate::facts::sync_effects) fn raw(values: Values) -> Self {
        Self {
            values,
            selection: None,
        }
    }
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) enum EvaluatedCallee {
    Direct(SelectedValue),
    Member {
        receiver: Values,
        targets: Values,
        selection_dependencies: Option<Box<ProofDependencies>>,
        array_push: Option<Box<ArrayPushSelection>>,
    },
}

impl EvaluatedCallee {
    pub(in crate::facts::sync_effects) fn targets(&self) -> &Values {
        match self {
            Self::Direct(targets) => &targets.values,
            Self::Member { targets, .. } => targets,
        }
    }

    pub(in crate::facts::sync_effects) fn receiver(&self) -> Option<&Values> {
        match self {
            Self::Direct(_) => None,
            Self::Member { receiver, .. } => Some(receiver),
        }
    }
}

/// One value produced by an adapter for the kernel: its allocation judgment,
/// the effects of producing it, and the callable it selects, if any.
pub(in crate::facts::sync_effects) struct LocalValueResult {
    pub(in crate::facts::sync_effects) allocation: AllocationValue,
    pub(in crate::facts::sync_effects) effects: EffectCoverageFragment,
    pub(in crate::facts::sync_effects) selection: Option<CallableSelection>,
}

/// A call whose callee and operands the kernel evaluated in source order. The
/// adapter performs the invocation and must not evaluate the operands again.
pub(in crate::facts::sync_effects) struct LocalCallRequest {
    pub(in crate::facts::sync_effects) callee: EvaluatedCallee,
    pub(in crate::facts::sync_effects) arguments: Vec<SelectedValue>,
    pub(in crate::facts::sync_effects) span: Span,
}

pub(in crate::facts::sync_effects) struct LocalTransferResult {
    pub(in crate::facts::sync_effects) ledger: TransferLedger<LocalNormal, KnownThrow>,
}

/// What remains to account for after a refusal. An adapter that summarized
/// the body's remainder when it refused reports `AlreadySummarized`; a kernel
/// or adapter refusal with no summary leaves that to the driver's caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(in crate::facts::sync_effects) enum RemainderDisposition {
    NeedsSummary,
    AlreadySummarized,
}

pub(in crate::facts::sync_effects) enum LocalExecutionAttempt {
    Completed(LocalTransferResult),
    Refused(RemainderDisposition),
}
