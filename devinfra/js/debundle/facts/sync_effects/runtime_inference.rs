//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::super::property_inference::OwnPropertyProof;
use super::super::value_model::{CapturedContext, Context, ValueExpr, Values};
use super::Call;
use super::interpreter::{ExecutionInputs, ExecutionState, Executor, Outcome, OwnPropertyQuery};
use super::runtime_callbacks::{self, CallbackOperation, CallbackResult};
use super::runtime_heap::{HeapOperation, HeapResult};
use super::runtime_models::{self, ModelOperation, ModelResult};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::statement_facts::SynchronousCalls;
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::reports::schema::{NativeProgramPointKind, NativeProgramPointStep};
use source_identity::SourceUnitOrdinal;
use std::cell::{Cell, RefCell};

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum RuntimeKey {
    Prefix(SourceUnitOrdinal),
    Effects(SourceUnitOrdinal),
    Request(ProgramPoint),
    Result(ProgramPoint),
    OwnPropertyRequest(ProgramPoint),
    OwnPropertyResult(ProgramPoint),
    InvocationMemo(ProgramPoint),
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum RuntimeRule {
    ExecuteOwner(SourceUnitOrdinal),
    Transfer(ProgramPoint),
    OwnProperty(ProgramPoint),
}

enum RuntimeFact {
    Prefix(Box<ExecutionState>),
    Effects(Box<SynchronousCalls>),
    Request(Box<RuntimeRequest>),
    Result(Box<RuntimeResult>),
    OwnPropertyRequest(Box<OwnPropertyQuery>),
    OwnPropertyResult(Option<Box<OwnPropertyProof>>),
    InvocationMemo(Box<Outcome>),
}

/// The path identifies an occurrence within its parent's transfer, rather than
/// conflating repeated evaluation of the same expression in different contexts.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct ProgramPoint {
    owner: SourceUnitOrdinal,
    path: Vec<(NativeProgramPointKind, usize)>,
}

pub(super) enum RuntimeOperation {
    Heap(HeapOperation),
    Evaluate {
        expression: ValueExpr,
        environment: Context,
        captures: Context,
    },
    Invoke {
        callable: u32,
        arguments: Vec<Values>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    },
    Call {
        call: Call,
        environment: Context,
        captures: Context,
        outcome: Box<Outcome>,
    },
    Callbacks(CallbackOperation),
    Model(ModelOperation),
}

pub(super) enum RuntimeValue {
    Heap(HeapResult),
    Values(Values),
    Outcome(Box<Outcome>),
    Callbacks(CallbackResult),
    Model(ModelResult),
}

struct RuntimeRequest {
    state: Box<ExecutionState>,
    operation: RuntimeOperation,
}

pub(super) struct RuntimeResult {
    pub(super) state: Box<ExecutionState>,
    pub(super) value: RuntimeValue,
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

    pub(super) fn assume(&self, dependencies: &ProofDependencies) {
        self.context.borrow_mut().assume(dependencies);
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
        proof.map(|proof| *proof)
    }

    pub(super) fn request(
        &self,
        state: Box<ExecutionState>,
        operation: RuntimeOperation,
    ) -> RuntimeResult {
        let kind = match &operation {
            RuntimeOperation::Heap(_) => NativeProgramPointKind::Heap,
            RuntimeOperation::Evaluate { .. } => NativeProgramPointKind::Evaluate,
            RuntimeOperation::Invoke { .. } => NativeProgramPointKind::Invoke,
            RuntimeOperation::Call { .. } => NativeProgramPointKind::Call,
            RuntimeOperation::Callbacks(_) => NativeProgramPointKind::Callbacks,
            RuntimeOperation::Model(_) => NativeProgramPointKind::Model,
        };
        let point = self.child_point(kind);
        let mut context = self.context.borrow_mut();
        context.propose(
            RuntimeKey::Request(point.clone()),
            RuntimeFact::Request(Box::new(RuntimeRequest { state, operation })),
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
        let queries = RuntimeQueries {
            inputs: self.inputs,
            point: ProgramPoint {
                owner,
                path: Vec::new(),
            },
            next_child: Cell::new(0),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(self.inputs, state, queries);
        let effects = executor.execute_owner(owner);
        let state = executor.into_state();
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
        let RuntimeRequest { state, operation } = *request;
        let queries = RuntimeQueries {
            inputs,
            point: point.clone(),
            next_child: Cell::new(0),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(inputs, state, queries);
        let value = match operation {
            RuntimeOperation::Heap(operation) => {
                RuntimeValue::Heap(operation.execute(&mut executor))
            }
            RuntimeOperation::Evaluate {
                expression,
                environment,
                captures,
            } => RuntimeValue::Values(executor.evaluate_transfer(
                &expression,
                &environment,
                &captures,
            )),
            RuntimeOperation::Invoke {
                callable,
                arguments,
                captured,
                fresh_checked_constructor,
            } => RuntimeValue::Outcome(Box::new(executor.invoke_transfer(
                callable,
                arguments,
                captured,
                fresh_checked_constructor,
            ))),
            RuntimeOperation::Call {
                call,
                environment,
                captures,
                mut outcome,
            } => {
                executor.execute_call_transfer(&call, &environment, &captures, &mut outcome);
                RuntimeValue::Outcome(outcome)
            }
            RuntimeOperation::Model(operation) => {
                RuntimeValue::Model(runtime_models::dispatch(&mut executor, operation))
            }
            RuntimeOperation::Callbacks(operation) => {
                RuntimeValue::Callbacks(runtime_callbacks::dispatch(&mut executor, operation))
            }
        };
        let state = executor.into_state();
        context.propose(
            RuntimeKey::Result(point.clone()),
            RuntimeFact::Result(Box::new(RuntimeResult { state, value })),
        );
    }
}

pub(super) fn infer_synchronous_execution(
    inputs: &ExecutionInputs<'_>,
    initial: ExecutionState,
    owner_count: usize,
) -> Vec<SynchronousCalls> {
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
        .map(
            |ordinal| match facts.remove(&RuntimeKey::Effects(SourceUnitOrdinal(ordinal))) {
                Some(RuntimeFact::Effects(effects)) => *effects,
                _ => unreachable!("every source owner has a settled execution result"),
            },
        )
        .collect()
}
