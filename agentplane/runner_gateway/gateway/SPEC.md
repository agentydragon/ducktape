# Environment Runtime proxy authorization

The public Gateway authenticates Gateway-audience Kubernetes tokens with singular Pod-name and
Pod-UID proof. All calls retain Sandbox UID/owner and current Runtime incarnation fencing.

- Only Pod-bound tokens of the combined Gateway/Dispatcher ServiceAccount may call Runtime `CreateSession`, `SubmitInput`,
  `ChangeModel`, `ChangeReasoningEffort` and `InterruptTurn`. Startup derives its account from the
  existing mounted delivery token, requires it in `gateway_callers`, and verifies the configured
  current Pod namespace/name, Kubernetes Pod UID and Pod ServiceAccount. An unreadable token, unavailable
  TokenReview or Pod read, missing Pod proof or identity mismatch prevents startup.
- Dispatcher workers use that workload's projected token across replicas. They replace outgoing
  metadata; caller credentials and caller-supplied identity headers cannot become this proof.
  Every other caller uses SessionDispatcher for Session creation and commands, with its existing
  direct or delegated authorization. The workload credential trusts the combined process, since
  Gateway and Dispatcher share one Pod and executable; it is not a separate in-process identity. Any Pod able to obtain that ServiceAccount
  is trusted equally, including other Gateway replicas. Existing platform principals allowed to
  create arbitrary Pods remain trusted Kubernetes administrators; this policy does not constrain
  that capability.
- `gateway_callers` grants `ListSessions` and `GetStatus` diagnostics. Diagnostics grant neither
  command nor wake authority. `WatchState` remains private to Gateway-to-Runtime observation.
- Only `sandbox_controller_account` may `Quiesce`; omitting it admits nobody.
- The separate Executor service retains its own `executor_callers` grant, including its streaming
  `CreateSession`. It does not create a Runtime Session or enter Dispatcher admission.
- Gateway proxy and Executor methods reject delegated envelopes. Delegation is configured only
  on Dispatcher and EnvironmentService methods. Explicit resume and automatic wake retain their
  own lifecycle authorization.

Runtime Session creation and commands, including queued interrupts, are writes: the Gateway
refuses forwarding once its synchronized inventory observes `Quiescing` or deletion. Diagnostics
remain available while quiescing, and the controller's authorized `Quiesce` can still complete the
seal. Cache observation is asynchronous; this routing check does not make Kubernetes
`deletionTimestamp` atomic with Runtime admission.

For ordinary managed deletion, the receiver barrier is the exact incarnation's completed Runtime
quiescence: every Session has closed final admission, sealed its journal and received History's seal
acknowledgement before the controller removes the workload. A request already past Gateway routing
must still pass Runtime's admission gate. Runtime's initial lameduck permits exact-turn interrupts;
its final Session cutoff refuses new command IDs, including interrupts. Existing command IDs retain
journal reconciliation. Removing finalizers or using foreground deletion bypasses this managed
protocol and supplies no such proof.
