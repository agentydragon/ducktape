# Harness Host

Harness Host owns one environment's native Sessions, retained Session records, continuation state,
journals and History ingress. Claude uses one native process per Session; Codex uses one shared
app-server with independent native threads. Host never adopts an environment incarnation, writes
its seal marker, runs Sandbox bootstrap or launches the Execution Service.

## Runtime boundary

Environment Runtime runs Host in the same process and provides shared configuration, a direct
COSI lifecycle state and direct typed Session calls. Deployment mounts and filesystem access
remain authoritative.
Each native Session, shared Codex app-server and isolated workspace helper receives its 5-second
stop grace at launch; later `Stop(ctx)` calls use that immutable process policy.

In a Pod, Host uses Runtime's PID1 supervisor so one reaper owns every child wait. In a VM, Host's
native services keep their distinct `harness-host` scope in the workload slice, but the owner unit
and invocation marker are those of Environment Runtime. Runtime joins Host cleanup and fences the
Pod PID namespace before releasing the environment owner.

The Host lifetime begins with its boot ID. Runtime publishes a typed intent bound to Runtime boot ID,
Host boot ID and nonzero generation. The shared COSI engine applies it and publishes readiness,
native cleanup and EnvironmentRuntimeFacts with that exact binding. Host process attempts share
the Runtime's COSI process controller and registry, but retain the Host lifetime and worker barrier.
The Host drain waits for Host-domain process finalizers; Runtime-domain attempts have their own
cleanup barriers. Mode/identity changes require a new generation;
Execution fence acknowledgement may advance monotonically within the same generation.

Runtime passes copied Runtime boot ID, Host boot ID and environment generation bindings with
direct Session calls. Host checks all three at ingress and again under the
native admission lock before durable mutation. A model lookup does not hold that lock. Runtime alone
serves external WatchState, health, epoch ownership and Quiesce.

## Drain and durability

1. Runtime publishes a draining generation. Host closes admission synchronously.
2. Accepted journal admissions and setup settle; Session/native/app-server cleanup and inspection join.
3. Host reports `native_stopped` for the current binding.
4. Runtime proves Execution fenced and sends `execution_fenced` for that binding. Host waits for
   successful application of the exact intent before sealing.
5. A seal-bearing intent seals each journal at its final cursor. Host keeps History ingress alive
   until the exact sealed cursor is acknowledged durably, then reports QUIESCED.
6. Runtime stops and joins Host before releasing the environment owner.

A CLOSING intent may carry no seal (ordinary shutdown), or preserve the current incarnation's seal.
Host lifetime loss closes admission and triggers local cleanup without manufacturing a seal. Failed
cleanup retains Host ownership. A Pod Runtime exit does not by itself fence escaped native descendants;
abnormal replacement requires the outer container fence. VM native services use the Host's distinct
systemd scope with the Runtime service's invocation marker.

Runtime keeps the legacy environment owner inode and `<state_dir>/sessions/sealed`. Host locks
`<state_dir>/harness-host/.agentplane-harness-host-owner`; its records remain in `<state_dir>/sessions`,
journals keep their IDs/cursors, and native homes remain at their existing paths. Bootstrap waiters use
Runtime's generation-bound bootstrap wait; cancellation ends a waiter, never the bootstrap effect.
