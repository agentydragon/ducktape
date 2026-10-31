# Idle Session ownership changes reset a healthy JavaScript session

## Observed failure

Session `99fb87ba-7dfb-408f-97cb-ac32f2aef7f0`, conversation
`47b79ce8-4949-46ad-84a2-802555324e0e`, received two platform notices stating that its JavaScript
host had been lost. Both followed ordinary ownership changes between live sampler replicas.

Inspected deployment: two replicas of
`git.allegedly.works/ducktape-ci/agentplane-central-sampler:devel-20261031120819-c91f790`.
Both Pods were Ready with zero container restarts. They started at `12:10:29Z` and `12:10:43Z`,
before the Session's first lease acquisition at `12:26:53Z`. The Deployment's last completed roll
was at `12:10:53Z`. No rollout overlapped the ownership changes below.

## Evidence

Lease-acquisition logs on 2026-10-31 UTC:

| Time                  | Generation | Sampler Pod                        |
| --------------------- | ---------- | ---------------------------------- |
| `12:26:53.649407265Z` | 1          | `central-sampler-7bdd79cbfd-ffq5d` |
| `12:27:32.381899839Z` | 2          | `central-sampler-7bdd79cbfd-c6whg` |
| `12:28:41.572789403Z` | 3          | `central-sampler-7bdd79cbfd-ffq5d` |
| `12:30:32.601753332Z` | 4          | `central-sampler-7bdd79cbfd-c6whg` |
| `12:31:01.564017752Z` | 5          | `central-sampler-7bdd79cbfd-c6whg` |

The code-mode child started once per Pod: `c6whg` at `12:27:35.993414476Z` and `ffq5d` at
`12:29:26.931406994Z`, both PID `15` in their respective containers. The retained logs contain no
execution failure or lease-renewal failure for this Session.

Two `sampler_event` boundary records have `boundary=code_host_loss`, `direction=committed`:

| Source sequence | Previous host instance                           | Next host instance                               |
| --------------- | ------------------------------------------------ | ------------------------------------------------ |
| 1772            | `d619ee53-f0ac-443d-858e-3d4da32b8b5b` (`c6whg`) | `ff81385e-0ca9-43d2-95eb-d62c0286908b` (`ffq5d`) |
| 4528            | `ff81385e-0ca9-43d2-95eb-d62c0286908b` (`ffq5d`) | `d619ee53-f0ac-443d-858e-3d4da32b8b5b` (`c6whg`) |

Both payloads record `javascript_state_reset=true`, `automatic_replay=false`. The host-instance
identity on return to `c6whg` is unchanged: its original host generation had not been replaced.
The resulting `code_session_epoch` is `2`. At inspection, the Session was idle and unblocked,
with no pending intent; all five cells were completed and all 17 process rows were exited.

The retained model context contains the notices at ordinals `10` and `17`. No earlier cell called
`store()` or `load()`; the only `load()` use was a diagnostic probe after the second notice.
This conversation therefore shows unnecessary resets/notices without demonstrating loss of a
stored application value. A workflow depending on retained JavaScript values would be affected.

## Cause

The relevant paths are unchanged between deployed `c91f790` and the inspected source:

1. [`Runtime.execute`](../runtime.go) returns when no command, live cell or running process remains.
   Its deferred `liveHost.Close` detaches the current binding; `CodeModeHost` retains logical
   sessions and their JavaScript values across idle periods within that host.
2. `Runtime.drive` releases the database lease. [`Store.RecoverableSessions`](../store.go) and
   `Store.Claim` allow either replica to acquire subsequent work; they have no preference or routing
   to the still-live JavaScript host.
3. Before consuming the next command, `Runtime.execute` calls `Store.ReconcileCodeHostLoss` with
   its own local host instance. A different `code_host_instance` is treated as lost without
   checking availability of, or routing to, the original host.
4. Reconciliation appends the notice, increments `code_session_epoch` and clears the host marker.
   [`CodeModeHost.OpenSession`](../codemode_host.go) includes this epoch in its conversation key,
   so even returning to the original Pod opens a fresh logical session instead of reusing its
   earlier values.

The execution lease correctly serializes work, but it does not preserve host placement across
turns. Host mismatch is being used as evidence of host death. Ordinary scheduling between two
healthy replicas therefore invokes the genuine-loss recovery path.

[`Anthropic.Context.AppendNotice`](../anthropic/context.go) encodes platform notices as native
`role=user` messages. The model's statement that it saw text from the user follows this encoding;
the operator did not author these two messages.

## Follow-up

Track the focused correction as
[`CENTRAL_SAMPLER_HOST_AFFINITY`](../../../plans/task_dag.md#central_sampler_host_affinity--preserve-healthy-javascript-state-across-idle-turns).
Keep turn-execution ownership distinct from the identity/liveness of the host retaining JavaScript
state. Reuse or route to the healthy host across idle turns; invoke loss reconciliation only after
the host is actually unavailable. Preserve generation fencing and uncertain-effect handling.
Transparent restoration after actual host death is outside this correction.

The investigation used read-only Kubernetes logs/metadata and read-only PostgreSQL queries. No
Session, command, environment or deployment was changed. Raw captures remain under
`/tmp/agentplane-js-host-loss-99fb87ba/` and are ephemeral; the minimal causal evidence is retained
above. No runtime tests were added or run.
