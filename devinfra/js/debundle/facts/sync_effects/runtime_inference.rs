//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::allocation_transfer::AllocationJudgment;
use super::effect_coverage::EffectCoverageFragment;
use super::execution_index::Call;
use super::interpreter::{
    ExecutionInputs, ExecutionState, Executor, Observations, Outcome, OwnPropertyQuery,
};
use super::kernel::frame::{CallableSelection, EvaluatedCallee};
use super::noninvoking_initialization::{EvaluatedInitializer, OriginalInitializer};
use super::property_inference::OwnPropertyProof;
use super::runtime_callbacks::{self, CallbackOperation, CallbackResult};
use super::runtime_heap::{HeapOperation, HeapResult};
use super::runtime_models::{self, ModelOperation, ModelResult};
use super::value_model::{CapturedContext, Context, ValueExpr, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedCallAdmissionRefusal;
use crate::facts::evaluation_effects::EvaluationEffects;
use crate::facts::statement_facts::SynchronousCalls;
use crate::facts::{
    CompletedPurity, LocalRecordFactoryPurityCertificate, OwnerInitializationCoverageRefusalReason,
    PreparedPurity,
};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::reports::schema::{NativeProgramPointKind, NativeProgramPointStep};
use source_identity::SourceUnitOrdinal;
use std::cell::{Cell, RefCell};

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum RuntimeKey {
    Prefix(SourceUnitOrdinal),
    /// Current-owner preparatory classifier obligations at this prefix.
    PreparedPurity(SourceUnitOrdinal),
    /// Final verdict published only after every owner finalization step.
    CompletedPurity(SourceUnitOrdinal),
    Effects(SourceUnitOrdinal),
    Request(ProgramPoint),
    Result(ProgramPoint),
    OwnPropertyRequest(ProgramPoint),
    OwnPropertyResult(ProgramPoint),
    InvocationMemo(ProgramPoint),
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum RuntimeRule {
    ExecuteOwner(SourceUnitOrdinal),
    Transfer(ProgramPoint),
    OwnProperty(ProgramPoint),
}

enum RuntimeFact {
    Prefix(Box<ExecutionState>),
    PreparedPurity(Box<PreparedPurity>),
    CompletedPurity(Box<CompletedPurity>),
    Effects(Box<SynchronousCalls>),
    Request(Box<RuntimeRequest>),
    Result(Box<RuntimeResult>),
    OwnPropertyRequest(Box<OwnPropertyQuery>),
    OwnPropertyResult(Option<Box<OwnPropertyProof>>),
    InvocationMemo(Box<Outcome>),
}

/// The path identifies an occurrence within its parent's transfer, rather than
/// conflating repeated evaluation of the same expression in different contexts.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct ProgramPoint {
    owner: SourceUnitOrdinal,
    path: Vec<(NativeProgramPointKind, usize)>,
}

/// The ordered caller has already read its callee and arguments. The call
/// transfer must use these snapshots rather than evaluate their source again.
pub(super) struct CallOperands {
    pub(super) callee: EvaluatedCallee,
    pub(super) arguments: Vec<Values>,
    pub(super) argument_selections: Vec<Option<CallableSelection>>,
    /// The exact root has independent pure-initializer and operation inputs.
    /// This only admits an existing callback-factory result transfer; the
    /// ordered caller still checks the operation domains.
    pub(super) selected_pure_initializer_call: bool,
}

pub(super) enum CallTransfer {
    /// Consult the accompanying outcome for effect completeness. No return
    /// value was requested, or the selected proof supplies only effects.
    EffectsOnly,
    Values(Values),
    /// The ordered operand path declined this target. Its source-based
    /// helpers cannot safely replay already evaluated operands.
    UnsupportedOperands(OrderedCallAdmissionRefusal),
}

impl CallTransfer {
    pub(super) fn requested(needs_value: bool, values: impl FnOnce() -> Values) -> Self {
        if needs_value {
            Self::Values(values())
        } else {
            Self::EffectsOnly
        }
    }
}

pub(super) enum RuntimeOperation {
    Heap(HeapOperation),
    Evaluate {
        expression: ValueExpr,
        environment: Context,
        captures: Context,
        source_covered: bool,
    },
    EvaluateInitializer {
        source: OriginalInitializer,
        expression: ValueExpr,
        environment: Context,
        captures: Context,
    },
    Invoke {
        callable: u32,
        arguments: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    },
    Call {
        call: Call,
        environment: Context,
        captures: Context,
        outcome: Box<Outcome>,
        operands: Option<Box<CallOperands>>,
    },
    Callbacks(CallbackOperation),
    Model(ModelOperation),
}

pub(super) enum RuntimeValue {
    Heap(HeapResult),
    Values(Values),
    Initializer {
        values: Values,
        evaluated: Option<Box<EvaluatedInitializer>>,
        operation_coverage: Option<Box<EffectCoverageFragment>>,
        refusal: Option<OwnerInitializationCoverageRefusalReason>,
    },
    Outcome(Box<Outcome>),
    Call {
        outcome: Box<Outcome>,
        transfer: CallTransfer,
    },
    Callbacks(CallbackResult),
    Model(ModelResult),
}

struct RuntimeRequest {
    state: Box<ExecutionState>,
    /// The parent's run-level admission facts so far; see [`Observations`].
    observations: Observations,
    operation: RuntimeOperation,
}

#[must_use]
pub(super) struct RuntimeResult {
    pub(super) state: Box<ExecutionState>,
    pub(super) observations: Observations,
    pub(super) value: RuntimeValue,
    pub(super) allocation_judgment: AllocationJudgment,
    pub(super) evaluation_effects: EvaluationEffects,
}

/// Transfers consume settled inputs and cannot send information back to their
/// predecessors. Evaluate these acyclic children immediately on the same fact
/// context, then retire their single-consumer payloads. Growing fact families
/// continue to use the dependency worklist. This avoids replaying a parent or
/// retaining every heap snapshot for a long, flat sequence of source calls.
pub(super) struct RuntimeQueries<'source, 'query> {
    inputs: &'query ExecutionInputs<'source>,
    point: ProgramPoint,
    next_child: Cell<usize>,
    dependencies: RefCell<ProofDependencies>,
    context: RefCell<&'query mut dyn FactContext<RuntimeKey, RuntimeFact>>,
}

impl RuntimeQueries<'_, '_> {
    pub(super) fn point_path(&self) -> Vec<NativeProgramPointStep> {
        self.point
            .path
            .iter()
            .map(|(kind, ordinal)| NativeProgramPointStep {
                kind: *kind,
                ordinal: *ordinal,
            })
            .collect()
    }
    pub(super) fn invocation_memo(&self, point: &ProgramPoint) -> Outcome {
        let mut context = self.context.borrow_mut();
        let Some(RuntimeFact::InvocationMemo(outcome)) =
            context.read_final(RuntimeKey::InvocationMemo(point.clone()))
        else {
            unreachable!("a live invocation memo points to its completed outcome");
        };
        outcome.as_ref().clone()
    }

    pub(super) fn publish_invocation_memo(&self, outcome: &Outcome) -> ProgramPoint {
        self.context.borrow_mut().propose(
            RuntimeKey::InvocationMemo(self.point.clone()),
            RuntimeFact::InvocationMemo(Box::new(outcome.clone())),
        );
        self.point.clone()
    }

    pub(super) fn retire_invocation_memo(&self, point: ProgramPoint) {
        assert!(matches!(
            self.context
                .borrow_mut()
                .take_final(RuntimeKey::InvocationMemo(point)),
            Some(RuntimeFact::InvocationMemo(_))
        ));
    }

    /// Current-owner proof preparation always consumes the unchanged classifier.
    pub(super) fn prepared_statement_is_pure(&self, owner: SourceUnitOrdinal) -> bool {
        assert_eq!(
            owner, self.point.owner,
            "prepared purity is current-owner only"
        );
        match self
            .context
            .borrow_mut()
            .read_final(RuntimeKey::PreparedPurity(owner))
        {
            Some(RuntimeFact::PreparedPurity(purity)) => {
                self.dependencies
                    .borrow_mut()
                    .extend(&purity.proof_dependencies());
                purity.is_pure()
            }
            _ => unreachable!("prepared purity precedes current-owner execution"),
        }
    }

    /// Earlier owners, and the finalized current owner's frontier, consume
    /// only the verdict published after all evaluation/certificate work.
    pub(super) fn completed_statement_is_pure(&self, owner: SourceUnitOrdinal) -> bool {
        assert!(owner <= self.point.owner);
        match self
            .context
            .borrow_mut()
            .read_final(RuntimeKey::CompletedPurity(owner))
        {
            Some(RuntimeFact::CompletedPurity(purity)) => {
                self.dependencies
                    .borrow_mut()
                    .extend(purity.proof_dependencies());
                purity.is_pure()
            }
            _ => unreachable!("completed purity requires a finalized owner"),
        }
    }

    pub(super) fn complete_owner_purity(
        &self,
        owner: SourceUnitOrdinal,
        certificate: Option<&LocalRecordFactoryPurityCertificate>,
    ) -> Option<crate::reports::schema::PurityCompletionDisagreement> {
        assert_eq!(owner, self.point.owner);
        let mut context = self.context.borrow_mut();
        let Some(RuntimeFact::PreparedPurity(prepared)) =
            context.take_final(RuntimeKey::PreparedPurity(owner))
        else {
            unreachable!("owner finalization consumes its prepared obligations once");
        };
        let (completed, disagreement) = prepared.complete(certificate);
        context.assume(completed.proof_dependencies());
        context.propose(
            RuntimeKey::CompletedPurity(owner),
            RuntimeFact::CompletedPurity(Box::new(completed)),
        );
        disagreement
    }

    pub(super) fn assume(&self, dependencies: &ProofDependencies) {
        self.dependencies.borrow_mut().extend(dependencies);
        self.context.borrow_mut().assume(dependencies);
    }

    pub(super) fn dependencies(&self) -> ProofDependencies {
        self.dependencies.borrow().clone()
    }

    pub(super) fn extend_dependencies(&self, dependencies: &mut ProofDependencies) {
        dependencies.extend(&self.dependencies.borrow());
    }

    fn child_point(&self, kind: NativeProgramPointKind) -> ProgramPoint {
        let ordinal = self.next_child.get();
        self.next_child.set(ordinal + 1);
        let mut point = self.point.clone();
        point.path.push((kind, ordinal));
        point
    }

    pub(super) fn project_own_property(&self, query: OwnPropertyQuery) -> Option<OwnPropertyProof> {
        let point = self.child_point(NativeProgramPointKind::OwnProperty);
        let mut context = self.context.borrow_mut();
        context.propose(
            RuntimeKey::OwnPropertyRequest(point.clone()),
            RuntimeFact::OwnPropertyRequest(Box::new(query)),
        );
        RuntimeRules::project_own_property(self.inputs, &point, &mut **context);
        let Some(RuntimeFact::OwnPropertyResult(proof)) =
            context.take_final(RuntimeKey::OwnPropertyResult(point))
        else {
            unreachable!("a settled property query produces a terminal optional proof");
        };
        if let Some(proof) = &proof {
            self.dependencies.borrow_mut().extend(&proof.dependencies);
        }
        proof.map(|proof| *proof)
    }

    pub(super) fn request(
        &self,
        state: Box<ExecutionState>,
        observations: Observations,
        operation: RuntimeOperation,
    ) -> RuntimeResult {
        let kind = match &operation {
            RuntimeOperation::Heap(_) => NativeProgramPointKind::Heap,
            RuntimeOperation::Evaluate { .. } | RuntimeOperation::EvaluateInitializer { .. } => {
                NativeProgramPointKind::Evaluate
            }
            RuntimeOperation::Invoke { .. } => NativeProgramPointKind::Invoke,
            RuntimeOperation::Call { .. } => NativeProgramPointKind::Call,
            RuntimeOperation::Callbacks(_) => NativeProgramPointKind::Callbacks,
            RuntimeOperation::Model(_) => NativeProgramPointKind::Model,
        };
        let point = self.child_point(kind);
        let mut context = self.context.borrow_mut();
        context.propose(
            RuntimeKey::Request(point.clone()),
            RuntimeFact::Request(Box::new(RuntimeRequest {
                state,
                observations,
                operation,
            })),
        );
        RuntimeRules::transfer(self.inputs, &point, &mut **context);
        let Some(RuntimeFact::Result(result)) = context.take_final(RuntimeKey::Result(point))
        else {
            unreachable!("a transfer with settled inputs produces a terminal result");
        };
        *result
    }
}

struct RuntimeRules<'source, 'inputs> {
    inputs: &'inputs ExecutionInputs<'source>,
    owner_count: usize,
}

impl IterativeRules for RuntimeRules<'_, '_> {
    type Key = RuntimeKey;
    type Fact = RuntimeFact;
    type Rule = RuntimeRule;

    fn producers_for(&self, key: &RuntimeKey, _: &RuntimeFact) -> Vec<RuntimeRule> {
        match key {
            RuntimeKey::Prefix(owner) if owner.0 < self.owner_count => {
                vec![RuntimeRule::ExecuteOwner(*owner)]
            }
            RuntimeKey::Request(point) => vec![RuntimeRule::Transfer(point.clone())],
            RuntimeKey::OwnPropertyRequest(point) => vec![RuntimeRule::OwnProperty(point.clone())],
            RuntimeKey::Prefix(_)
            | RuntimeKey::PreparedPurity(_)
            | RuntimeKey::CompletedPurity(_)
            | RuntimeKey::Effects(_)
            | RuntimeKey::Result(_)
            | RuntimeKey::OwnPropertyResult(_)
            | RuntimeKey::InvocationMemo(_) => Vec::new(),
        }
    }

    fn evaluate(
        &self,
        rule: &RuntimeRule,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        match rule {
            RuntimeRule::ExecuteOwner(owner) => self.execute_owner(*owner, context),
            RuntimeRule::Transfer(point) => Self::transfer(self.inputs, point, context),
            RuntimeRule::OwnProperty(point) => {
                Self::project_own_property(self.inputs, point, context)
            }
        }
    }

    fn merge(key: &RuntimeKey, current: &mut RuntimeFact, contribution: RuntimeFact) -> bool {
        // Each key has one producer operating on an immutable predecessor.
        // Re-evaluation can add proof dependencies, never a different state.
        assert!(matches!(
            (key, current, contribution),
            (
                RuntimeKey::Prefix(_),
                RuntimeFact::Prefix(_),
                RuntimeFact::Prefix(_)
            ) | (
                RuntimeKey::PreparedPurity(_),
                RuntimeFact::PreparedPurity(_),
                RuntimeFact::PreparedPurity(_)
            ) | (
                RuntimeKey::CompletedPurity(_),
                RuntimeFact::CompletedPurity(_),
                RuntimeFact::CompletedPurity(_)
            ) | (
                RuntimeKey::Effects(_),
                RuntimeFact::Effects(_),
                RuntimeFact::Effects(_)
            ) | (
                RuntimeKey::Request(_),
                RuntimeFact::Request(_),
                RuntimeFact::Request(_)
            ) | (
                RuntimeKey::Result(_),
                RuntimeFact::Result(_),
                RuntimeFact::Result(_)
            ) | (
                RuntimeKey::OwnPropertyRequest(_),
                RuntimeFact::OwnPropertyRequest(_),
                RuntimeFact::OwnPropertyRequest(_)
            ) | (
                RuntimeKey::OwnPropertyResult(_),
                RuntimeFact::OwnPropertyResult(_),
                RuntimeFact::OwnPropertyResult(_)
            ) | (
                RuntimeKey::InvocationMemo(_),
                RuntimeFact::InvocationMemo(_),
                RuntimeFact::InvocationMemo(_)
            )
        ));
        false
    }
}

impl RuntimeRules<'_, '_> {
    fn project_own_property(
        inputs: &ExecutionInputs<'_>,
        point: &ProgramPoint,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::OwnPropertyRequest(query)) =
            context.take_final(RuntimeKey::OwnPropertyRequest(point.clone()))
        else {
            return;
        };
        let proof = inputs.project_own_property(*query);
        if let Some(proof) = &proof {
            context.assume(&proof.dependencies);
        }
        context.propose(
            RuntimeKey::OwnPropertyResult(point.clone()),
            RuntimeFact::OwnPropertyResult(proof.map(Box::new)),
        );
    }

    fn execute_owner(
        &self,
        owner: SourceUnitOrdinal,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Prefix(state)) = context.take_final(RuntimeKey::Prefix(owner)) else {
            return;
        };
        // Preparatory classifier obligations remain exactly the current
        // source/prefix transfer. Finalization publishes a separate verdict.
        context.propose(
            RuntimeKey::PreparedPurity(owner),
            RuntimeFact::PreparedPurity(Box::new(PreparedPurity::new(
                self.inputs.statement_effects(owner, &state),
            ))),
        );
        let queries = RuntimeQueries {
            inputs: self.inputs,
            point: ProgramPoint {
                owner,
                path: Vec::new(),
            },
            next_child: Cell::new(0),
            dependencies: RefCell::default(),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(self.inputs, state, Observations::default(), queries);
        let effects = executor.execute_owner(owner);
        executor.mark_allocation_opaque();
        let (mut state, _, _, _) = executor.into_transfer();
        // Requests/results are taken at their single-consumer sites, and
        // owner finalization retires invocation memos. No active transfer
        // frame can hold a cell reference outside this settled prefix now.
        state.reclaim_lexical_cells(owner.0 + 1 == self.owner_count);
        context.propose(
            RuntimeKey::Effects(owner),
            RuntimeFact::Effects(Box::new(effects)),
        );
        context.propose(
            RuntimeKey::Prefix(SourceUnitOrdinal(owner.0 + 1)),
            RuntimeFact::Prefix(state),
        );
    }

    fn transfer(
        inputs: &ExecutionInputs<'_>,
        point: &ProgramPoint,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Request(request)) =
            context.take_final(RuntimeKey::Request(point.clone()))
        else {
            return;
        };
        let RuntimeRequest {
            state,
            observations,
            operation,
        } = *request;
        let queries = RuntimeQueries {
            inputs,
            point: point.clone(),
            next_child: Cell::new(0),
            dependencies: RefCell::default(),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(inputs, state, observations, queries);
        if !matches!(
            &operation,
            RuntimeOperation::Evaluate { .. } | RuntimeOperation::EvaluateInitializer { .. }
        ) {
            executor.mark_allocation_opaque();
        }
        let value = match operation {
            RuntimeOperation::Heap(operation) => {
                RuntimeValue::Heap(operation.execute(&mut executor))
            }
            RuntimeOperation::Evaluate {
                expression,
                environment,
                captures,
                source_covered,
            } => {
                if source_covered {
                    executor.cover_source_expression();
                }
                RuntimeValue::Values(executor.evaluate_transfer(
                    &expression,
                    &environment,
                    &captures,
                ))
            }
            RuntimeOperation::EvaluateInitializer {
                source,
                expression,
                environment,
                captures,
            } => {
                let (values, judgment, native_constructor_transfer, operation_coverage) = executor
                    .evaluate_initializer_transfer(&source, &expression, &environment, &captures);
                let operation_source = source.operation_source(&expression).is_some();
                let evaluated = if operation_source {
                    None
                } else {
                    source.evaluated(&expression, &values, &judgment, native_constructor_transfer)
                };
                let covered_operation = operation_source
                    && !values.is_empty()
                    && !values.contains(&super::value_model::AbstractValue::Value(
                        crate::facts::abstract_domain::value::Value::Unavailable,
                    ))
                    && operation_coverage
                        .as_ref()
                        .and_then(EffectCoverageFragment::normal_dependencies)
                        .is_some();
                let refusal = (evaluated.is_none() && !covered_operation)
                    .then_some(OwnerInitializationCoverageRefusalReason::InitializerResultUnproved);
                RuntimeValue::Initializer {
                    values,
                    evaluated: evaluated.map(Box::new),
                    operation_coverage: operation_coverage
                        .filter(|_| covered_operation)
                        .map(Box::new),
                    refusal,
                }
            }
            RuntimeOperation::Invoke {
                callable,
                arguments,
                argument_selections,
                captured,
                fresh_checked_constructor,
            } => RuntimeValue::Outcome(Box::new(executor.invoke_transfer(
                callable,
                arguments,
                argument_selections,
                captured,
                fresh_checked_constructor,
            ))),
            RuntimeOperation::Call {
                call,
                environment,
                captures,
                mut outcome,
                operands,
            } => {
                let transfer = executor.execute_call_transfer(
                    &call,
                    &environment,
                    &captures,
                    &mut outcome,
                    operands.map(|operands| *operands),
                );
                RuntimeValue::Call { outcome, transfer }
            }
            RuntimeOperation::Model(operation) => {
                RuntimeValue::Model(runtime_models::dispatch(&mut executor, operation))
            }
            RuntimeOperation::Callbacks(operation) => {
                RuntimeValue::Callbacks(runtime_callbacks::dispatch(&mut executor, operation))
            }
        };
        let (state, observations, allocation_judgment, evaluation_effects) =
            executor.into_transfer();
        context.propose(
            RuntimeKey::Result(point.clone()),
            RuntimeFact::Result(Box::new(RuntimeResult {
                state,
                observations,
                value,
                allocation_judgment,
                evaluation_effects,
            })),
        );
    }
}

pub(super) fn infer_synchronous_execution(
    inputs: &ExecutionInputs<'_>,
    initial: ExecutionState,
    owner_count: usize,
) -> Vec<(SynchronousCalls, CompletedPurity)> {
    let mut inference = IterativeInference::new(RuntimeRules {
        inputs,
        owner_count,
    });
    inference.seed(
        RuntimeKey::Prefix(SourceUnitOrdinal(0)),
        RuntimeFact::Prefix(Box::new(initial)),
    );
    let mut facts = inference.solve([]).into_values();
    (0..owner_count)
        .map(|ordinal| {
            let owner = SourceUnitOrdinal(ordinal);
            match (
                facts.remove(&RuntimeKey::Effects(owner)),
                facts.remove(&RuntimeKey::CompletedPurity(owner)),
            ) {
                (Some(RuntimeFact::Effects(calls)), Some(RuntimeFact::CompletedPurity(purity))) => {
                    (*calls, *purity)
                }
                _ => unreachable!("every source owner has a completed execution and purity result"),
            }
        })
        .collect()
}
