# Codex readiness deadline failure, 2026-10-30

PR #13033 CI [4a1a5303](https://app.buildbuddy.io/invocation/4a1a5303-e2fe-50a4-bdd7-7b89006abbca)
failed the native Codex scenario before command dispatch. The run tested merge
2429f9b3 (main 57151418 plus executor rejection change 9c10d899). Main included
#13031, which runs six QEMU-emulated guests concurrently. The rejection change
is in the central sampler; this failure was the native Runner's startup path.
The sibling #13034 VM acceptance target passed in invocation 60a849d3.

## Retained guest evidence

The failed Sandbox is systest-vm-2, UID 3c12a44f-2427-4cb5-b498-d6ad4ab9e5bf.
Its console records these UTC times:

| Event                                                          | Time            |
| -------------------------------------------------------------- | --------------- |
| First executor readiness deadline                              | 11:24:06.328614 |
| First generation protocol probe refused connection             | 11:23:50.369037 |
| First generation protocol probe timed out and readiness failed | 11:24:06.424910 |
| Second generation protocol probe refused connection            | 11:24:14.039948 |
| Second generation handshake timed out                          | 11:24:24.326217 |
| Failed command was observed by scenario                        | 11:24:26.054278 |
| Second generation became ready                                 | 11:24:26.747976 |

The executor controller's 20-second budget starts before process launch and
includes the full protocol probe: WebSocket connection, initialize/initialized,
and environment/status. Failure fences the generation before retry. The native
app-server has a separate fixed 30-second wait for executor readiness; it does
not reset for a successor. Its dependency deadline expired before that successor
became ready. Command failure was reported later, about 0.7 seconds before readiness.
The command was settled as undispatched and must not be automatically replayed.

The logs prove eventual readiness and competing startup deadlines. They do not
establish why the first generation remained unready: CPU pressure from concurrent
emulation and executor/probe stalls are not distinguished by these artifacts.

## Configuration change under validation

Expose independent positive millisecond settings in Runner configuration for
executor readiness, probe timeout/interval, dependency wait, and app-server
startup/initialization. Preserve existing defaults and lifecycle/fencing semantics.
The emulated-VM fixture selects 60-second executor readiness, 5-second probes and
120-second dependency wait explicitly. Staging Runner defaults remain unchanged.
This removes hardcoded budgets; acceptance testing is still needed to assess the
fixture profile. One successful retry alone cannot establish the flake rate.

## Separate sampler initialization deadline

The controlled retry of #13033 ([9295b241](https://app.buildbuddy.io/invocation/9295b241-9477-5b19-ae46-0fc154bb7ef2))
instead interrupted central sampler setup. Its retained sampler log records an
Executor initialize receive deadline for Session 0238373b-d87a-415a-b78b-d8cdc8baee00.
The first readiness configuration CI run ([ad81e760](https://app.buildbuddy.io/invocation/ad81e760-3063-53b0-ba01-6ff66c1923fa))
hit the same error for Session 7e9fb7ff-45dd-42fe-9b60-a6564c2a44b1.

The sampler currently shares its ordinary RPC timeout with the initial Executor
OpenSession response wait; the VM fixture sets that timeout to 10 seconds.
Runner readiness configuration cannot extend that independent caller deadline.
Give initialization its own setting and set the VM profile explicitly, preserving
the unknown remote outcome and no-replay behavior when an initialization reply is lost.
