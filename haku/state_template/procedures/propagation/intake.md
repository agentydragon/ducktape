# Propagation checklist — intake (operator feedback)

When the operator leaves feedback (an `intake/` note or a UI click), it's a direct instruction:

- [ ] the targeted surface — apply the feedback where it points (an item, the board, a memory note)
- [ ] `memory/` — fold standing guidance into the right memory file (don't re-learn it each run)
- [ ] `items/` — if the feedback implies a new finding or changes an item's status/value
- [ ] `responses/` — reduce operator affordance input: read each `responses/<slug>/<field>.yaml`,
      reconcile it into the item (e.g. `status: done` → set the item done + do any follow-up), then
      it's handled (the item's frontmatter is the truth; a superseded response is just git history)
- [ ] move the processed note to `intake/processed/`

FLOOR — see README. Operator feedback overrides defaults; never drop it silently.
