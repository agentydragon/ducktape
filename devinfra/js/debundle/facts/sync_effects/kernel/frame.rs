//! One activation of the kernel and the values that cross its boundary.

use super::super::allocation_transfer::{AllocationJudgment, AllocationValue};
use super::super::array_transfer::ArrayMethodSelection;
use super::super::completion::{AbruptAlternative, Completion, TransferLedger};
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::value_model::{AbstractValue, Context, Values};
use analysis_vocabulary::inputs::ProofDependencies;
use analysis_vocabulary::value::{Kind, Value};
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Clone)]
pub(crate) struct LocalFrame {
    pub(crate) environment: Context,
    pub(crate) binding_selections: BTreeMap<Id, CallableSelection>,
    /// Present only when the caller delegates the actual parameter prologue
    /// to this kernel. Provider/compatibility frames are already bound.
    pub(crate) parameter_inputs: Option<Vec<SelectedValue>>,
    /// Bindings initialized by an actual operation in this activation.
    /// Prebound values alone do not establish a declarative-cell receipt.
    pub(crate) initialized_bindings: BTreeSet<Id>,
    /// Completion, effects and allocation judgment of the body so far. The
    /// normal path is `LocalNormal::Next` until a return or throw completes it.
    pub(crate) ledger: TransferLedger<LocalNormal, KnownThrow>,
    /// Entry arguments/captures and external values read while executing.
    /// Retained for a truncated remainder even after local aliases change.
    pub(crate) inputs: Values,
    pub(crate) borrowed_records: BTreeSet<u32>,
    pub(crate) fresh_records: BTreeSet<u32>,
    /// Set only by a checked constructor provider query or the kernel's
    /// construct rule. Other local calls do not acquire a receiver just
    /// because their syntax mentions `this`.
    pub(crate) this_values: Option<Values>,
    /// The class whose base constructor this activation runs, when the
    /// receiver is the fresh instance of the kernel's construct rule.
    pub(crate) constructed_class: Option<u32>,
}

impl LocalFrame {
    pub(crate) fn new(environment: Context) -> Self {
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
            constructed_class: None,
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

    pub(super) fn complete_throw(&mut self, values: Values) {
        let (_, mut abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        abrupt.push(AbruptAlternative::Exact(KnownThrow { values }));
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
    pub(crate) fn complete_unknown_return(&mut self, values: Values) {
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
    pub(crate) fn returns(&self) -> Values {
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
    pub(crate) fn return_selection(&self) -> Option<CallableSelection> {
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
pub(crate) enum LocalNormal {
    Next,
    Return {
        selected: Box<SelectedValue>,
    },
    /// The body boundary was refused after some prefix ran. This is a possible
    /// normal value only, never a proven fallthrough or source return.
    Unknown(SelectedValue),
}

/// An exact source throw of this body, as opposed to an operand that may have
/// thrown. The adopter publishes the thrown values; a handler construct can
/// consume them once the IR lowers one.
#[derive(Clone)]
pub(crate) struct KnownThrow {
    pub(crate) values: Values,
}

/// Nonterminal provenance of an actually selected abstract callable. This
/// proves neither fresh allocation nor complete invocation effects.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct CallableSelection {
    pub(crate) target: AbstractValue,
    pub(crate) dependencies: Box<ProofDependencies>,
}

impl CallableSelection {
    pub(crate) fn observed(values: &Values, dependencies: Box<ProofDependencies>) -> Option<Self> {
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

    pub(crate) fn matches(&self, values: &Values) -> bool {
        values.len() == 1 && values.first() == Some(&self.target)
    }
}

#[derive(Clone)]
pub(crate) struct SelectedValue {
    pub(crate) values: Values,
    pub(crate) selection: Option<CallableSelection>,
}

impl SelectedValue {
    pub(crate) fn raw(values: Values) -> Self {
        Self {
            values,
            selection: None,
        }
    }
}

#[derive(Clone)]
pub(crate) enum EvaluatedCallee {
    Direct(SelectedValue),
    Member {
        receiver: Values,
        targets: Values,
        selection_dependencies: Option<Box<ProofDependencies>>,
        array_method: Option<Box<ArrayMethodSelection>>,
    },
}

impl EvaluatedCallee {
    pub(crate) fn targets(&self) -> &Values {
        match self {
            Self::Direct(targets) => &targets.values,
            Self::Member { targets, .. } => targets,
        }
    }

    pub(crate) fn receiver(&self) -> Option<&Values> {
        match self {
            Self::Direct(_) => None,
            Self::Member { receiver, .. } => Some(receiver),
        }
    }
}

/// One value produced by an adapter for the kernel: its allocation judgment,
/// the effects of producing it, and the callable it selects, if any.
pub(crate) struct LocalValueResult {
    pub(crate) allocation: AllocationValue,
    pub(crate) effects: EffectCoverageFragment,
    pub(crate) selection: Option<CallableSelection>,
}

/// A call whose callee and operands the kernel evaluated in source order. The
/// adapter performs the invocation and must not evaluate the operands again.
pub(crate) struct LocalCallRequest {
    pub(crate) callee: EvaluatedCallee,
    pub(crate) arguments: Vec<SelectedValue>,
    pub(crate) span: Span,
    pub(crate) invocation: Invocation,
}

/// Which internal method the request invokes on its evaluated callee.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Invocation {
    Call,
    Construct,
}

pub(crate) struct LocalTransferResult {
    pub(crate) ledger: TransferLedger<LocalNormal, KnownThrow>,
}

/// What remains to account for after a refusal. An adapter that summarized
/// the body's remainder when it refused reports `AlreadySummarized`; a kernel
/// or adapter refusal with no summary leaves that to the driver's caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RemainderDisposition {
    NeedsSummary,
    AlreadySummarized,
}

pub(crate) enum LocalExecutionAttempt {
    Completed(LocalTransferResult),
    Refused(RemainderDisposition),
}
