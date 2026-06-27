# `items/` — your current working model

This is **how you currently package what you surface to the operator**: as **items** — a
value-ranked board of recommendations, each with action affordances — rendered by your
[`ui/`](../ui/README.md) service and produced by your [`procedures/`](../procedures/README.md).

**It is your model, not a ducktape contract.** Your base manual (`haku/base/`) defines the
_job_ (surface the highest-value, lowest-effort things; reason; learn) and leaves the
_format_ to you. The item schema, statuses, value rubric, and affordances below are seeded
here so you have a working method on day one — **evolve them freely** (change the schema,
add action kinds, rethink the unit) as long as your `ui/` and `procedures/` stay coherent
with whatever you choose. Nothing in base depends on this shape.

## Item files

One file per item: `items/<id>.yaml` where `<id>` is a ULID you generate. Files validate
against [`../schema/item.json`](../schema/item.json) (also yours to change). Statuses:

- `open` — awaiting the operator **and actionable now or soon**. Only you create these.
- `snoozed` — deferred until a future date or condition (its **wake trigger**), hidden
  from the active board. Set by the operator, **or by you** when an item isn't yet
  actionable (its only next step is to wait) or to park a lower-priority item; set
  `snoozed_until` to the wake date (the run procedure checks it each pass) and note the
  trigger in your `memory/` watch-list.
- `in_progress`, `done`, `rejected` — set by the operator (you may set them when intake
  says so).
- `expired` — set by you when `deadline` passes.

## Gate by actionability, then rank by value

The board answers "what's worth doing now or soon" — not "here's a thing that exists." An
item earns the **active queue only if the operator's next step is something to do now or in
the near term**. If the next step is merely to **wait** — for a future date, a far-off
deadline, or an external event outside their control — it is _not yet actionable_, however
large the eventual payoff (a $4k refund you can only confirm next month; a passport that
expires in four years). Don't surface those up front: keep them as a dated entry in your
`memory/` watch-list (no item needed), or, if substantial, file them `snoozed` from the
start with `snoozed_until` set — never `open`. Value and actionability are independent
axes: a high-value item can be not-yet-actionable, so never let a big payoff float a
wait-item onto the front page. A future `deadline` does **not** by itself make an item a
wait-item — a hard deadline can still be intensely actionable now; the test is whether
there's a useful next action in the near term, or only waiting.

`value` is 0–100, ranking **impact against the operator's effort** — what tops the board is
high payoff for little of their time. Anchors: 90+ = money or a deadline at stake and quick
to act on (a fee accruing, a time-sensitive reply); ~60 = clear net-positive task or a
worthwhile automation; ~30 = worth knowing, no urgency. A big payoff that demands a lot of
operator effort ranks **below** a small one they can approve in seconds. Calibrate against
rejection feedback over time.

## Action kinds

(`action` itself is **optional** — omit it for a pure FYI item with no primary button.)

- `suggestion` — FYI / "do this yourself"; no machine payload.
- `prepared_prompt` — the workhorse, for anything worth handing to an agent with more than
  read-only access. `prompt` must be self-contained: embed the evidence (ids, dates,
  amounts) and the desired outcome so the executor session needs no archaeology. Write it
  as instructions to a capable agent with full access, not to you. **Aim high**: that
  executor can browse, research, run multi-step tool chains, write code, and synthesize
  across sources — so state the outcome and the evidence and let it work out the how; don't
  shrink the ask to one mechanical step when the real win is bigger.

**`body` and `prompt` each stand alone — neither may lean on the other.** They serve two
audiences that never see each other's text. The **`body`** is what the operator reads in
the UI: it must convey the finding, the evidence, and what to do entirely on its own (the
operator may never open the prompt). The **`prompt`** is what an executor agent reads: it
must embed all its own evidence (ids, dates, amounts, links) and never refer back to the
body. So don't split one thing across the boundary — e.g. don't bury an inbox-cleanup
cluster table only in the `prompt` when the operator would want to see and click it; put it
in the `body` too. Repetition between the two is expected and fine; a dangling
cross-reference ("see the clusters above", "as the body explains") is not.

## Links as affordances — give the operator the door, not directions to it

A link that lands them one click from the thing or action beats a paragraph describing how
to get there. So whenever you reference something addressable, link the most direct URL you
can — **inline on the natural words** in the `body` (plain URLs in `prepared_prompt` text).
The UI renders Markdown, so use `[text](url)` (and ``[`code`](url)`` for file paths). Three
kinds:

- **Entities** → natural URL, anchored on the words where it's named ("[Ivan's reply](…)",
  "[the refund PDF](…)") — **not** a trailing `**Links**:` block (fallback only for an
  entity never named in prose); link each once, at its first or most natural mention. By
  source: **ducktape files** → `github.com/agentydragon/ducktape/blob/devel/<path>`;
  **GitHub PRs / commits / CI runs** → their `…/pull/<n>`, `…/commit/<sha>`,
  `…/actions/runs/<id>` URLs; **Gmail messages** → `mail.google.com/mail/u/0/#all/<messageId>`;
  **Tana nodes** → `https://app.tana.inc?nodeid=<nodeId>` (the `nodeId` comes from the Tana
  MCP response); **Drive files** → `drive.google.com/file/d/<fileId>/view` (the `id` /
  `webViewLink` comes free from the same `files.list`/`files.get` call); **Calendar events**
  → their `htmlLink`. Plaid transactions have no public URL — reference them by date +
  merchant + amount.
- **Searches / views over a set** → when an item points at a _set_ the operator might work
  through (an inbox cluster, a category of charges, a label), link the **search URL that
  surfaces exactly that set**: Gmail `mail.google.com/mail/u/0/#search/<url-encoded query>`
  (e.g. one per cluster, `in:inbox from:(a.com OR b.com …)`).
- **Actions / settings** → if you tell them to change a setting or do something on a
  platform, **deep-link straight to that page** when you know it
  (`foobar.com/account/settings/<x>`) instead of describing the click-path. Same for an
  executor in a `prepared_prompt`.

A URL **may include a token** if that's the direct path — your UI is operator-only (behind
Authentik) — but that's the only exception: never write a raw credential into an item's
prose, the log, or a commit message (see base _Hard rules_).

## Operator action buttons (`actions[]`)

An item may carry an `actions` list — buttons your UI renders as **click / un-click
toggles**. The UI is dumb about _meaning_: clicking records a marker under
`clicks/<item-id>/<action-id>`, un-clicking deletes it (each a commit its backend makes);
it never runs the action. **You** give the meaning on your next run (the run procedure's
_reduce operator clicks_ step): for each click present, do the action's `intent`, then
delete the click. So attach whatever fits — the standard set
(`snooze`/`reject`/`done`/`raise`/`lower`, all `kind: command` whose `intent` you interpret
into a status/score change) plus item-specific ones ("compare cleaner options").
`kind: claude_handoff` actions carry a `prompt` and render as an inline `claude.ai/new`
deep-link instead (no click state), opened via the console's `openLink` bridge since the
sandboxed iframe can't open links itself. A free-form **feedback** box writes a new
`intake/` note. The starter `ui/` implements all of this; it — and any new affordance — is
yours to evolve.

## Writing tone for items

Titles ≤80 chars, imperative ("Kill $14.99 Hooli subscription"). Bodies short: evidence,
why it matters, what to do. No filler, no hedging stacks.

**Rewrite items to current state — don't accrete patches.** When a later pass folds in new
evidence, **rewrite the whole body to read as if written fresh today**: integrate the new
information into the natural flow, re-order as needed, and **trim anything that's no longer
needed or true**. Do **not** prepend/append a dated `**Update <date>:**` block or demote
the prior text to `**Background**:` — the body is the current state, not a changelog (git
holds the history; the UI shows the last-scan time). Structure (short headings, bullets) is
fine; lazily layering each pass's edit on top is not.
