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
2. **Callers declare worlds directly,** one PR per group:
   - `study/guyton_klinger`, `study/trinity`;
   - `x/allocation_glide`, `x/bond_policies`, `x/bounded_spending`,
     `x/joint_spending_allocation`, `x/monthly_actions`;
   - the `sim/` tests that build worlds from scenario records;
   - the app: `product/scenarios.py`, `product/simulation.py`, `api/portfolio*.py`. Its API
     output must not change, and its existing tests are the check.
3. **Delete `sim/scenario.py` and `sim/compiler/`,** and remove the lowering step from
   `sim/README.md` and `sim/DESIGN.md`.
4. **Move unvetted models to `x/`,** each with its training code from `fit/`:
   - `structural_macro`, until it is shown to be good enough for core;
   - the VECM, the state-space models and the private-equity samplers;
   - `sample_sanity`;
   - the small samplers (`gbm`, `deterministic`, `independent`, `mirroring`, `composite`),
     or delete those with no caller.

   Historical replay (`model/historical_windows.py`) and the market-path infrastructure stay
   in core.

5. **Plans and notes.** Fold the remaining files under `plans/` into one short plan that
   holds only live work. Delete resolved `debug/` notes after moving any durable lesson.
   Update `SPEC.md` and `sim/DESIGN.md` to describe what exists.

Step 1 goes first, so later steps have a written target. Steps 2–3 are in order. Step 4
can follow step 1, and step 5 runs alongside.

## Open questions

- **Dollars helper.** The `Prepared*` facts are integer money quanta, so should `sim/money.py`
  keep a small `Decimal` dollars → quanta helper for callers? Nothing more than that.
- **The app after step 4.** The app picks its economy model through `model/provider_config.py`,
  a union of every provider's configuration. Once the fitted models are in `x/`, core can no
  longer import them, and that union is the pattern the rules above rule out. Deciding the
  app's model selection is part of deciding the app's future, which is not decided.
- **Held feature PRs.** #8139 (uncertain equity mean), #8141 (pinned equity mean), #8142
  (block bootstrap) and #8143 (trading costs) wait until this plan lands. The first three
  then target `x/`.
