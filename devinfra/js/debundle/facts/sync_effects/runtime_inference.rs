//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::super::value_model::{CapturedContext, Context, ValueExpr, Values};
use super::Call;
use super::interpreter::{ExecutionInputs, ExecutionState, Executor, Outcome};
use crate::facts::statement_facts::SynchronousCalls;
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

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
    Prefix(Rc<ExecutionState>),
    Effects(Box<SynchronousCalls>),
    Request(Rc<RuntimeRequest>),
    Result(Rc<RuntimeResult>),
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
    Owner(SourceUnitOrdinal),
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
    Effects(Box<SynchronousCalls>),
    Values(Values),
    Outcome(Box<Outcome>),
}

struct RuntimeRequest {
    state: Rc<ExecutionState>,
    operation: RuntimeOperation,
}

pub(super) struct RuntimeResult {
    pub(super) state: Rc<ExecutionState>,
    pub(super) value: RuntimeValue,
}

/// Query access lasts for one transfer attempt. A missing child suspends that
/// attempt: no later request is admitted and none of its scratch state escapes.
/// A retry starts from the immutable input and reuses settled earlier children.
/// Invocation counts and revisions are consequently charged in semantic state,
/// independent of how often the worklist revisits the parent.
pub(super) struct RuntimeQueries<'query> {
    point: ProgramPoint,
    next_child: Cell<usize>,
    suspended: Cell<bool>,
    context: RefCell<&'query mut dyn FactContext<RuntimeKey, RuntimeFact>>,
}

impl RuntimeQueries<'_> {
    pub(super) fn suspended(&self) -> bool {
        self.suspended.get()
    }

    pub(super) fn request(
        &self,
        state: &ExecutionState,
        operation: RuntimeOperation,
    ) -> Option<Rc<RuntimeResult>> {
        if self.suspended() {
            return None;
        }
        let kind = match &operation {
            RuntimeOperation::Evaluate { .. } => OperationKind::Evaluate,
            RuntimeOperation::Invoke { .. } => OperationKind::Invoke,
            RuntimeOperation::Call { .. } => OperationKind::Call,
            RuntimeOperation::Owner(_) => unreachable!("owners are prefix roots"),
        };
        let ordinal = self.next_child.get();
        self.next_child.set(ordinal + 1);
        let mut point = self.point.clone();
        point.path.push((kind, ordinal));
        let mut context = self.context.borrow_mut();
        if let Some(RuntimeFact::Result(result)) = context.read(RuntimeKey::Result(point.clone())) {
            return Some(Rc::clone(result));
        }
        context.propose(
            RuntimeKey::Request(point),
            RuntimeFact::Request(Rc::new(RuntimeRequest {
                state: Rc::new(state.clone()),
                operation,
            })),
        );
        self.suspended.set(true);
        None
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
        let Some(RuntimeFact::Prefix(state)) = context.read(RuntimeKey::Prefix(owner)) else {
            return;
        };
        let state = Rc::clone(state);
        let point = ProgramPoint {
            owner,
            path: Vec::new(),
        };
        context.propose(
            RuntimeKey::Request(point.clone()),
            RuntimeFact::Request(Rc::new(RuntimeRequest {
                state,
                operation: RuntimeOperation::Owner(owner),
            })),
        );
        let Some(RuntimeFact::Result(result)) = context.read(RuntimeKey::Result(point)) else {
            return;
        };
        let result = Rc::clone(result);
        let RuntimeValue::Effects(effects) = &result.value else {
            unreachable!("owner transfers produce synchronous effects");
        };
        context.propose(
            RuntimeKey::Effects(owner),
            RuntimeFact::Effects(effects.clone()),
        );
        context.propose(
            RuntimeKey::Prefix(SourceUnitOrdinal(owner.0 + 1)),
            RuntimeFact::Prefix(Rc::clone(&result.state)),
        );
    }

    fn transfer(
        &self,
        point: &ProgramPoint,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Request(request)) = context.read(RuntimeKey::Request(point.clone()))
        else {
            return;
        };
        let request = Rc::clone(request);
        if let Some(RuntimeFact::Result(result)) = context.read(RuntimeKey::Result(point.clone())) {
            // The operation is immutable. Forward newly grown request/proof
            // dependencies without spending another semantic expansion.
            let result = Rc::clone(result);
            context.propose(
                RuntimeKey::Result(point.clone()),
                RuntimeFact::Result(result),
            );
            return;
        }
        let queries = RuntimeQueries {
            point: point.clone(),
            next_child: Cell::new(0),
            suspended: Cell::new(false),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(self.inputs, request.state.as_ref().clone(), queries);
        let value = match &request.operation {
            RuntimeOperation::Owner(owner) => {
                RuntimeValue::Effects(Box::new(executor.execute_owner(*owner)))
            }
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
        let Some(state) = executor.into_settled_state() else {
            return;
        };
        context.propose(
            RuntimeKey::Result(point.clone()),
            RuntimeFact::Result(Rc::new(RuntimeResult {
                state: Rc::new(state),
                value,
            })),
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
        RuntimeFact::Prefix(Rc::new(initial)),
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
