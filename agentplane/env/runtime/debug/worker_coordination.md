# Close and transition worker coordination

The aggregate controller's workers should remain local to the controller. Replacing them with
`env/oneoff.Operation` would add ownership and publication machinery, not remove it.

| Boundary             | Aggregate lifecycle controller                                                                | `oneoff.Operation` hosts                                              |
| -------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Completion wakeup    | `Run` selects worker completion channels directly                                             | Host publishes operation state to wake a COSI controller              |
| Result               | Close error; transition step, typed outcome and error                                         | Error plus operation ID                                               |
| Publication          | Controller publishes each stage before starting its worker and advances only after completion | Start and terminal publication callbacks run around work              |
| Completion ownership | One event loop reads completion and clears its slot                                           | Owner mutex serializes terminal publication and slot consumption      |
| Joining              | `Run` cancels and joins its exact retained workers                                            | Host registers work in a shared `sync.WaitGroup` before launch        |
| Lifetime             | Close belongs to `Run`; transitions belong to the admitted caller plus fence settlement       | Host context, explicit operation cancellation, completion consumption |

`oneoff/oneoff.go` is callback-based; it does not itself create COSI resources. Its callers, such as
`bootstrap/host.go:startOperation`, create operation-state resources keyed by the generated UUID
and delete them during consumption. The aggregate already has generation/sequence-bound statuses
and typed transition results. Reuse here would require either extra resources and another join
barrier or no-op callbacks, while retaining the transition result channel machinery.

## Removed duplication

Close stages run sequentially. Previously each stage allocated a fresh background context and
stored its cancel function in `closeOperation`; only `Run` could cancel a running stage. All stages
now share one background context scoped to that invocation of `Run`. Completion still closes the
worker channel after storing its error. Controller exit cancels the shared context at the former
per-worker cancellation point, then joins both workers.

The context remains independent of both the cancelable Close caller and the engine context. An
engine cancellation is handled by `Run`, which first wakes admitted transition callers, cancels the
independent fence and transition contexts, cancels Close work, then joins. A reconciliation error
uses that same defer even when the engine context remains live. Completed workers are still retained
until reconciliation consumes them, including across controller restart; this change adds no replay.

## Preserved barriers

- `runtime.go:Close` closes admission and cancels the admitted transition. `finishClose` holds
  `transitionMu` and settles the prior fence before publishing a new Closing generation.
- `lifecycle_controller.go:Run` reconciles Close before transitions and runs blocking steps outside
  its event loop. Its exit wakes transition callers before joining a Prepare worker that can wait
  for `transitionMu`.
- `closePlan.fenceCtx` still bounds native cleanup, exact Execution fencing, History flushing and
  Session joining independently. The shared worker context is used by bootstrap settlement; it is
  not a replacement for native fence proof.
- `transition_sequence.go:runStep` still settles an interrupted Execution start with the public stop
  barrier and marks uncertain fences unsafe. Sequence context cancellation and engine-bound fence
  grace remain separate from Close work.
- `runtime.go:joinClose` still joins the engine and component workers, checks cleanup, fences the Pod
  namespace where applicable and only then releases the environment owner. Failed or uncertain
  cleanup retains ownership.

The race-enabled `//agentplane/env/runtime:runtime_test` covers canceled Close waiters, bootstrap
settlement cancellation, Close preempting blocked adoption, engine exit during an accepted Execution
start, exact fence waits and retention of the environment owner after unproved cleanup. Those tests
exercise the existing boundaries; no new worker abstraction is introduced.
