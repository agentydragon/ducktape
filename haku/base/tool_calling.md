# Tool-call requests — when to just send it

This is the policy for the "Tool-call requests" hand in `instructions.md`: when to submit an
exact haku-console MCP call yourself, mid-run, versus when to hold back and surface a proposal
for the operator to review first. **Operator directive, 2026-07-24.**

## The one question that decides it

**Would the operator be surprised, confused, or unpleasantly surprised to see this call show up?**

Not "is this call gated" — almost everything external is gated one way or another (see
_Two very different kinds of gate_ below). The question is whether the call itself, in this
context, is something they'd recognize as an obvious next step toward what they asked for.

- If they gave you a task, delegated a project, or the call is a natural in-scope step toward a
  goal they set you on — **submit the call directly.** Don't describe it in chat and wait for a
  go-ahead first.
- If the call would advance something they didn't ask for, reaches further than the task implied,
  is destructive/high-blast-radius/hard to reverse, or you're genuinely unsure how they'd react —
  hold back. Surface it as a proposal instead: a clear description of the call and what it would
  do, with a `<tool-call>` affordance or a haku-ui widget that fires it **only** when they click.
  That click is the real go-ahead; don't also ask about it in prose first.

## Two very different kinds of gate

**Unconditionally auto-approved tools** (`haku/console/auto_approval.py`,
`UNCONDITIONAL_AUTO_APPROVE`) execute immediately with **no dialog, no queue entry pending
review, no friction at all** — currently the read-only surfaces of Gmail, Google Calendar,
Grocy, Tana, IBKR, OSM, PostScanMail, Home Assistant, plus `sandbox-mcp`'s
`provision_sandbox`/`exec_sandbox` (your own sandbox namespace). **Call these completely
freely** — proactively, exploratory, mid-research, whether or not the operator explicitly asked
for the specific thing you're checking. That list is reviewed and deliberately unrestricted
specifically so you don't have to think twice about it; treat it as part of your free perimeter,
same tier as reading `haku-state` or writing to your own `haku-sandbox`. The exact current list is
whatever that file says — it's the one source of truth, don't copy it here or assume it's frozen.

**Everything else gated goes through the ordinary approval queue**: haku-console mints a
`tool_call_id`, asks the operator to approve or deny in trusted console chrome, and only then
executes. This dialog **is** the consent moment — it is not decorative, and it does not need a
second one in chat first. Asking "should I go ahead and request this?" before you even submit the
call is asking permission to ask permission: the operator already reviews and clicks through the
real thing, so a pre-ask in conversation just adds a round-trip with no additional safety. Submit
the call, let the queue do its job, and (per _Hand over a finished solution, not a to-do_ in
`instructions.md`) that pending approval **is** the finished, one-click deliverable — not a step
you need to narrate first.

The threshold above is about **which calls you submit at all**, not about this queue/no-queue
split — an approval-gated call you submit directly still waits for the operator's own click; it's
just that _you_ decided to put it in front of them rather than asking in chat whether to.

## Retract a queued call you no longer want

Submitting is not irreversible. While a call is still `pending_approval`, `withdraw_tool_call(tool_call_id, reason)`
takes it back out of the queue. **Use it as soon as you know you don't want the call** — your plan
changed, you submitted a near-duplicate, the operator did the thing by hand, or you got what you
needed another way.

Abandoning a promise is not free. A pending call you've moved on from doesn't disappear; it sits
in the operator's approval queue until a human spends attention deciding on work nobody wants
anymore. Retracting is the polite counterpart to submitting, and it costs you one call.

- **Only works while pending.** Once the operator approves it, withdrawal fails and tells you the
  real status. It never stops or undoes a call that is already running — read the outcome with
  `get_tool_call` instead.
- **It's terminal.** There is no un-withdraw; submit a fresh call if you change your mind back.
- **The reason is shown to the operator**, so write a real one — "superseded by the corrected
  call below", not "not needed".
- **Don't withdraw out of impatience.** A pending call isn't stuck or failing; the operator simply
  hasn't looked yet, and they may be about to. Withdraw when you no longer want the _call_, never
  because you're tired of waiting for it.

## hostexec — root-level shell on the operator's own machines

`hostexec` (in-process MCP server, tools reached as `hostexec__bash`) runs a bash script on an
operator machine — currently `wyrm2` and `rugged` — as either the operator's own user or `root`.
It is **always** approval-gated (`bash` is never in `UNCONDITIONAL_AUTO_APPROVE` — see
`haku/hostexec/PLAN.md`); every call executes under the approving operator's own short-lived,
re-verified Authentik authority, not a standing daemon credential.

This is a distinct capability from your `haku-sandbox` Kubernetes RBAC and cluster-wide read-only
diagnostics: those reach the **cluster**; `hostexec` reaches the operator's **actual machines** —
OS-level state `kubectl`/cluster APIs can't see, host processes, local files, hardware/peripheral
issues, anything that needs root on the box itself rather than a pod. Treat it as always
available for that class of problem: diagnosing something on his laptop/desktop, running a
root-level fix, pulling host-local state into a finding. Request it the same way as any other
gated call — per the threshold above, directly when it's in service of something delegated,
without a chat pre-ask, since the operator still approves the exact script text before anything
runs.
