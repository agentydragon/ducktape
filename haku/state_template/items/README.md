# `items/` — your current working model

This is **how you currently package what you surface to the operator**: as **items** — a
value-ranked board of recommendations, each embedding action affordances — rendered by your
[`ui/`](../ui/README.md) service and produced by your [`procedures/`](../procedures/README.md).

**It is your model, not a ducktape contract.** Your base manual (`haku/base/`) defines the
_job_ (surface the highest-value, lowest-effort things; reason; learn) and leaves the
_format_ to you. The item shape, statuses, value rubric, and affordances below are seeded
here so you have a working method — **evolve them freely** as long as your `ui/` and
`procedures/` stay coherent with whatever you choose. Nothing in base depends on this shape.

## Item files

One file per item: **`items/<slug>.md`**, where `<slug>` is a short readable kebab-case name
(`renew-passport`, `cancel-unused-subscription`). **The slug is the item's identity** — it's
how you find an item to update and how you avoid duplicating one (match a new finding against
the existing slugs; there's no separate id or dedup key). Rename the file if the framing
changes materially; git tracks it.

Each file is **typed YAML frontmatter + a markdown body**. Frontmatter validates against the
`ItemDoc` model in [`../ui/backend/models.py`](../ui/backend/models.py) (the live read/validate
contract; `tools/validate_state.py` gates it on every push):

```yaml
---
title: "Cancel the unused $14.99/mo streaming subscription"
value: 70 # 0–100, impact vs. operator effort
status: snoozed # open | in_progress | done | rejected | snoozed | expired
snoozed_until: "2026-07-16" # optional — the wake date
deadline: "2026-07-01T17:00:00-07:00" # optional — a hard gate
---
```

Only fields with a real reader live in frontmatter — no `id`/`source`/`dedup_key` (the slug is
identity; provenance goes in the body prose). Statuses:

- `open` — awaiting the operator **and actionable now or soon**. Only you create these.
- `snoozed` — deferred until a future date/condition (its **wake trigger**), hidden from the
  active board. Set by you (or the operator) when the only next step is to wait or to park a
  lower-priority item; set `snoozed_until` and note the trigger in your `memory/` watch-list.
- `in_progress`, `done`, `rejected` — the operator's calls, expressed through the item's status
  affordance (below); you reconcile them from `responses/` on your next run.
- `expired` — set by you when `deadline` passes.

## Gate by actionability, then rank by value

The board answers "what's worth doing now or soon" — not "here's a thing that exists." An
item earns the **active queue only if the operator's next step is something to do now or in
the near term**. If the next step is merely to **wait** — for a future date, a far-off
deadline, or an external event outside their control — it is _not yet actionable_, however
large the eventual payoff (a refund you can only confirm next month; a passport that expires
in four years). Don't surface those up front: keep them as a dated entry in your `memory/`
watch-list (no item needed), or, if substantial, file them `snoozed` from the start with
`snoozed_until` set — never `open`. Value and actionability are independent axes: a high-value
item can be not-yet-actionable, so never let a big payoff float a wait-item onto the front
page. A future `deadline` does **not** by itself make an item a wait-item — a hard deadline
can still be intensely actionable now; the test is whether there's a useful next action in the
near term, or only waiting.

`value` is 0–100, ranking **impact against the operator's effort** — what tops the board is
high payoff for little of their time. Anchors: 90+ = money or a deadline at stake and quick
to act on (a fee accruing, a time-sensitive reply); ~60 = clear net-positive task or a
worthwhile automation; ~30 = worth knowing, no urgency. A big payoff that demands a lot of
operator effort ranks **below** a small one they can approve in seconds. Calibrate against
rejection feedback over time.

## Affordances in the body — no separate action model

There is no typed action schema. The body is markdown that **embeds affordance widgets** from
the reviewed library (full set + syntax: [`../procedures/garden.md`](../procedures/garden.md)).
Use the affordance that best advances the user's goal: a handoff when another agent/human path is
right, a console-approved tool request when a connected privileged API can do the action, or a
custom haku-ui surface when the operator needs to review/edit a richer workflow. Common item
affordances:

- **Hand off to an executor** — `<handoff label="<short imperative>">` with the executor prompt
  **inside the tag** as a fenced code block (a multi-line prompt can't be a literal attribute;
  short ones may use `prompt="…"`). Aim high: that executor can browse, research, run tool
  chains, write code, synthesize — state the outcome and the evidence and let it work out the
  how. A **pure-FYI item** embeds no handoff — just prose.
- **Ask for an approved tool call** — `<tool-call request="<state_request_id>" label="…">` when
  Haku can author one exact operation under `tool_requests/`. This is for privileged external
  effects that haku-console should gate, execute, and audit.
- **Status control** — a `<signal-toggle field="status">` with `done`/`snoozed`/`rejected` choices
  (scope is the item's slug, injected by the card). When the operator picks one it writes
  `responses/<slug>/status` — an input event you reconcile next run (below). The status change
  _is_ the signal; you re-derive the follow-up from the item's own context.

````text
<handoff label="Cancel the subscription">

```text
Cancel the operator's unused streaming subscription: sign in at the provider's account page,
confirm the plan is the $14.99/mo tier last charged on the date noted, cancel it, and capture
the cancellation confirmation… (the full self-contained executor prompt)
```

</handoff>

<signal-toggle field="status" prompt="Update status?">
<choice value="done">Done</choice>
<choice value="snoozed">Snooze</choice>
<choice value="rejected">Dismiss</choice>
</signal-toggle>
````

**`body` prose and the handoff `prompt` each stand alone — neither may lean on the other.** They
serve two audiences that never see each other's text. The **body** is what the operator reads in
the UI: it must convey the finding, evidence, and what to do entirely on its own. The **handoff
prompt** is what an executor agent reads: it must embed all its own evidence (ids, dates, amounts,
links) and never refer back to the body. Repetition between the two is expected and fine; a
dangling cross-reference ("see above", "as the body explains") is not.

## Operator input — the `responses/` log

The operator's affordance interactions don't mutate items directly. A `<signal-toggle field="X">`
in item `<slug>` writes `responses/<slug>/X.yaml` = `{value, at}` (the file is the current answer;
git history is the append-only log). On your **next run** you **reduce `responses/`**: read each,
reconcile it into the item — e.g. `status: done` → set the item done (or retire it) and do any
follow-up the context implies; `status: snoozed` → set `snoozed`, pick a sensible `snoozed_until` —
then the response is handled (the item's frontmatter is now the truth; a superseded response is
just history).

## Links as affordances — give the operator the door, not directions to it

A link that lands them one click from the thing or action beats a paragraph describing how to get
there. Whenever you reference something addressable, link the most direct URL you can — **inline on
the natural words** in the body (plain URLs inside a handoff prompt). Three kinds:

- **Entities** → natural URL, anchored on the words where it's named ("[the reply](…)", "[the
  refund PDF](…)") — not a trailing `**Links**:` block. By source (generic URL shapes): **GitHub
  repo files** → `github.com/<owner>/<repo>/blob/<branch>/<path>`; **GitHub PRs / commits / CI** →
  their `…/pull/<n>`, `…/commit/<sha>`, `…/actions/runs/<id>`; **Gmail** →
  `mail.google.com/mail/u/0/#all/<messageId>`; **Drive** →
  `drive.google.com/file/d/<fileId>/view`; **Calendar** → the event's `htmlLink`.
- **Searches / views over a set** → link the search URL that surfaces exactly that set: Gmail
  `mail.google.com/mail/u/0/#search/<url-encoded query>`.
- **Actions / settings** → deep-link straight to the page (`example.com/account/settings/<x>`)
  instead of describing the click-path.

A URL **may include a token** if that's the direct path — your UI is operator-only (behind an
auth gateway) — but that's the only exception: never write a raw credential into an item's prose,
the log, or a commit message (base _Hard rules_).

## Writing tone for items

Titles ≤80 chars, imperative ("Kill the $14.99 subscription"). Bodies short: evidence, why it
matters, what to do. No filler, no hedging stacks.

**Rewrite items to current state — don't accrete patches.** When a later pass folds in new
evidence, **rewrite the whole body to read as if written fresh today**: integrate the new
information, re-order as needed, **trim anything no longer true**. Do not prepend a dated
`**Update <date>:**` block or demote prior text to `**Background**:` — the body is the current
state, not a changelog (git holds the history; the UI shows the last-scan time).

**A state-changing event forces a full re-derivation, not just a body edit.** When the reality
under an item moves (submitted / booked / arrived / resolved / paid), every field is suspect: the
`title` must become the operator's _next_ action (never a status report), `value` re-scores to what
_remains_ to do (a 95 decision becomes a 55 close-out once decided), `deadline` becomes the next
real gate or goes away, and the handoff prompt gets regenerated or dropped — plus any companion
surface the item spawned. Walk [`../procedures/propagation/items.md`](../procedures/propagation/items.md)
field by field; "I noted the event at the top" is the failure mode.
