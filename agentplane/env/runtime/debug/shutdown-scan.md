# Aggregate shutdown scan

The aggregate controller watches the resources consumed by its shutdown predicates.
`ExecutionStatuses` has no reader in `session_lifecycle_controller.go`; its fence
predicate reads `ExecutionLifecycleSignals.Fence` and checks the lifecycle generation.
The asynchronous fence worker does read Execution status: `stopExecution` waits
on Runtime change notifications, handles terminal failures and then publishes the
exact lifecycle generation through `fenceExecution`. That worker does not wait
on aggregate reconciliation. The app-server independently watches `ExecutionStatuses` for its readiness predicate
and publishes `CodexAppServerStatuses`, which the aggregate does consume.

Removing the unused subscription reduces the aggregate input kinds from 11 to 10.
An Execution-status-only update no longer schedules aggregate reconciliation. This
is an event dependency reduction, not measured latency or CPU savings; concurrent
input events can coalesce in COSI. The retained Session inventory, all predicate
reads, generation checks and process finalizer checks remain unchanged.

## Remaining work

After admissions settle, each non-Running reconciliation lists retained Session IDs,
loads missing Sessions and sorts the live inventory. A full successful pass reads
each Session lifecycle status twice, inspection once, and (for a sealed shutdown)
the journal seal twice plus the exact durable acknowledgement. Early blockers stop
these later reads. Each harness process also needs a current desired state, status
and finalizer check.

Before adding cached inventories or generation-scoped scan state, profile a real
shutdown with many retained Sessions and process attempts. Use a Go CPU profile to
separate retained-store listing, inventory sorting and COSI resource reads; record
Session/process counts and blocker transitions alongside it. No such profile was
collected here, so these remaining costs do not justify cached shutdown predicates.
