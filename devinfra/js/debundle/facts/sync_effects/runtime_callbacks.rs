use super::interpreter::{Executor, Outcome};
use super::value_model::{Context, Values};
use crate::execution_evidence::{ExecutionPrecisionLossReason, HavocReach};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

/// The invocation's diagnostic origin survives a switch into its callee's
/// source. It carries no effect or invocation authority.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct InvocationSite {
    pub(super) call: super::value_model::RuntimeCallSite,
    pub(super) owner: Option<crate::program_execution::RuntimeSourceUnitId>,
}

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
        outcome: Box<Outcome>,
    },
    InvokePossible {
        arguments: Vec<Values>,
        no_sync_arguments: bool,
        publication_span: Option<Span>,
        outcome: Box<Outcome>,
    },
    BoundedInvocationFallback {
        request: Box<BoundedInvocationFallbackRequest>,
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
    pub(super) callsite: Option<crate::reports::schema::BoundedInvocationFallbackCallSite>,
    /// Abstract input values used only to root diagnostics. At a callback-pool
    /// boundary these are the original opaque-operation arguments, not the
    /// synthetic flattened callback-pool argument. An empty vector means the
    /// pool admitted no supplied arguments. This never changes semantic args.
    pub(super) arguments: Option<Vec<Values>>,
}

#[derive(Clone)]
pub(super) struct CallbackResult(pub(super) Box<Outcome>);

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
            CallbackResult(outcome)
        }
        CallbackOperation::PromoteDeferred {
            captured,
            mut outcome,
        } => {
            executor.promote_deferred_callbacks_transfer(&captured, &mut outcome);
            CallbackResult(outcome)
        }
        CallbackOperation::PromoteReturned {
            returned,
            mut outcome,
        } => {
            executor.promote_returned_callbacks_transfer(&returned, &mut outcome);
            CallbackResult(outcome)
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
            CallbackResult(outcome)
        }
        CallbackOperation::BoundedInvocationFallback { request } => CallbackResult(Box::new(
            executor.bounded_invocation_fallback_transfer(*request),
        )),
    }
}
