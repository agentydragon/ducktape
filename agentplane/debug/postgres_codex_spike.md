# Postgres Codex sampler spike

Baseline merged in [PR #12207](https://github.com/agentydragon/ducktape/pull/12207), 2026-10-27. The
operator-dispatched code-mode-only experiment was implemented in `/root/postgres_harness_spike` under
`agentplane/x/postgres_codex`, branch `x/postgres-codex-20261027`. The test-only source and Bazel targets
have since been removed; this report preserves the measured behavior and limitations. The experiment was
a custom outer loop reusing upstream components, not a persistence fork or production replacement. No
staging changes or live inference were requested or performed.

## What the slice establishes

The executable checkpoints a complete provider response, runs a real upstream JavaScript cell and
remote shell/patch, commits its output, and lets a fresh sampler process issue the next model request.
The integration fixture uses PostgreSQL and the pinned npm Codex 0.162.1 binaries, rather than copies
of their runtime or patch implementation. The same SSE fixture also drives the full native app-server.
Tests cover encrypted reasoning continuation, stale-owner rejection, lost replies after a real append,
a truncated provider stream, sampler SIGKILL while sampling, and executor SIGKILL mid-tool.

All seven integration cases passed in **51.9 seconds** on RBE, invocation
[`8a1a2810-b81f-45ea-b971-ad6cec41e15a`](https://app.buildbuddy.io/invocation/8a1a2810-b81f-45ea-b971-ad6cec41e15a).
The invocation also named all five runtime libraries so their mypy and Ruff aspects ran. The command below
is the historical validation command; its Bazel targets have since been removed:

```sh
bbr test //agentplane/x/postgres_codex:test_sampler \
  //agentplane/x/postgres_codex:store //agentplane/x/postgres_codex:executor \
  //agentplane/x/postgres_codex:code_mode //agentplane/x/postgres_codex:sampler \
  //agentplane/x/postgres_codex:main --test_output=errors
```

Normal pre-commit hooks passed on the implementation commit. The adapter consumes upstream's
`process/output`, `process/exited` and `process/closed` push hints and collects retained output
through `process/read`, whose `nextSeq` is an exclusive cursor. The real binary tests exposed these
interleaved events; a request/response-only fake would have missed them.

## Cost accounting

At the passing implementation, physical lines including comments and blanks were: `store.py` 143,
`executor.py` 81, `code_mode.py` 153, `sampler.py` 103 and `main.py` 83: **563 new runtime lines**.
Test/support code is 511 lines. These are source counts, not a claim that a complete harness fits
in that size. BUILD and documentation lines are excluded.

The direct Python runtime dependencies are existing `aiohttp`, `SQLAlchemy` and `asyncpg`; the
test-only executable also uses the existing managed-engine helper. Tests add existing pytest,
pytest-bazel, testcontainers and native harness fixtures. No lockfile or production dependency changes
are needed. Runtime also requires PostgreSQL, a sampler-side `codex-code-mode-host`, and executor-side
`codex` plus `/bin/sh`. These compiled upstream components dominate the reused implementation.

Source at tag `rust-v0.162.1` (`092d3acd`), counting all `.rs` files including tests:

| Upstream crate       | Rust files | Physical lines | Direct Cargo dependencies |
| -------------------- | ---------: | -------------: | ------------------------: |
| code-mode-host       |         31 |          9,412 |                        19 |
| code-mode-runtime    |         26 |          8,393 |                        11 |
| code-mode-protocol   |         20 |          5,023 |                         8 |
| exec-server          |        181 |         64,975 |                        43 |
| exec-server-protocol |         10 |          2,891 |                         9 |
| apply-patch          |         16 |          5,638 |                         9 |
| core, for comparison |        846 |        448,069 |                       105 |

Dependency counts are manifest entries, not unique transitive packages. These crate totals are not
executed-line counts, a minimal link closure, runtime memory measurements, or quantities that can be
summed as the exact binary footprint. The experiment reuses release binaries; it does not trim them.
No fork patch or full persistence-fork implementation was built, so a measured fork LOC comparison
is unavailable. Removing upstream rollouts/state-db alone would retain core's substantial orchestration.

## Provider and runtime surface we would own

The new provider code owns Responses Lite's header, `additional_tools`, a freeform `exec` declaration,
SSE terminal detection, encrypted-reasoning echo, custom-tool output shape, model/cache metadata and
full-history requests. It persists only complete provider responses, without partial streaming deltas.
It does not test OpenAI authentication, provider cache behavior, live model acceptance, compaction,
multimodal output, retries or reconnect protocols. The native baseline establishes agreement with a
pinned independent consumer of the scripted protocol; it does not establish live API compatibility.

The upstream host owns JavaScript parsing, V8 isolation and nested-tool delegation. Our adapter owns
host protocol V1 and the enabled tool definitions. The executor adapter owns initialization,
`process/start`, `process/read`, output-cursor draining and a deliberately restricted tool result.
It executes actual upstream patch code by argv. It does not replicate upstream shell policy, approval,
PTY/stdin, image handling or formatted tool metadata. Neither remote process effects nor live V8
isolate state become transactional because their intent was committed in Postgres.

The implementation-time experimental contract listed the status of every omitted headless capability;
this report preserves its key boundaries. At the time, the slice showed custom conversation storage was
feasible with a small adapter, while the acceptability of omitted control features remained a product
decision. The reported recommendation was to prefer upstream central Codex for broad feature compatibility;
a custom loop was a candidate only if its code-mode-only scope and narrower tool contract were accepted,
with explicit owners for provider tracking and uncertainty reconciliation. Frontend tracing is separate work;
the operator explicitly removed it as an acceptance gate because Langfuse provides partial links.

## Process-control extension

The operator required `wait`, stdin and interrupts as minimum custom-sampler capabilities after the
baseline merged. [PR #12213](https://github.com/agentydragon/ducktape/pull/12213) retains the actual pinned code-mode host across model sampling and
`session/wait` observations in `main_bin run`. A yielded JS cell has a host-UUID-prefixed handle;
raw native IDs restart at `1`, so they cannot be global database keys or model-facing identities.
A separate durable process registry tracks running/terminal shell handles and retained-output cursors.
The model receives current execution state regenerated from these tables alongside conversation input.

`exec_command` can yield, and `write_stdin` sends real bytes or polls the same process. A sole Ctrl-C
uses `process/signal`; EOF uses PTY Ctrl-D. The pinned protocol has no pipe-close operation, and that
unsupported EOF request is rejected. Initialization must resume the database's executor `sessionId`:
process IDs are session-scoped, and initializing a different session makes them unknown even on the
same server. The pinned server keeps a detached session for **30 seconds**. Expiry/server loss with
live handles blocks recovery without a replacement session or replay. Terminal handles permit a new
session. V8 state remains live-only: a fresh sampler owner cannot reconstruct a yielded cell from
nested results and must block, rather than replay JavaScript.

An external CLI `interrupt` targets the active owner generation, persists a request and is observed
by `advance`/`run`; `status` exposes its outcome. A committed request prevents the next outer or
nested dispatch. Interrupt closes the sampling response, tries native cell termination, observes
host shutdown, gates further executor mutations, terminates known processes and observes their exits.
Stopping the whole host closes the window where native execution started before its cell registry
write. Known fixture shell/child PIDs are checked for no remaining live process after successful
cleanup; this is not a cgroup fence for escaped descendants.

Successful cleanup appends interrupted/not-started results for every outstanding committed model
call, stops sampling and allows only explicit new input to reopen the turn. Provider cancellation is
local HTTP cancellation; provider completion and partial remote effects are not rolled back. Lost
cleanup outcomes remain `effects_unknown` with a blocking intent, including an interrupt that occurs
between transitions. Stdin mutations and shell commands are never automatically retried. The
implementation-time SPEC described deadlines and remaining omissions, summarized above.

The extension has **1,277 physical runtime Python lines**, including comments/blanks, across
`store.py` 435, `executor.py` 193, `code_mode.py` 331, `sampler.py` 127, `main.py` 116 and `control.py` 75. The baseline was 563; the
additional lifecycle and durable control code is 714 lines. New control test/support code is 739
lines. Counts exclude BUILD, SQL and docs, and do not include the reused upstream Rust inventory
above. No new third-party runtime dependency or production caller was added.

All **20 integration cases across four targets passed** with named runtime libraries' Ruff/mypy
aspects in [invocation 5dd30dcf-e2c5-4aa2-b4a3-12c86b470a80](https://app.buildbuddy.io/invocation/5dd30dcf-e2c5-4aa2-b4a3-12c86b470a80):
seven control cases, five interrupt cases, one additive-schema upgrade case and the original seven.
The final boundary case interrupts after native host execution starts but before its live-cell
registry write; observed host exit prevents hidden V8 work from continuing. The migration starts
with the original conversation/intent tables and verifies preserved prompt and result rows.
Normal commit hooks passed.

The final-code rerun [565aa96e](https://app.buildbuddy.io/invocation/565aa96e-ae4f-490a-aad7-04a220edd5a3)
passed interrupt/schema checks and runtime lint, but the controls and original sampler targets hit
their unchanged 60-second deadlines. A streamed diagnostic rerun of those two targets
[e1991210](https://app.buildbuddy.io/invocation/e1991210-f0dc-4cb1-a1d2-8b324e18f7e3) passed all 14 cases
in 33.2 and 42.5 seconds. Its stack dumps observed PostgreSQL image loading in
`util.oci.push_to_daemon`; fixture setup took 19.14 and 22.50 seconds. This establishes final-code
passes for all 20 cases across the two invocations, with a target-timeout flake still observed in
the ordinary parallel run. No deadlines were increased.

The command below is the historical validation command; its Bazel targets have since been removed:

```sh
bbr test //agentplane/x/postgres_codex:test_controls \
  //agentplane/x/postgres_codex:test_interrupts //agentplane/x/postgres_codex:test_schema \
  //agentplane/x/postgres_codex:test_sampler //agentplane/x/postgres_codex:store \
  //agentplane/x/postgres_codex:executor //agentplane/x/postgres_codex:code_mode \
  //agentplane/x/postgres_codex:control //agentplane/x/postgres_codex:sampler \
  //agentplane/x/postgres_codex:main --test_output=errors
```

These checks use real binaries and Postgres. No live inference, production integration, merge
or deployment is established by this extension.

## Combined compaction and controls

The integrated experiment supports all four requested controls: compaction, model-facing `wait`,
interactive stdin and external interruption. `responses.py` owns the shared wire adapter and tool
schema. `run --auto-compact-tokens` defers compaction while a JS cell is live, allowing the model to
wait it to completion. Once compacted, the model still receives durable process handles and can
send stdin to the existing executor process. Interrupted and unstarted sibling outputs append to
both audit history and the active compacted context; explicit new user input continues from that
context. External interruption during automatic compaction preserves the prior context and records
provider-completion uncertainty.

A native executor session permits only one attached connection. The fresh-sampler fixture detaches
its original connection, compacts in one CLI process, samples in another, then resumes the recorded
executor session in a third to send real input and observe process exit. Keeping the initial socket
attached was rejected by upstream with error `-32010`; session resumption never substitutes a fresh
executor session for live handles.

All **38 cases across five targets passed**: the 20 sampler/control/interrupt/schema cases and 18
compaction/integration cases. [105393d7](https://app.buildbuddy.io/invocation/105393d7-2865-45b6-a2e5-458631dd4b1a)
passed all four sampler/control/interrupt/schema targets and named runtime library Ruff/mypy; its
compaction target exposed the attached-session fixture error above.
[8e92aa72](https://app.buildbuddy.io/invocation/8e92aa72-d60a-439a-9f54-3d7412881fd2)
passed all 18 compaction cases after that fixture correction, in 31.8 seconds. The upgrade case applies
both additive migrations to original experiment tables and verifies preserved prompt/result rows,
active context and process-control state. Normal commit hooks passed; no timeout was increased.

The combined runtime is **1,501 physical Python lines**, including comments/blanks, across `main.py`,
`sampler.py`, `code_mode.py`, `executor.py`, `store.py`, `responses.py`, `compaction.py` and `control.py`.
This still reuses the substantial pinned upstream host, execution and patch implementations counted
above. Live provider acceptance, production integration and the remaining harness capabilities are
not established by the scripted tests.
