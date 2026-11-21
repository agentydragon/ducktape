# Runtime shutdown retry policy probe

[`Runtime.Close`](../env/runtime/internal/coordinator/runtime.go) closes admission, cancels
start work, and runs one shared shutdown. Later callers observe its cached result. Caller
cancellation only ends that caller's wait. A recoverable failure before finalization can
therefore strand the current instance in shutdown. Which boundary can recover safely is still
open.

| Owner                                                                                                                    | Shutdown boundary                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Runtime coordinator](../env/runtime/internal/coordinator/runtime.go)                                                    | Settle the prior transition fence, preserve an existing seal UID, publish one `Closing` generation, and wait for its status. A publication error returns before the in-memory desired state advances.                                                                                                   |
| [Close worker](../env/runtime/internal/coordinator/close.go)                                                             | `closeComponents` waits for that generation's native stop, execution fence, History delivery, Session stop and bootstrap settlement. `Runtime.joinClose` then stops and joins the lifecycle engine, then joins Session and lifecycle workers. Runtime retains ownership if Session cleanup is unproved. |
| [Process supervision](../env/runtime/supervision/host.go)                                                                | Install one stop slot per process entry, cancel and join an in-flight launch, then fence the actual instance. Reissuing the stop request reuses this slot. Process cleanup and ingress acknowledgement have separate owners.                                                                            |
| [Session journal](../env/runtime/journal/journal.go) and [bootstrap event log](../env/runtime/internal/bootstrap/log.go) | Latch uncertain writes. Journal closure marks the handle closed before releasing SQL resources. A later no-op closure cannot prove that the first attempt succeeded.                                                                                                                                    |
| [COSI backing store](../env/runtime/internal/cosistate/state.go)                                                         | Own the lifetime of the exclusive bbolt lock. Runtime closes it after joining work and applying any PID namespace fence.                                                                                                                                                                                |

Start with a fault at the `Closing` publication boundary. Exercise both a rejected publication
and a publication that took effect before reporting failure. Re-read the desired resource before
selecting a retry. Require a live controller, the same closing goal, a preserved seal UID, and
closed admission. Prove that the controller sees one closing generation and that concurrent
waiters share its result.

Keep engine exit, uncertain journal writes, and ambiguous handle release terminal for the
current instance. Consider a native stop retry only once the process owner can prove that a
second fence targets the same instance. Then validate one supported retry against coordinator
and supervision races and native restart recovery.

```bash
bbr test //agentplane/env/runtime/internal/coordinator:coordinator_test //agentplane/env/runtime/supervision:supervision_test
bbr test //agentplane/env/e2e:test_restart
```
