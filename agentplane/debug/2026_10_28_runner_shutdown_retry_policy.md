# Runner shutdown retry policy probe

`Runner.Close` caches its first cleanup result with `sync.Once`. A transient failure before
finalization therefore prevents a later caller from completing cleanup. Repeating the whole
function is unsafe: several resource closers forget their handles before reporting an error.

This source review uses devel `c4d5353a42e0ad6d77422b2b3b6961f34b3f170f` and
[PR #12759](https://github.com/agentydragon/ducktape/pull/12759) at
`57682e2437670dcda5c7c9ed7e962af791e75c42`. That PR separates shared cleanup from caller waits
and observes engine exit; it preserves the first-result cache. No retry behavior is implemented
by this note, and no cleanup failure injection was run for this probe.

## Boundaries and evidence

| Boundary                          | Observed behavior                                                                                                                                                                                                                                                                          | Proposed retry rule                                                                                                                                                                                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Publish `Closing`                 | [`setDesiredState`](../runner/lifecycle_runtime.go) preserves an existing seal UID and can advance the generation after a reported error.                                                                                                                                                  | Re-read desired state after a publication error. Reconcile the same closing goal only while the engine is alive; preserve its seal UID and admission barrier.                                                                                           |
| Halt, stop, inspect, seal         | [`runner_controller.go`](../runner/runner_controller.go) requires current-generation outcomes, completed process cleanup, no finalizers, settled inspections, and required seals before `Closed`. Accepted setup/bootstrap must finish naturally.                                          | Retry only an operation whose owner can establish a safe second attempt. A generation bump alone is insufficient: [`Host.requestStop`](../runner/supervision/host.go) reuses the original stop slot, including its error. Pending work remains pending. |
| Engine exit or journal failure    | Engine completion is terminal in #12759. [`Journal.write`](../runner/journal/journal.go) latches uncertain storage failures until reopen.                                                                                                                                                  | Terminal for this Runner instance. Do not restart the engine, clear a journal failure, or admit new work as a cleanup retry. Replacement still requires native fencing and retained-state recovery.                                                     |
| Stop controllers and join workers | [`Close`](../runner/runner.go) cancels only after `Closed`, waits for engine exit, then joins lifecycle and Session workers.                                                                                                                                                               | Perform once after teardown is proven complete. Keep finalization under one owner; later waiters observe its result.                                                                                                                                    |
| Close journals and bootstrap log  | [`Journal.Close`](../runner/journal/journal.go) sets `closed` before closing SQL handles. [`Host.CloseLog`](../runner/bootstrap/host.go) and [`Log.Close`](../runner/bootstrap/log.go) clear their handles before the close result. A second call can return `nil` after the first failed. | Record the original outcome per resource. Do not interpret an idempotent second call as proof that the original failure recovered. Treat close errors as terminal until the closer exposes a verifiable recovery contract.                              |
| Release state ownership           | [`Owner.Close`](../runner/retained/retained.go) clears its file field, then attempts unlock and close. It runs only after journal/log closure succeeds.                                                                                                                                    | Release last and only once. Preserve any release error; after a release attempt, an error does not prove that the lock is still held.                                                                                                                   |

## Proposed shape

Keep teardown convergence in the existing COSI controllers. Separate its retryable operations
from irreversible finalization, with a single shared cleanup coordinator recording phase and
resource outcomes. Caller cancellation ends only that caller's wait. It neither resets cleanup
nor advances the phase. Preserve terminal errors instead of converting a later no-op to success.

The first implementation should support only an explicitly classified, owner-controlled retry
before finalization. Do not replace `closeOnce` with an unrestricted loop or add a second
imperative teardown path. Process cleanup remains distinct from `Quiesce`: it preserves any
selected seal but does not require ingress acknowledgement.

## Smallest implementation probe

1. Inject a desired-state publication failure, including a write that succeeded before returning
   an error. Prove that another attempt preserves the closing goal, seal UID, and admission fence.
2. Inject a process stop failure. Establish whether the same process instance can safely retry
   its fence before adding a retry mechanism to its owner; retain its finalizer until proven stopped.
3. Inject journal/log close and owner-release errors. Prove that repeated waiters retain the
   original outcome and that a later no-op never reports recovered cleanup.
4. Add those cases to the Runner race and supervision suites, then validate native restart:

   ```bash
   bbr test //agentplane/runner:runner_test //agentplane/runner/supervision:supervision_test
   bbr test //agentplane/runner:test_restart
   ```

Proceed only if the first recoverable case has an explicit owner and a checkable success
condition. Keep engine death, uncertain journal state, and ambiguous handle closure terminal;
they need recovery semantics before a retry policy can be honest.
