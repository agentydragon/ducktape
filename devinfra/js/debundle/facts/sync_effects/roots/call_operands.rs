//! The operands-supplied call transfer: the kernel evaluated the callee and
//! arguments of a direct `Call` in order and hands them over with their
//! selections; the transfer never re-evaluates either.

use super::super::execution_index::{Call, CallKind};
use super::super::interpreter::{EvaluatedCallOperands, Executor, Outcome, SuppliedCallSelections};
use super::super::kernel::frame::EvaluatedCallee;
use super::super::runtime_inference::{CallOperands, CallTransfer};
use super::super::value_model::{AbstractValue, Context, HeapAddress, MemberKey, ValueExpr};
use crate::facts::abstract_domain::value::Kind;

impl Executor<'_, '_> {
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
