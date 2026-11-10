//! The operands-supplied call transfer: the kernel evaluated the callee and
//! arguments of a direct `Call` in order and hands them over with their
//! selections; the transfer never re-evaluates either.

use super::super::execution_index::{Call, CallKind};
use super::super::interpreter::{EvaluatedCallOperands, Executor, Outcome, SuppliedCallSelections};
use super::super::kernel::frame::EvaluatedCallee;
use super::super::runtime_inference::{CallOperands, CallTransfer};
use super::super::value_model::{
    AbstractValue, Context, HeapAddress, MemberKey, RetainedResultKind, ValueExpr, Values,
};
use crate::analysis_inputs::{ForeignCallSummary, ForeignInvocation, ForeignReturn};
use crate::facts::abstract_domain::value::Kind;
use crate::reports::schema::CallbackFactoryUseKind;
use std::collections::BTreeSet;

impl Executor<'_, '_> {
    /// The derived summary of a callback-factory import called with one
    /// inline function: the provider body runs no code but its own, so it
    /// does not invoke the callback, and its fresh result is the only thing
    /// that retains it. The callback is a deferred effect of whoever later
    /// holds the result, never of this call.
    pub(in crate::facts::sync_effects) fn retaining_factory_summary(
        &self,
        call: &Call,
    ) -> Option<&ForeignCallSummary> {
        let ValueExpr::Binding(binding) = &call.callee else {
            return None;
        };
        let summary = self
            .inputs
            .index
            .callback_factory_call_summaries
            .get(binding)?;
        (call.kind == CallKind::Call
            && !call.has_spread
            && matches!(call.args.as_slice(), [ValueExpr::Alloc(Kind::Function, _)])
            && summary.invocation == ForeignInvocation::Call
            && summary.returns == BTreeSet::from([ForeignReturn::FreshAllocation])
            // The summary proves nothing about the writers of a provider
            // cell its body reads, so such a call keeps the flattened lane.
            && summary.provider_cell_reads.is_empty())
        .then_some(summary)
    }

    /// Substitutes a retaining factory call: the result is the audited
    /// carrier of the callback the kernel just created, so every later use of
    /// the result still reaches the callback.
    fn retaining_factory_result(&mut self, call: &Call, operands: &CallOperands) -> Option<Values> {
        let summary = self.retaining_factory_summary(call)?.clone();
        let EvaluatedCallee::Direct(selected) = &operands.callee else {
            return None;
        };
        let ([ValueExpr::Alloc(Kind::Function, source)], [callback]) =
            (call.args.as_slice(), operands.arguments.as_slice())
        else {
            return None;
        };
        if selected.values != Values::from([AbstractValue::CallbackFactory])
            || callback.len() != 1
            || !callback.iter().all(|value| {
                matches!(
                    value,
                    AbstractValue::Value(crate::facts::abstract_domain::value::Value::Alloc(
                        Kind::Function,
                        actual,
                    )) | AbstractValue::Closure(HeapAddress { callable: actual, .. }, _)
                        if actual == source
                )
            })
        {
            return None;
        }
        self.record_foreign_call_use(&summary);
        Some(self.retained_factory_carrier(call, true, callback))
    }

    /// The audited carrier of the one callback a factory call retains. With
    /// `records_evidence` the call's callback-factory evidence is reported as
    /// used for its timing and its return shape.
    pub(in crate::facts::sync_effects) fn retained_factory_carrier(
        &mut self,
        call: &Call,
        records_evidence: bool,
        callback: &Values,
    ) -> Values {
        if records_evidence && let ValueExpr::Binding(binding) = &call.callee {
            for kind in [
                CallbackFactoryUseKind::CallTiming,
                CallbackFactoryUseKind::ReturnShape,
            ] {
                self.record_callback_factory_use(binding, kind, call.owner, call.span);
            }
        }
        let callbacks = self.callback_values(callback);
        Values::from([self.audited_retained_result(RetainedResultKind::Factory, callbacks)])
    }

    /// Every call of `binding` among `calls` is one the ordered lane
    /// substitutes, so a body that reads the factory import only to call it
    /// gets no worse an outcome on the kernel than on the flattened lane.
    pub(in crate::facts::sync_effects) fn substitutes_factory_calls(
        &self,
        calls: &[Call],
        binding: &swc_ecma_ast::Id,
    ) -> bool {
        let mut calls = calls
            .iter()
            .filter(|call| matches!(&call.callee, ValueExpr::Binding(callee) if callee == binding))
            .peekable();
        calls.peek().is_some() && calls.all(|call| self.retaining_factory_summary(call).is_some())
    }

    pub(in crate::facts::sync_effects) fn execute_supplied_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
        operands: CallOperands,
    ) -> CallTransfer {
        assert!(call.kind == CallKind::Call && !call.has_spread);
        assert!(match (&call.callee, &operands.callee) {
            (ValueExpr::Binding(_), EvaluatedCallee::Direct(_)) => true,
            (ValueExpr::Alloc(Kind::Function, source), EvaluatedCallee::Direct(selected)) => {
                selected.values.len() == 1
                    && matches!(selected.values.first(),
                        Some(AbstractValue::Closure(HeapAddress { callable: actual, .. }, _)) if actual == source)
            }
            (
                ValueExpr::Member(receiver, Some(_), MemberKey::Static, _),
                EvaluatedCallee::Member { .. },
            ) => matches!(receiver.as_ref(), ValueExpr::Binding(_)),
            _ => false,
        });
        if let Some(values) = self.retaining_factory_result(call, &operands) {
            return CallTransfer::Values(values);
        }
        let selected_member_dependencies = match &operands.callee {
            EvaluatedCallee::Member {
                selection_dependencies,
                ..
            } => selection_dependencies.clone(),
            EvaluatedCallee::Direct(_) => None,
        };
        let selected_value = match &operands.callee {
            EvaluatedCallee::Direct(value) => value
                .selection
                .as_ref()
                .filter(|selection| selection.matches(&value.values))
                .cloned(),
            EvaluatedCallee::Member { .. } => None,
        };
        let supplied = SuppliedCallSelections {
            selected_pure_initializer_call: operands.selected_pure_initializer_call,
            selected_member_dependencies,
            selected_value,
            argument_selections: operands.argument_selections,
        };
        assert_eq!(operands.arguments.len(), call.args.len());
        let (targets, args, prepared_receiver) = match operands.callee {
            EvaluatedCallee::Direct(targets) => (targets.values, operands.arguments, None),
            EvaluatedCallee::Member {
                receiver, targets, ..
            } => (targets, operands.arguments, Some(receiver)),
        };
        self.transfer_evaluated_call(
            call,
            env,
            captures,
            outcome,
            EvaluatedCallOperands {
                targets,
                args,
                prepared_receiver,
            },
            Some(supplied),
        )
    }
}
