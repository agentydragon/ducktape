# Runbook: driving Codex worker agents from a Claude Code orchestrator

Paved 2026-08-28 against `codex-cli 0.150.1`, workers on `chatgpt/oai-responses/gpt-5.6-luna` via
the cluster LiteLLM gateway. Every command and shape here was executed, not read from docs. Helper
scripts live in `scripts/` next to this file.

## The one constraint everything follows from

A Codex worker is a subprocess. **It cannot push a message into the orchestrator's (Claude Code's)
process.** The orchestrator only regains control on one of: a user message, a **background Bash task
exiting**, a **`Monitor` stdout line**, a scheduled trigger, or a PR webhook. So every "hear from the
worker" below is one of those three local mechanisms — there is no fourth. "Send to the worker" is a
launch prompt, a `resume`, or (app-server) a `turn/start` / `turn/steer`.

| Need                                         | Send to worker                                            | Hear back                                          |
| -------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------- |
| One task, wait for the answer                | `codex exec` (blocking Bash)                              | tool returns when it exits                         |
| Multi-turn with memory                       | `codex exec` then `codex exec resume <id>`                | blocking, per turn                                 |
| Long task, don't block                       | `codex exec` under **Bash `run_in_background`**           | one task-notification on exit                      |
| Live progress / mid-run events               | `codex exec --json` piped to a filter under **`Monitor`** | one notification per milestone                     |
| Mid-turn back-and-forth, abort, many threads | **`codex app-server`** JSON-RPC                           | notifications via the driver's stdout (Monitor it) |

## Container liveness — don't lose workers to the idle reaper

The orchestrator here is a Claude Code **web** session, and its VM is reclaimed on inactivity:
per the docs, "Cloud sessions stop after a period of inactivity and the session's VM is reclaimed …
Reopen … to provision a **fresh VM** with your conversation history restored." No exact idle timeout
is published. The consequences are load-bearing for a fleet:

- **Survives for sure:** git-pushed commits and the conversation history (restored on reopen).
- **Dies for sure:** every running process — a long-lived `codex app-server` broker, backgrounded
  `codex exec` workers, and the in-memory thread/turn ids they hold. A fresh VM has no live
  processes, full stop.
- **Observed wiped:** filesystem state (uncommitted files, a scratchpad). Operator-reported
  2026-08-29: another cloud agent's environment was wiped after a container restart — consistent with
  the docs' "fresh VM" (disk re-cloned from git). So treat in-container files as **lost** across a
  restart/reclaim; **push anything that matters to git**. (This was earlier hedged as "maybe some
  paths persist"; the observation says don't count on it.)

Two ways to cope, and a fleet usually needs both:

1. **Stay active so the reaper never fires** (keep-alive within a window):
   - A **foreground** tool call keeps the turn active for its whole duration — inherently reap-proof
     while it runs. Safe for a bounded worker turn.
   - An armed **`Monitor`** (or a `run_in_background` Bash task) is tracked work that re-invokes the
     session on each event, so the session keeps working rather than sitting idle. This is the
     practical keep-alive for a live worker stream — **but** `Monitor` is capped at ≤1 h per arm, and
     "an armed Monitor prevents reclamation" is a reasoned expectation from the tool semantics, **not
     a documented guarantee**. Don't bet unrecoverable state on it; re-arm before the cap.
2. **Make the work survive a reclaim** (durability + recovery), which matters the moment a fleet runs
   longer than one active window:
   - **Checkpoint to durable storage, not container memory** — push worker branches/results to git,
     or keep task state in a store the next VM can re-read. Never hold the only copy of a thread id
     or a result in the orchestrator's RAM.
   - **Re-provisioning wakes** (`send_later` / a routine / a subscribed PR event) bring the session
     back as a _fresh_ VM — they are the recovery path, not a keep-alive. On wake, reconnect from the
     checkpoint.
   - **Run the workers outside the container.** In-container `codex app-server`/`exec` workers are the
     most exposed: a reclaim kills them and every unpersisted thread. The robust substrate is the
     cluster — provision workers as `SandboxClaim` pods (or run `codex app-server` on a pod) so they
     keep running across an orchestrator reclaim, and reconnect to them on the next wake. See
     `README.md` → "Launching workers" and its next-steps.

Liveness profile of each pattern below: foreground `exec` — safe while it runs. Background `exec` /
`Monitor` — safe while armed (bounded), checkpoint + re-arm for longer. In-container `app-server`
broker — **least durable**; keep it inside one active window or move it to a pod.

## Three interfaces, when to reach for each

1. **`codex exec` / `codex exec resume`** — turn-based, synchronous, has session memory. The default.
   Simple and robust; the orchestrator drives every turn. Cannot steer or abort mid-turn.
2. **`codex app-server`** — one long-lived process serving **multiple threads**, with **`turn/steer`**
   (inject mid-turn), **`turn/interrupt`** (abort), and **server→client approval requests**. The only
   interface with true mid-turn back-and-forth; heaviest to drive. Transport is `--listen <url>`:
   `stdio://` (what we drove, and what `haku/runner` uses) **or a network websocket URL** — the latter
   is the one that survives a pod boundary (§ Running the fleet as cluster pods).
3. **`codex mcp-server`** — Codex exposed as MCP tools (`codex`, `codex-reply`). **A dead end for a
   durable fleet**, read from source at `rust-v0.150.1` (not run): it is **stdio-only, deprecated at
   0.150.1**, and its `codex-reply` resolves the thread from an **in-memory map** — a server restart
   loses the session even though the rollout is on disk. Use `app-server` (mid-turn) or `exec`/`resume`
   (durable) instead. Details + tool schemas in § Running the fleet as cluster pods.

## Recipe A — turn-based back-and-forth (`scripts/codex_turn.sh`)

```bash
# turn 1 (new session) — prints "THREAD=<uuid>" then the worker's reply
scripts/codex_turn.sh /path/to/workdir new 'Write calc.py with add(a,b). Print T1_OK.'
# turn 2 (resume) — worker remembers turn 1's files
scripts/codex_turn.sh /path/to/workdir <THREAD-from-turn-1> 'Add multiply() to the SAME calc.py. Print RESULT=<add(2,3)>'
```

Verified: turn 2 appended to the same file and printed `RESULT=5`; independent check passed. The
script hides two gotchas (below). For async, wrap the same call in a background Bash job and act on
the exit notification; for live progress, see Recipe C.

## Recipe B — the app-server bidirectional loop (`scripts/codex_appserver.py`)

`AppServer` (in that file) is a reusable driver: spawns `codex app-server`, does the handshake, and
exposes `request()`, `notify()`, auto-answers approval requests, and streams notifications. Modes:

```bash
python3 scripts/codex_appserver.py basic      # init → thread/start → turn/start → turn/completed
python3 scripts/codex_appserver.py steer      # inject a new requirement mid-turn
python3 scripts/codex_appserver.py interrupt  # abort a running turn
python3 scripts/codex_appserver.py multi      # two independent threads on one connection
python3 scripts/codex_appserver.py effort     # change reasoning effort mid-session (see below)
```

All four verified live:

- **basic**: streamed `thread/started → turn/started → item/completed(userMessage, reasoning,
fileChange, <final text>) → turn/completed`; the file was created.
- **steer**: sent `turn/steer` (with `expectedTurnId` = the active turn) ~1.5 s into a 3-file task;
  the worker's own reasoning mid-turn said "…I'll now create STEERED.txt with the requested word" and
  `STEERED.txt` = `QUACK`. Real mid-turn back-and-forth.
- **interrupt**: `turn/interrupt {threadId,turnId}` → the turn ended `status=interrupted`, the slow
  work never finished.
- **multi**: two `thread/start`s on one connection, each turn writing in its own cwd; both files
  landed. (Run sequentially here; the connection also supports interleaving turns across threads.)

### Protocol facts for 0.150.1 (from `codex app-server generate-json-schema --out DIR`)

- **Framing**: newline-delimited JSON objects, one per line. No `Content-Length` headers, and the
  `jsonrpc` field is omitted.
- **Handshake**: `initialize {clientInfo:{name,version}, capabilities:{}}` → result
  `{userAgent, codexHome, …}`; then the `initialized` notification (no params).
- **Client→server** (the useful ones): `thread/start`, `thread/resume`, `thread/fork`, `thread/list`,
  `turn/start`, `turn/steer`, `turn/interrupt`, `thread/compact/start`, `review/start`.
- **Server→client requests** (must be answered or the turn blocks): `execCommandApproval` (reply
  `{decision:"approved"}`), `item/commandExecution/requestApproval` (`{decision:"accept"}`),
  `applyPatchApproval` / `item/fileChange/requestApproval`. Setting `approvalPolicy:"never"` +
  `sandbox:"danger-full-access"` at `thread/start` means they never fire (unattended workers).
- **Notifications**: `thread/started`, `turn/started`, `turn/completed` (carries
  `status: completed|interrupted`), `turn/failed`, `item/started`, `item/completed`, and delta
  streams (`item/agentMessage/delta`, `item/reasoning/textDelta`, `item/commandExecution/outputDelta`).
- **ID nesting (bit us twice)**: the thread id is `result.thread.id`, the turn id is
  `result.turn.id` — **not** `result.threadId` / `result.turnId`. `turn/steer` and `turn/interrupt`
  take `threadId` + the turn id at top level.
- `input` is an array of `{"type":"text","text":"…"}`.

## Recipe C — live progress via Monitor (`scripts/codex_stream_filter.py`)

Pipe a worker's `--json` through the filter under the orchestrator's `Monitor` tool; each milestone
line becomes a notification. The filter matches success **and** failure signatures (turn.failed,
item error) so a crash can't hide as silence.

```
Monitor(command: 'codex exec --json ... </dev/null 2>/dev/null | python3 scripts/codex_stream_filter.py',
        persistent: false, timeout_ms: 180000)
```

## Reasoning effort (and changing it mid-session)

`reasoning_effort` (`low` | `medium` | `high` | `xhigh`) trades latency/cost for how hard the worker
thinks. Cheap fleet workers default to `low`; raise it for a hard subtask. Three places to set it,
verified against 0.150.1:

- **Per worker (static)** — `model_reasoning_effort = "high"` in `config.toml` (valid under
  `--strict-config`). The scripts read `CODEX_WORKER_EFFORT` (default `low`) into this.
- **Per `exec` invocation** — `codex exec -c model_reasoning_effort=high …` overrides the config for
  that run (and `codex exec -c model_reasoning_effort=high resume <sid> …` for a resumed session).
- **Per turn / mid-session (app-server)** — `turn/start` takes an `effort` field that "overrides the
  reasoning effort for this turn and subsequent turns." So effort is changeable **mid-session**: on
  one thread, turn 1 with `effort:"high"` and turn 2 with `effort:"low"` both complete (see
  `codex_appserver.py effort`). Observed: the per-turn override is accepted and turns complete; a
  visible change in reasoning depth was **not** measured on a trivial task (both emitted one
  reasoning item) — raise-and-measure on a real hard task before assuming a depth change.

## Gotchas (all found by paving, not by reading docs)

- **`codex exec` hangs on stdin** when stdin is not a TTY (it waits to append a `<stdin>` block).
  Always redirect `</dev/null` in a non-interactive driver.
- **`codex exec resume` flag order**: exec options (`--json`, `-C`, `--skip-git-repo-check`) must come
  **before** the `resume` subcommand: `codex exec --json -C DIR resume <SID> 'PROMPT'`. Putting `-C`
  after the session id fails with `error: unexpected argument '-C' found`.
- **`CODEX_HOME` must exist before `codex app-server` starts** — it exits with "CODEX_HOME points to
  …, but that path does not exist" otherwise. The driver `mkdir -p`s it and writes `config.toml`.
- **`CODEX_HOME` under `/tmp`** makes codex refuse to create PATH-alias helper binaries (a warning
  only; `exec`/`app-server` still work). Put it elsewhere for a real deployment.
- **Config keys**: `model_context_window` and `model_reasoning_effort` are **valid** codex config
  keys (pass `--strict-config`); **`model_max_output_tokens` is NOT** — `--strict-config` rejects it
  as an unknown field (non-strict codex silently ignores unknown keys, which is why a worker still
  runs with it set). Also: the `[model_providers.<x>]` block **requires a `name`** or strict-config
  errors "provider name must not be empty".
- **Model metadata warning**: workers on a gateway slug print "Model metadata not found → fallback".
  It **still fires with `model_context_window = 372000` set** (observed), so that key does not
  silence it and whether it changes codex's accounting is unverified. The warning is non-blocking:
  workers completed correctly throughout.

## Coordination patterns (higher level)

- **Fan-out**: N workers = N app-server threads (or N `codex exec` in N git worktrees). Keep one
  worktree per worker so file edits don't collide (~8–10 concurrent is the practical ceiling).
- **Architect/worker**: orchestrator (Fable) writes a spec, worker (Luna) implements, orchestrator
  reviews the diff and either resumes with fixes or accepts. The single-writer rule: workers propose,
  one integrator commits.
- **Async mailbox** (when you want the _worker_ to initiate): worker writes messages to an outbox
  file; the orchestrator `Monitor`s that file (`tail -F outbox`), so each worker message wakes it; it
  replies by `resume`-ing the worker or (app-server) `turn/steer`. The mid-turn version of this is
  just `turn/steer` — already demonstrated.

## Running the fleet as cluster pods (design, dug 2026-08-29)

The container-liveness section's durable answer is "run workers as pods." Two investigations (cluster
wiring; codex source at tag `rust-v0.150.1`) pin down what that takes. **Read from source / manifests,
not run live** — treat as design, not a paved procedure.

### Which codex interface survives a pod boundary

| Interface               | Transport                          | Mid-turn steer                          | Durable across a process restart                                 | Reachable via today's tools                         |
| ----------------------- | ---------------------------------- | --------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------- |
| `codex exec` / `resume` | one-shot                           | no                                      | **yes** — resumes from `CODEX_HOME` on disk (Recipe A, verified) | **yes** — one-shot fits the MCP `exec_sandbox` tool |
| `codex app-server`      | websocket, or `stdio://`           | **yes** (`turn/steer`,`turn/interrupt`) | `thread/resume` exists (disk-resume not re-verified)             | needs a network path in/out of the pod              |
| `codex mcp-server`      | **stdio only, deprecated 0.150.1** | no                                      | **no** — `codex-reply` resolves the thread from an in-memory map | dead end                                            |

codex source (`rust-v0.150.1`): mcp-server is stdio-only and prints a deprecation warning
(`codex-rs/cli/src/main.rs`, `mcp-server/src/lib.rs`); the network `--listen` transport lives in the
separate `app-server` crate; `codex-reply`'s in-memory-only resolve is
`mcp-server/src/message_processor.rs` → `core/src/thread_manager.rs`. Its two tools: **`codex`** (`prompt`
required; optional `cwd`, `model`, `approval-policy` ∈ {`on-request`,`never`}, `sandbox`, and a generic
`config` map — reasoning effort rides there as `config:{model_reasoning_effort}`, no dedicated field) and
**`codex-reply`** (continue by `threadId` + `prompt`). **Net: mcp-server is out; `app-server` is the
live/mid-turn path; `exec`/`resume` is the genuinely-durable turn-based path.**

### The cluster already runs codex app-server in a pod

`haku/runner`'s codex harness (`haku-runtime-sandbox` ns) spawns `codex app-server --listen stdio://`,
holds the NDJSON stdio channel, and bridges each frame outbound over a WebSocket to haku-console with
replay across reconnects (`haku/runner/{harness,transport,backend}.py`). `approvalPolicy:never`,
`danger-full-access`, warm pool `replicas:0` (on-demand). So the durable-stdio-app-server-in-a-pod pattern
is **built** — but it is a Console chat runtime, not exposed to an external agent. Tier-2 below is "expose
the existing runtime," not "build it."

### Three sandbox systems — don't conflate them

- **`sandbox__*` MCP tools** (what a web session reaches via haku-console) → `haku-sandbox` ns, image
  `haku-sandbox-image` (**no codex/node/claude**). `exec_sandbox` is **one-shot `pods/exec` with
  `stdin=False`, buffered output, 5-min / 100 KB cap** (`haku/sandbox/kubernetes_client.py`) — it cannot
  hold a live stdio JSON-RPC channel, and a web session has **no** direct `kubectl exec`/attach RBAC. Fine
  for one-shot `codex exec`; useless for `app-server` stdio.
- **`haku/runner` runtimes** (`codex_app_server`) → the pod pattern above.
- **legacy `agent-workspaces`** → image `agent-workspace` bakes claude+codex+node
  (`cluster/k8s/agents/agent-sandbox/workspace-image/Dockerfile`), region-pinned OVH.

### Egress + placement — already the right posture

Sandbox egress is **namespace-fixed** by a Cilium policy: reaches **in-cluster LiteLLM**
(`litellm.litellm.svc:4000`) and GitHub through a fenced proxy, **blocked from `api.openai.com`** (workers
hit LiteLLM in-cluster; no public OpenAI egress; a worker cannot widen it). Pods schedule on any always-on
node (roaming laptops excluded by taint); only the legacy `agent-workspace` template pins OVH. Claim
lifetime is a **renew-on-exec deadline** (8h initial, +2h per exec) plus a 7-day Kyverno backstop — no
idle-timer field, so a claim with no exec activity is reaped.

### Two-tier plan

1. **Turn-based, durable, ~now:** one-shot `codex exec`/`resume` in a codex-image sandbox via
   `exec_sandbox`. Blockers, both in README § Next steps: point a template the MCP pool serves at a codex
   image, and reflect a worker LiteLLM key. Costs: 5-min/100 KB per exec, no live stream, no mid-turn.
2. **Live / mid-turn:** drive `codex app-server` — a network `--listen` reachable via ingress, or expose
   the existing `haku/runner` runtime to external agents via a haku-console tool. Not mcp-server.

## Not yet paved / limitations

- `codex mcp-server` and the two pod-deployment tiers are characterized from source / manifests
  (§ Running the fleet as cluster pods), not run live.
- Concurrent (interleaved) turns across app-server threads were not stress-tested; threads were
  driven one at a time.
- The file-mailbox worker-initiated loop is sketched, not executed end-to-end (app-server `steer`
  covers the mid-turn case, which is the harder one).
