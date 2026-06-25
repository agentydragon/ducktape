# delegation_scan (example)

A standing, cross-source pass whose single question is: **what here could a capable
AI agent take off the operator's plate — now, or if given one specific affordance?**
The operator wants to maximally exploit AI delegation, and treats the set of
delegatable work as large and growing (see `instructions.md` → _How you reason_,
the delegation-lever paragraph). This playbook is how you mine for it deliberately
instead of only noticing it on obvious chores.

## Inputs (triangulate — don't read one source)

- **Tana** (`tana_review`) — his primary task store; usually the richest seam of
  intentions he's tracked nowhere else. Pull open `#Task` nodes and recent daily
  notes.
- **Google Tasks** (`tasks`), **Gmail** (`gmail_triage`), **Calendar**
  (`calendar_prep`), **Drive** (`drive_activity`) — explicit and latent to-dos.
- **Your own `items/` and `memory/`** — existing open items that are really
  "delegate this," and the delegation register you maintain (below).
- **Your knowledge** — automatable angles he hasn't spotted (a service exists, an
  API exists, an agent could just do it).

## Triage each candidate

For every task-shaped or problem-shaped thing, ask in order:

1. **Already tracked / already done?** Cross-check before filing — much of this is in
   Tana or a prior item. If tracked, **advance it** (do the research, draft the
   artifact, compute the deadline), don't restate it. If a Tana checkbox is `[X]`,
   it's done — skip it (the `is:todo` flag lies; read the checkbox).
2. **Delegatable to an agent?** Could an executor with browser + code + research +
   his accounts/cluster carry it out? If yes, it's a `prepared_prompt` — write the
   outcome and embed the evidence.
3. **What would unlock it?** Doable today, or blocked on one affordance — an **API
   key**, an **MCP server**, a **service signup**, a **scoped credential**, a piece
   of **automation worth building**? If blocked only on an affordance, **name it in
   the item** so the operator can decide to provision it ("an agent could run this
   end-to-end given a Plaid write token / a Calendar-write scope / a $X/mo service").
4. **Value vs. his effort?** Rank per the contract. A task that becomes a
   click-to-approve agent handoff is worth more than the same task done manually.

## Group, don't spam

Cluster related tasks into one well-framed item rather than one item per line — e.g.
a change-of-address sweep, a subscription-cancellation sweep, or a "hand my NixOS /
device backlog to a coding agent" bundle pointing at the relevant ducktape configs.
The `prepared_prompt` enumerates the members with their evidence; the `body` shows
the operator the list.

## Keep a delegation register in `memory/`

Maintain `memory/delegation.md` (or similar): the running backlog of delegatable
work, what each piece needs to be runnable (already-possible vs. needs-key/tool/
service), and what you've **learned about what he values** — every accept / reject /
snooze / "was nothing" recalibrates which delegations are worth surfacing. Grow it
each run; this is what makes the scan compound over time instead of restarting.
