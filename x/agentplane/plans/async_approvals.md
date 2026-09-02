# Asynchronous approvals

Status: **design notes for machinery layered on the Agent thread API.** Builds on the shared
protocol and service seam (`B`, `C` in [`task_dag.md`](task_dag.md)) and is the concrete case
behind delivering external events as thread inputs (`Y`). Nothing here is built yet; the harness
behaviors it relies on are pinned by [`../harness_tests/`](../harness_tests/).

## Decisions

- **Native harness approvals stay off.** Both harnesses run allow-everything inside the sandbox
  (Claude `can_use_tool` auto-allowed, Codex `approval_policy = "never"`). The sandbox is the
  blast radius; a sandbox-local action needs no human. Two facts rule native prompts out as the
  approval channel: they block the turn with no "come back later" answer, and a harness can only
  execute an approved action if it holds the credential, which the model's own shell can then read.
  Codex's decline additionally carries no text the model sees.
- **Haku console stays the credential holder and approval broker** for operations that need the
  operator's credential; where the target's own RBAC can scope the agent, it gets a delegated
  identity instead ([`external_access.md`](external_access.md)). Anything that leaves the
  sandbox is a Haku tool call. Credentials are injected server-side at execution; the tool result
  is the message channel, which both harnesses pass to the model verbatim.
- **Submission never blocks.** A call that is not auto-approved returns a stub immediately. The
  agent keeps working, withdraws if the ask becomes moot, and learns the decision as a later thread
  input rather than by polling.
- **No expiry.** A pending call waits as long as the agent wants it to; `withdraw_tool_call` is the
  agent's lever. Withdrawing pending calls of a thread that is archived for good is a follow-up.

## Tool contract

One schema shape for every Haku tool, auto-approved or not:

- **Input:** the upstream tool's own argument schema plus one optional `rationale` string, the
  agent's own "why I am doing this" for the approval view and the audit row. No `title`, no
  `wait_for_result_ms`, no separate envelope for gated tools.
- **Result:** one envelope for every tool.

```json
{
  "tool_call_id": "tc_01J...",
  "status": "pending",
  "submitted_at": "2026-09-02T08:14:03Z",
  "now": "2026-09-02T08:14:03Z",
  "message": "Not auto-approved; the operator has been notified. Pending means unanswered, not refused: answers usually take hours. Keep working; the decision and any result arrive as a later input. Use get_tool_call to check early or withdraw_tool_call if this is no longer needed."
}
```

| `status`    | Meaning                                              | Carries                 |
| ----------- | ---------------------------------------------------- | ----------------------- |
| `completed` | Auto-approved or approved, executed, result attached | `result`                |
| `pending`   | Waiting for the operator                             | timestamps, message     |
| `running`   | Approved, execution in progress                      | `decided_at`            |
| `denied`    | Operator or policy refused                           | `decided_at`, `message` |
| `withdrawn` | Agent retracted it                                   | `withdrawal_reason`     |
| `failed`    | Approved but execution failed                        | `error`                 |

Timestamps are data the model reads instead of inferring: `submitted_at` and `now` on every
response, so "pending for two minutes" is read as the operator not looking yet, not as a refusal.
An operator-presence field is undecided: "queue last viewed" has no clear meaning when approvals
come from notification buttons rather than the queue view. If one is added, the honest candidate is
the time of the operator's last decision on any call, which every approval channel updates.

Lifecycle tools:

- `get_tool_call(tool_call_id, wait_for = "approval" | "completion", wait_ms)`: the long poll,
  returning as soon as the named state is reached or `wait_ms` elapses. `completion` implies
  `approval`. This is the exception path, for checking early or after a resume; the normal path is
  the decision arriving as input.
- `withdraw_tool_call(tool_call_id, reason)`: the agent's only lifecycle action.

Auto-approved calls return `completed` with the upstream result inside `result`. They lose the exact
upstream response shape; the one contract for every tool is worth that.

## Decision delivery

A Haku decision (approved, denied with reason, completed with result, failed) becomes a thread
input delivered by Agentplane. The delivery path follows thread state, and each path is pinned by a
scripted test:

| Thread state        | Delivery                                                                     | Pinned by                   |
| ------------------- | ---------------------------------------------------------------------------- | --------------------------- |
| idle, process alive | new `user` frame / `turn/start`                                              | `test_turns.py` baseline    |
| idle, process gone  | `--resume` / `thread/resume`, then the input                                 | `test_turns.py` idle resume |
| turn active         | Claude: appended to the running tool's result; Codex: joins the running turn | `test_active_turn.py`       |

The injected input rides the same channel as the operator's own words, so it carries an
unmistakable machine envelope the system prompt explains, for example an `<agentplane-event>`
element with `kind`, `tool_call_id`, `status`, and the result or reason inside. A denial reason is
the one place the operator's words travel; the envelope attributes them correctly.

## Notification batcher

Decisions are one source among several that want to reach a thread: GitHub activity, Matrix
messages, subscriptions. A batcher sits in front of thread input:

- collects events per thread, tagged with source, kind, and ids;
- debounces with one constant window of a few seconds after the first event, then delivers one
  input carrying every held event in arrival order; no urgency levels, since an event that misses
  the window simply rides the next input;
- never merges events, so each item keeps its own ids and the model can act on them separately.

Five approvals clicked in one sitting arrive as one input, not five turns.

## Policy evaluation

Haku decides auto-approval; this section records the evaluation shape the redesign should keep,
independent of where the rules are written.

- One input document per request: actor, access profile, server, tool, arguments, plus resolved
  facts (derived target repository, its visibility, Kubernetes SAR verdicts, label existence).
- Facts are resolved in a phase between parsing the call and evaluating rules, by typed resolvers
  with I/O; rules are pure over input plus facts. A fact that cannot be resolved is a named
  `unknown` value the rules must treat as manual, never an exception that skips the rule.
- One decision document: `approve | manual | deny`, matched policy ids, reasons. Logged with its
  input so decisions replay.
- Partial evaluation derives two things the registry currently hand-maintains: which facts a call
  needs (evaluate with facts unknown; the residual names the concrete keys) and a tool's static mode
  (evaluate with arguments and facts unknown; no residual means unconditional pass-through, a
  residual means conditional, undefined means manual).
- The same engine answers three callers: Haku tool calls, native permission prompts the Agentplane
  bridge must acknowledge, and grant requests.

Verified with OPA 1.20 on a Haku-shaped Rego policy: the residual for a public-repository rule with
facts unknown is `data.facts.github.visibility["agentydragon/ducktape"] = "public"`, the exact
lookup to run before deciding. Partial evaluation cannot see through a top-level rule built from
`count()` and `else`, so the queries target the leaf `approve`/`deny` sets and Python assembles the
outcome. Whether the rules live in Rego (sidecar or WASM) or stay in typed Python is a later choice;
the input, facts, decision split is what to keep either way.

## Burn-down

1. Haku console: uniform argument schema with optional `rationale`; uniform result envelope; drop
   the gated-tool envelope and `wait_for_result_ms`.
2. Haku console: `get_tool_call` gains `wait_for` and `wait_ms`.
3. Haku console: decision events (approved, denied, completed, failed) published for Agentplane.
4. Agentplane: decision delivery by thread state with the machine envelope; system prompt text.
5. Agentplane: notification batcher with per-thread debounce.
6. Follow-up: withdraw pending calls of an archived thread with reason `thread archived`.
