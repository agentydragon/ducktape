//! A bounded, read-only use of the shared kernel at an execution depth loss.

use super::{
    AbstractValue, Context, EvaluatedCallee, Executor, Kind, LocalBody, LocalBodyIdentity,
    LocalBodyInputs, LocalCallReceipt, LocalExecutionContext, LocalExecutionMode,
    LocalExecutionScope, LocalExpression, LocalOperation, LocalValueResult,
    MAX_SYNCHRONOUS_INVOCATIONS, Outcome, PrimKinds, SelectedValue, Span, Value, ValueExpr, Values,
};
use crate::facts::sync_effects::local_operations::{LocalCall, LocalCallee, LocalInvocation};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Default)]
pub(super) struct RetainedClosureQuery {
    completed: BTreeMap<u32, Outcome>,
    active: BTreeSet<u32>,
    requested: Option<u32>,
}

pub(super) fn primitive_return_envelope(values: &Values) -> bool {
    !values.is_empty()
        && values.iter().all(
            |value| matches!(value, AbstractValue::Value(Value::Prim(kinds)) if !kinds.is_empty()),
        )
}

/// Syntax admission keeps the proof query from mutating the prefix or creating
/// activation cells. Shared transfers still check operator completion, every
/// effect domain and the returned capability envelope.
fn terminal_read_only_body(body: &LocalBody) -> bool {
    if !body.bindings.is_empty() || !body.parameters.is_empty() {
        return false;
    }
    let [LocalOperation::Return(Some(expression), _)] = body.operations.as_ref() else {
        return false;
    };
    read_only_expression(expression)
}

fn read_only_expression(expression: &LocalExpression) -> bool {
    match expression {
        LocalExpression::PrimitiveLiteral(_) | LocalExpression::Value(ValueExpr::Binding(_)) => {
            true
        }
        LocalExpression::Binary { left, right, .. } => {
            read_only_expression(left) && read_only_expression(right)
        }
        LocalExpression::Sequence(expressions) => expressions.iter().all(read_only_expression),
        LocalExpression::Call(LocalCall {
            invocation: LocalInvocation::Call,
            callee: LocalCallee::Binding(_),
            arguments,
            ..
        }) => arguments.is_empty(),
        _ => false,
    }
}

impl Executor<'_, '_> {
    pub(super) fn retained_invocation_effects(&mut self, key: u32) -> Option<Outcome> {
        if !self.inputs.index.dynamic_scope_owners.is_empty() {
            return None;
        }
        let mut query = RetainedClosureQuery::default();
        let mut pending = vec![key];
        query.active.insert(key);
        while let Some(&current) = pending.last() {
            if query.active.len() + query.completed.len() > MAX_SYNCHRONOUS_INVOCATIONS
                || self.state.tainted_functions.contains(&current)
                || self
                    .state
                    .active
                    .iter()
                    .any(|active| active.callable == current)
            {
                return None;
            }
            let callable = self.inputs.index.callables.get(&current)?;
            if !callable.params.is_empty()
                || callable.implicit_capture
                || callable.generator
                || callable.opaque
                || self.inputs.index.async_callables.contains(&current)
            {
                return None;
            }
            let body = callable.body_facts.ordered_plan()?;
            if !terminal_read_only_body(body) {
                return None;
            }
            let owner = callable.owner;
            query.requested = None;
            let mut outcome = self.execute_local_body_mode(
                body,
                Context::new(),
                LocalBodyInputs::default(),
                owner,
                LocalExecutionScope::Callable(current),
                LocalExecutionMode::Retained(&mut query),
            );
            if let Some(child) = query.requested.take() {
                if !query.active.insert(child) {
                    return None;
                }
                pending.push(child);
                continue;
            }
            if !outcome.complete
                || outcome.unknown_call
                || !outcome.effects.uninitialized_reads.is_empty()
                || !outcome.effects.precision_losses.is_empty()
                || !outcome.effect_coverage.has_closed_effects()
                || !primitive_return_envelope(&outcome.returns)
            {
                return None;
            }
            // A primitive envelope cannot hide a returned callback or heap
            // capability. Keep its value unknown even when this proof query
            // happened to resolve a narrower primitive type.
            outcome.returns = Values::from([AbstractValue::Value(Value::Prim(PrimKinds::ANY))]);
            outcome.return_selection = None;
            outcome
                .effect_coverage
                .depend_on(&self.settled_prefix_dependencies());
            query.completed.insert(current, outcome);
            query.active.remove(&current);
            pending.pop();
        }
        query.completed.remove(&key)
    }
}

impl LocalExecutionContext<'_, '_, '_> {
    pub(super) fn retained_call(
        &mut self,
        callee: &EvaluatedCallee,
        arguments: &[SelectedValue],
        span: Span,
    ) -> Option<LocalValueResult> {
        if !arguments.is_empty() {
            return None;
        }
        let EvaluatedCallee::Direct(selected) = callee else {
            return None;
        };
        if !selected.selection.as_ref()?.matches(&selected.values) {
            return None;
        }
        if selected.values.len() != 1 {
            return None;
        }
        let AbstractValue::Value(Value::Alloc(Kind::Function, key)) = selected.values.first()?
        else {
            return None;
        };
        let key = *key;
        let query = self.retained_query.as_mut()?;
        let Some(mut child) = query.completed.get(&key).cloned() else {
            query.requested = Some(key);
            return None;
        };
        let LocalExecutionScope::Callable(caller) = self.scope else {
            return None;
        };
        let dependencies = self.executor.boxed_settled_prefix_dependencies();
        child.effect_coverage.selected_local_call(LocalCallReceipt {
            caller: LocalBodyIdentity::Callable {
                key: caller,
                owner: self.owner,
            },
            call_span: span,
            target: LocalBodyIdentity::Callable {
                key,
                owner: self.executor.inputs.index.callables[&key].owner,
            },
            dependencies,
        });
        self.outcome
            .absorb_call_effects(&mut child.effects, self.executor.state.assumption.as_ref());
        self.outcome.executed_owners.extend(child.executed_owners);
        Some(LocalValueResult {
            allocation: crate::facts::sync_effects::allocation_transfer::AllocationValue::opaque(
                child.returns,
            ),
            effects: child.effect_coverage,
            selection: None,
            postcondition: None,
        })
    }
}
