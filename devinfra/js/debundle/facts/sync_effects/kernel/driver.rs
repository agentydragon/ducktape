//! The one loop that runs a continuation to completion.

use super::super::local_operations::LocalBody;
use super::context::KernelContext;
use super::continuation::{LocalContinuation, LocalSuspension};
use super::frame::{LocalExecutionAttempt, LocalFrame};

/// Run `continuation` over `body`, performing each suspended call through the
/// adapter. Syntax coverage comes from `LocalBody`'s canonical lowering; the
/// ledger composes every reached operation, including values a sequence
/// discards.
pub(in crate::facts::sync_effects) fn drive(
    context: &mut impl KernelContext,
    body: &LocalBody,
    mut continuation: LocalContinuation,
) -> (LocalFrame, LocalExecutionAttempt) {
    loop {
        match continuation.poll(context, body) {
            LocalSuspension::Call(request) => {
                let result = context.call(&mut continuation.frame, &request);
                continuation.resume_call(result);
            }
            LocalSuspension::Complete(attempt) => return (continuation.frame, attempt),
        }
    }
}
