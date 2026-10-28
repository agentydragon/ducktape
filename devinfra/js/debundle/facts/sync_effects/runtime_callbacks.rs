use super::super::value_model::{Context, Values};
use super::interpreter::{Executor, Outcome};
use crate::reports::schema::ExecutionPrecisionLossReason;
use source_identity::SourceUnitOrdinal;

pub(super) enum CallbackOperation {
    PromoteRaw {
        values: Values,
        outcome: Box<Outcome>,
    },
    PromoteDeferred {
        captured: Values,
        outcome: Box<Outcome>,
    },
    PromoteReturned {
        returned: Values,
    },
    InvokePossible {
        arguments: Vec<Values>,
        no_sync_arguments: bool,
        outcome: Box<Outcome>,
    },
    BoundedInvocationFallback {
        owner: SourceUnitOrdinal,
        arguments: Vec<Values>,
        captured: Context,
        reason: ExecutionPrecisionLossReason,
    },
}

#[derive(Clone)]
pub(super) enum CallbackResult {
    Outcome(Box<Outcome>),
    Completed,
}

pub(super) fn dispatch(
    executor: &mut Executor<'_, '_>,
    operation: CallbackOperation,
) -> CallbackResult {
    match operation {
        CallbackOperation::PromoteRaw {
            values,
            mut outcome,
        } => {
            executor.promote_raw_callbacks_transfer(&values, &mut outcome);
            CallbackResult::Outcome(outcome)
        }
        CallbackOperation::PromoteDeferred {
            captured,
            mut outcome,
        } => {
            executor.promote_deferred_callbacks_transfer(&captured, &mut outcome);
            CallbackResult::Outcome(outcome)
        }
        CallbackOperation::PromoteReturned { returned } => {
            executor.promote_returned_callbacks_transfer(&returned);
            CallbackResult::Completed
        }
        CallbackOperation::InvokePossible {
            arguments,
            no_sync_arguments,
            mut outcome,
        } => {
            executor.invoke_possible_callbacks_transfer(
                &arguments,
                no_sync_arguments,
                &mut outcome,
            );
            CallbackResult::Outcome(outcome)
        }
        CallbackOperation::BoundedInvocationFallback {
            owner,
            arguments,
            captured,
            reason,
        } => CallbackResult::Outcome(Box::new(
            executor.bounded_invocation_fallback_transfer(owner, &arguments, &captured, reason),
        )),
    }
}
