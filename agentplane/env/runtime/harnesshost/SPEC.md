# Harness Host

Harness Host owns one environment's native Sessions, retained Session records, continuation state
and journals. Runtime owns History delivery using Host journal sources and the durable ACK store.
Claude uses one native process per Session; Codex uses one shared
app-server with independent native threads. Host never adopts an environment incarnation, writes
its seal marker, runs Sandbox bootstrap or launches the Execution Service.

## Runtime boundary

Environment Runtime starts Host Session recovery in the same process and provides shared configuration, a direct
COSI lifecycle state and direct typed Session calls. Deployment mounts and filesystem access
remain authoritative.
Each native Session, shared Codex app-server and isolated workspace helper receives its 5-second
stop grace at launch; later `Stop(ctx)` calls use that immutable process policy.

In a Pod, Host uses Runtime's PID1 supervisor so one reaper owns every child wait. Subprocess
fixtures also use Runtime's supervisor and inherit its environment owner descriptor. In a VM, Host's
native services keep their distinct `harness-host` scope in the workload slice, but the owner unit
and invocation marker are those of Environment Runtime. Runtime's single Close worker drives Host
native drain, joins the shared engine and Host workers, closes Session journals, then fences the Pod
PID namespace before releasing the environment owner. A canceled Close waiter does not cancel that
worker. Host has no separate asynchronous Close operation.

Runtime and its Session component share one lifetime identity. Runtime publishes a typed intent
bound to its boot ID and a nonzero generation. The shared COSI engine applies it and publishes readiness,
native cleanup and EnvironmentRuntimeFacts with that exact binding in the report's sampled intent.
The fact payload carries no second binding. Host process attempts share
the Runtime's COSI process controller and registry, but retain their native worker barrier.
The Host drain waits for Host-domain process finalizers; Runtime-domain attempts have their own
cleanup barriers. Mode/identity changes require a new generation;
Execution fence acknowledgement may advance monotonically within the same generation.

Before publishing an Applying marker, the intent controller closes native admission under
the Host admission lock. Session and app-server controllers can still observe the prior
Running desired state, but cannot start a prior-generation native effect. Applying with an
unknown outcome is terminal and never replayed. Once native application publishes the new
Desired and reopens admission, its generation may start effects before the Applied status
is written; Applied is Runtime observation proof, not the native admission commit. If that
status write fails, Host revokes the Runtime binding and fails closed.

Runtime passes copied Runtime boot ID and environment generation bindings with
direct Session calls. Host checks both at ingress and again under the
native admission lock before durable mutation. A model lookup does not hold that lock. Runtime alone
serves external WatchState, health, epoch ownership and Quiesce.

## Drain and durability

1. Runtime publishes a draining generation. Host closes admission synchronously.
2. Accepted journal admissions and setup settle; Session/native/app-server cleanup and inspection join.
3. Host reports `native_stopped` for the current binding.
4. Runtime proves Execution fenced and sends `execution_fenced` for that binding. Host waits for
   successful application of the exact intent before sealing.
5. A seal-bearing intent seals each journal at its final cursor. Runtime keeps History delivery alive
   until the exact sealed cursor is acknowledged durably; Host then reports QUIESCED.
6. Runtime joins History delivery, closes the Session component's native lifetime, joins its controllers
   and workers, then closes Session journals before releasing the environment owner.

A CLOSING intent may carry no seal (ordinary shutdown), or preserve the current incarnation's seal.
Session recovery or History delivery failure closes admission and Runtime triggers local cleanup without manufacturing a seal. Failed
cleanup retains Runtime ownership. A Pod Runtime exit does not by itself fence escaped native descendants;
abnormal replacement requires the outer container fence. VM native services use the Host's distinct
systemd scope with the Runtime service's invocation marker.

Runtime owns the `EnvironmentSeals` COSI resource in `<state_dir>/runtime-resources.db` and holds
the environment owner until Host native cleanup, worker joins and journal close complete. Host
records remain in `<state_dir>/sessions`, journals keep their IDs/cursors, and native homes remain
at their existing paths. Bootstrap waiters use Runtime's generation-bound bootstrap wait;
cancellation ends a waiter, never the bootstrap effect.

## Session configuration

The COSI `SessionRecord` owns durable admission, the initial model and reasoning effort,
setup source and native recovery identity. Confirmed configuration belongs to the Session journal:
`ModelChanged` and `ReasoningEffortChanged`, their terminal command receipts and the effective
configuration checkpoint commit in one SQLite transaction. Native adapters and Session observations
read only the committed configuration; concurrent completions derive previous values in journal order.
Restart recovers that checkpoint before Session publication. A dispatched command without a committed
outcome remains uncertain and is never replayed merely to repair configuration.
