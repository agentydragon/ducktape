# Consolidation before features

The conventions this plan brings the package in line with are in `README.md` § Using Augur
and `AGENTS.md` § Conventions.

No new features land until this plan is done. Each numbered step is its own PR, and a step
leaves this file when it lands.

## Steps

1. **Plans and notes.** Fold the remaining files under `plans/` into one short plan that
   holds only live work. Delete resolved `debug/` notes after moving any durable lesson.
   Update `SPEC.md` and `sim/DESIGN.md` to describe what exists.

## Open questions

- **The app's model selection.** The app picks its economy model through
  `x/models/provider_config.py`, a union of every provider's configuration, and reaches it and
  the fitted models through a tombstoned visibility exception. That union is the pattern
  the conventions rule out. Deciding the app's model selection is part of deciding the app's
  future, which is not decided.
- **Held feature PRs.** #8139 (uncertain equity mean), #8141 (pinned equity mean), #8142
  (block bootstrap) and #8143 (trading costs) wait until this plan lands. The first three
  then target `x/`.
- **Integer money, later.** Integer quanta were chosen for speed at large rollout counts, and
  that gain was never measured. Once this plan lands, measure it. If it does not pay, `World` may
  instead know its currency and take and return exact fixed-point `Decimal` money.
