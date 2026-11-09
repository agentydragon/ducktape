use super::interpreter::{Executor, Outcome};
use super::value_model::{Context, Values};
use crate::execution_evidence::{ExecutionPrecisionLossReason, HavocReach};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

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
        publication_span: Option<Span>,
        outcome: Box<Outcome>,
    },
    BoundedInvocationFallback {
        request: BoundedInvocationFallbackRequest,
    },
}

pub(super) struct BoundedInvocationFallbackRequest {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) arguments: Vec<Values>,
    pub(super) captured: Context,
    pub(super) reason: ExecutionPrecisionLossReason,
    pub(super) reach: HavocReach,
    pub(super) span: Span,
    pub(super) diagnostics: BoundedInvocationFallbackDiagnostics,
}

#[derive(Default)]
pub(super) struct BoundedInvocationFallbackDiagnostics {
    /// Span of the opaque operation whose callback pool entered this fallback.
    pub(super) callsite_span: Option<Span>,
    /// Abstract input values used only to root diagnostics. At a callback-pool
    /// boundary these are the original opaque-operation arguments, not the
    /// synthetic flattened callback-pool argument. An empty vector means the
    /// pool admitted no supplied arguments. This never changes semantic args.
    pub(super) arguments: Option<Vec<Values>>,
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
            publication_span,
            mut outcome,
        } => {
            executor.invoke_possible_callbacks_transfer(
                &arguments,
                no_sync_arguments,
                publication_span,
                &mut outcome,
            );
            CallbackResult::Outcome(outcome)
        }
        CallbackOperation::BoundedInvocationFallback { request } => CallbackResult::Outcome(
            Box::new(executor.bounded_invocation_fallback_transfer(request)),
        ),
    }
}
