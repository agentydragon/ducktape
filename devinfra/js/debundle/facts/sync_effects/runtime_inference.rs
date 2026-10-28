//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::interpreter::{ExecutionInputs, ExecutionState, Executor};
use crate::facts::statement_facts::SynchronousCalls;
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use std::rc::Rc;

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum RuntimeKey {
    Prefix(SourceUnitOrdinal),
    Effects(SourceUnitOrdinal),
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum RuntimeRule {
    ExecuteOwner(SourceUnitOrdinal),
}

enum RuntimeFact {
    Prefix(Rc<ExecutionState>),
    Effects(Box<SynchronousCalls>),
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
            RuntimeKey::Prefix(_) | RuntimeKey::Effects(_) => Vec::new(),
        }
    }

    fn evaluate(
        &self,
        rule: &RuntimeRule,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let RuntimeRule::ExecuteOwner(owner) = rule;
        let Some(RuntimeFact::Prefix(state)) = context.read(RuntimeKey::Prefix(*owner)) else {
            return;
        };
        let mut executor = Executor::new(self.inputs, state.as_ref().clone());
        let effects = executor.execute_owner(*owner);
        let state = executor.into_state();
        context.propose(
            RuntimeKey::Effects(*owner),
            RuntimeFact::Effects(Box::new(effects)),
        );
        context.propose(
            RuntimeKey::Prefix(SourceUnitOrdinal(owner.0 + 1)),
            RuntimeFact::Prefix(Rc::new(state)),
        );
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
            )
        ));
        false
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
