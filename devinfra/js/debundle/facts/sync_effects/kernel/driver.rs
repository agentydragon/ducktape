//! The one loop that runs a continuation to completion.

use super::super::alternatives::{self, Family, Leaf};
use super::super::local_operations::LocalBody;
use super::context::{ForkPolicy, KernelContext};
use super::continuation::{LocalContinuation, LocalSuspension};

/// Run `continuation` over `body`, performing each suspended call through the
/// adapter. Syntax coverage comes from `LocalBody`'s canonical lowering; the
/// ledger composes every reached operation, including values a sequence
/// discards.
///
/// An undecided test forks the body. Under `ForkPolicy::Refuse` the body is
/// refused there. Under `ForkPolicy::Explore` every feasible path runs to the
/// end of the body in its own path state, and the family is the caller's to
/// join; whether a refused leaf refuses the family is the caller's
/// `Family::all_completed` check.
pub(in crate::facts::sync_effects) fn drive<C: KernelContext>(
    context: &mut C,
    body: &LocalBody,
    mut continuation: LocalContinuation,
) -> Family<C::Branch> {
    loop {
        match continuation.poll(context, body) {
            LocalSuspension::Call(request) => {
                let result = context.call(&mut continuation.frame, &request);
                continuation.resume_call(result);
            }
            LocalSuspension::Complete(attempt) => {
                return Family::One(Box::new(Leaf {
                    frame: continuation.frame,
                    attempt,
                }));
            }
            LocalSuspension::Fork(children) => match context.fork_policy() {
                ForkPolicy::Refuse => continuation.refuse_branching(),
                ForkPolicy::Explore => {
                    return alternatives::explore(context, children, |context, child| {
                        drive(context, body, child)
                    });
                }
            },
        }
    }
}
