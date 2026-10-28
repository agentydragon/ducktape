//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::super::value_model::{CapturedContext, Context, ValueExpr, Values};
use super::Call;
use super::interpreter::{ExecutionInputs, ExecutionState, Executor, Outcome};
use crate::facts::statement_facts::SynchronousCalls;
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use std::cell::{Cell, RefCell};

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum RuntimeKey {
    Prefix(SourceUnitOrdinal),
    Effects(SourceUnitOrdinal),
    Request(ProgramPoint),
    Result(ProgramPoint),
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum RuntimeRule {
    ExecuteOwner(SourceUnitOrdinal),
    Transfer(ProgramPoint),
}

enum RuntimeFact {
    Prefix(Box<ExecutionState>),
    Effects(Box<SynchronousCalls>),
    Request(Box<RuntimeRequest>),
    Result(Box<RuntimeResult>),
}

/// The path identifies an occurrence within its parent's transfer, rather than
/// conflating repeated evaluation of the same expression in different contexts.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
struct ProgramPoint {
    owner: SourceUnitOrdinal,
    path: Vec<(OperationKind, usize)>,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum OperationKind {
    Evaluate,
    Invoke,
    Call,
}

pub(super) enum RuntimeOperation {
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
}

#[derive(Clone)]
pub(super) enum RuntimeValue {
    Values(Values),
    Outcome(Box<Outcome>),
}

struct RuntimeRequest {
    state: ExecutionState,
    operation: RuntimeOperation,
}

pub(super) struct RuntimeResult {
    pub(super) state: ExecutionState,
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
    pub(super) fn request(
        &self,
        state: ExecutionState,
        operation: RuntimeOperation,
    ) -> RuntimeResult {
        let kind = match &operation {
            RuntimeOperation::Evaluate { .. } => OperationKind::Evaluate,
            RuntimeOperation::Invoke { .. } => OperationKind::Invoke,
            RuntimeOperation::Call { .. } => OperationKind::Call,
        };
        let ordinal = self.next_child.get();
        self.next_child.set(ordinal + 1);
        let mut point = self.point.clone();
        point.path.push((kind, ordinal));
        let mut context = self.context.borrow_mut();
        context.propose(
            RuntimeKey::Request(point.clone()),
            RuntimeFact::Request(Box::new(RuntimeRequest { state, operation })),
        );
        RuntimeRules {
            inputs: self.inputs,
            owner_count: 0,
        }
        .transfer(&point, &mut **context);
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
            RuntimeKey::Prefix(_) | RuntimeKey::Effects(_) | RuntimeKey::Result(_) => Vec::new(),
        }
    }

    fn evaluate(
        &self,
        rule: &RuntimeRule,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        match rule {
            RuntimeRule::ExecuteOwner(owner) => self.execute_owner(*owner, context),
            RuntimeRule::Transfer(point) => self.transfer(point, context),
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
            )
        ));
        false
    }
}

impl RuntimeRules<'_, '_> {
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
        let mut executor = Executor::new(self.inputs, *state, queries);
        let effects = executor.execute_owner(owner);
        let state = executor.into_state();
        context.propose(
            RuntimeKey::Effects(owner),
            RuntimeFact::Effects(Box::new(effects)),
        );
        context.propose(
            RuntimeKey::Prefix(SourceUnitOrdinal(owner.0 + 1)),
            RuntimeFact::Prefix(Box::new(state)),
        );
    }

    fn transfer(
        &self,
        point: &ProgramPoint,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Request(request)) =
            context.take_final(RuntimeKey::Request(point.clone()))
        else {
            return;
        };
        let queries = RuntimeQueries {
            inputs: self.inputs,
            point: point.clone(),
            next_child: Cell::new(0),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(self.inputs, request.state, queries);
        let value = match &request.operation {
            RuntimeOperation::Evaluate {
                expression,
                environment,
                captures,
            } => {
                RuntimeValue::Values(executor.evaluate_transfer(expression, environment, captures))
            }
            RuntimeOperation::Invoke {
                callable,
                arguments,
                captured,
                fresh_checked_constructor,
            } => RuntimeValue::Outcome(Box::new(executor.invoke_transfer(
                *callable,
                arguments.clone(),
                captured.clone(),
                *fresh_checked_constructor,
            ))),
            RuntimeOperation::Call {
                call,
                environment,
                captures,
                outcome,
            } => {
                let mut outcome = outcome.clone();
                executor.execute_call_transfer(call, environment, captures, &mut outcome);
                RuntimeValue::Outcome(outcome)
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
