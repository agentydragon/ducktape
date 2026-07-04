# Propagation checklist — items (state-changing events)

When the reality underlying an item **changes state** — the thing was submitted / booked /
arrived / resolved / paid / cancelled, a deadline passed, the operator did the task — do a
**full re-derivation**, not an annotation. Prepending "Update:" / "DONE:" to yesterday's
body is the failure mode: a stale item that reads as if the world hadn't moved.

Walk every field and re-derive it from the new state:

- [ ] `title` — the operator's **next action** (imperative), never a status report of what
      happened ("SUBMITTED — good job" is not a title)
- [ ] `value` — re-scored for **what remains** for the operator to do, not the historic
      stakes of the event (a 95 decision becomes a 55 close-out once decided)
- [ ] `deadline` — the next real gate, or removed; a passed deadline on an `open` item is
      always wrong (expire it, replace it, or drop it)
- [ ] `status` — still `open`? or `done` / `snoozed`-to-the-next-trigger?
- [ ] `body` — rewritten fresh (per `items/README.md`); every instruction it gives must be
      something still worth doing; stale drafts/wordings it embeds get replaced
- [ ] the body's `<handoff>` prompt — still the right ask for an executor? Regenerate or delete; an
      agent must never be handed a prompt about a world that no longer exists
- [ ] the body's affordances — the status toggle and any other widgets still fit the new next step
- [ ] **Companion surfaces the item spawned** — a dedicated UI page/tab, a prepared draft, a
      board section, a memory watch-note: each re-derived or retired with the item
- [ ] Related items — does the change move a sibling (e.g. a downstream item that was waiting on
      this event)?

FLOOR — see README. The test for "done walking": read the item (and its companions) as if
fresh; nothing visible may claim, ask, or urge anything that yesterday's world made true
but today's doesn't.
