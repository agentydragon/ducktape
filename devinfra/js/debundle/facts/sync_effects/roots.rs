//! One source owner's execution, as named phases: operands, body,
//! assignment and export, finalization. Each phase states in its
//! signature what it may mutate and returns what it observed.

mod assignment;
mod body;
mod finalize;
mod operands;

use super::interpreter::Executor;
use crate::facts::statement_facts::SynchronousCalls;
use source_identity::SourceUnitOrdinal;

impl Executor<'_, '_> {
    pub(super) fn execute_owner(&mut self, owner: SourceUnitOrdinal) -> SynchronousCalls {
        let mut root = operands::prepare(self, owner);
        let body = body::execute(self, &mut root);
        let assigned = assignment::assign(self, &mut root, body);
        let Executor {
            inputs,
            state,
            observations,
            queries,
            evaluation_effects,
            ..
        } = self;
        finalize::finalize(
            inputs,
            state,
            observations,
            queries,
            evaluation_effects,
            root,
            assigned,
        )
    }
}
