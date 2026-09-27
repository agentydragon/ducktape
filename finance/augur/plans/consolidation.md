# Consolidation before features

Augur is a set of building blocks, not a framework. A caller owns the rollout loop and uses
Augur's operations inside it, the way a PyTorch user writes their own training loop. A
strategy specific to one study (Guyton–Klinger, a glide, a spending ladder) is caller code,
not something core learns to configure.

No new features land until this plan is done. Each numbered step is its own PR, and a step
leaves this file when it lands.

## Rules

These graduate into `README.md` and `AGENTS.md` with step 1.

- **One way to declare a world:** build a `World` (`sim/world.py`), `declare_*` its
  month-0 state from the `Prepared*` facts (`sim/prepared.py`), `track` its actors, then
  `start()` and `step()`, or submit batches of actions through `ActionSession`
  (`sim/session.py`).
- **No scenario objects.** Nothing tries to represent every possible use case as one
  configuration value or enum.
- **No layer without a caller that needs it now.**
- **A model identifies itself with a string** (`model_id`), and results display that string.
- **Core never depends on `study/` or `x/`.** A model stays in `x/` until evidence shows it
  is good enough for core.

## Steps

1. **Conventions.** Add a "Using Augur" section to `README.md`, built around one short
   canonical loop: build the world, declare state, sample paths from the model the caller
   chooses, step or act each month, read results. Add the rules above to `AGENTS.md`.
2. **Callers declare worlds directly,** building the `Prepared*` facts themselves. `World`
   stays on integer quanta and unit-agnostic. Callers convert at their edge with a small
   helper in `sim/money.py` that holds the currency quantum and turns exact `Decimal`
   amounts into quanta, plus the existing quantity-scale and ppb helpers. The helpers that
   still read scenario records (`compile_income_sources`, `bond_income_categories`,
   `distribution_income_categories`, `level_series_demand`) move to reading the facts. One
   PR per group:
   - `study/guyton_klinger`, `study/trinity`;
   - `x/allocation_glide`, `x/bond_policies`, `x/bounded_spending`,
     `x/joint_spending_allocation`, `x/monthly_actions`;
   - the `sim/` tests that build worlds from scenario records;
   - the app: `product/scenarios.py`, `product/simulation.py`, `api/portfolio*.py`. Its API
     output must not change, and its existing tests are the check.
3. **Delete `sim/scenario.py` and `sim/compiler/`,** and remove the lowering step from
   `sim/README.md` and `sim/DESIGN.md`.
4. **Plans and notes.** Fold the remaining files under `plans/` into one short plan that
   holds only live work. Delete resolved `debug/` notes after moving any durable lesson.
   Update `SPEC.md` and `sim/DESIGN.md` to describe what exists.

Step 1 goes first, so later steps have a written target. Steps 2–3 are in order, and step 4
runs alongside.

## Open questions

- **The app's model selection.** The app picks its economy model through
  `x/models/provider_config.py`, a union of every provider's configuration, and reaches it and
  the fitted models through a tombstoned visibility exception. That union is the pattern the
  rules above rule out. Deciding the app's model selection is part of deciding the app's
  future, which is not decided.
- **Held feature PRs.** #8139 (uncertain equity mean), #8141 (pinned equity mean), #8142
  (block bootstrap) and #8143 (trading costs) wait until this plan lands. The first three
  then target `x/`.
- **Integer money, later.** Integer quanta were chosen for speed at large rollout counts, and
  that gain was never measured. Once this plan lands, measure it. If it does not pay, `World` may
  instead know its currency and take and return exact fixed-point `Decimal` money.
