# Guyton–Klinger experiment implementation

Proposed STUDY consumer, not an implemented reproduction. The
[source contract](README.md) owns primary evidence, study versions,
the declared three-sleeve adaptation and remaining convention decisions. This plan
owns the proposed code and leaves as its slices land; the [roadmap](../../plans/roadmap.md) owns
cross-component dependencies.

## Composition on current Augur primitives

- <run.py> drives the annual windows of
  <paths.py> through one caller-supplied batch policy
  (`BatchPolicy = Callable[[list[Decision]], list[DecisionActions]]`); its fixed
  nominal withdrawal is a plumbing placeholder. Trinity's sales-only funding,
  coupon cash and success boundary are **not** GK defaults.
- <../../sim/observations.py> supplies current lots/prices/basis, cash, CPI and typed
  receipts; exact sell/buy/consume actions own canonical settlement. Policies
  see observations, never the prepared future path arrays.
- <../../policy/sleeves.py> supplies FIFO funding proposals and exact quantity
  budgeting, not PMR. One PMR proposal must reserve each lot/cash quantum once
  across its ordered stages. Independent helper calls do not share reservations.
  Extract a small reusable quote/lot-selection operation only where this real
  consumer needs it; no mutable ledger, native policy enum or new action system.
- <../../sim/results.py> retains compact payments/holdings and selected detailed
  receipts/books. <../../x/joint_spending_allocation/policy.py> demonstrates separate
  author-owned intention records; study measurements must not replay accounting.

## Desired annual policy

**Sketch only.** `annual_actions` and `Memory` are proposed ordinary study-local
code, not new library APIs. Configuration names the resolved conventions.

```python
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.observations import Decision
from finance.augur.study.guyton_klinger.run import run

for cell in cells:
    memory = {rollout_id: Memory(cell) for rollout_id in selected_ids}

    def policy(batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                decision.rollout_id,
                decision.observation.month,
                annual_actions(decision.observation, memory[decision.rollout_id]),
            )
            for decision in batch
        ]

    rollouts = run(windows, policy, wealth=cell.wealth, weights=cell.weights, rollout_ids=selected_ids)
    # Analyze typed rollouts plus this cell's decision records.
```

`annual_actions` returns an empty list between reviews but can record the next
month's canonical post-withdrawal book as the investment-return denominator.
With no interim cashflows, the next annual opening value relative to that book
measures investment return without counting withdrawal as loss. Per-sleeve
returns come from saved/current observed unit prices. On review, one plan emits
PMR sales, consumption and any cash-sleeve reinvestment in explicit caller order;
the executor adds nothing. Fresh memory and the same paths drive selected replay.

## Next independently reviewable slices

1. **Complete annual policy:** settle ORDER/PORTFOLIO/OPENING, add spending and
   PMR together with actual session receipts. If split, label the spending-only
   control as such. Reuse/extract only the proposal pieces this consumer needs.
2. **Historical report:** retain original/reordered path identity, source and
   unconditional spending metrics; run a pinned sourced panel for the declared
   adaptation and explain substitutions/discrepancies. Keep source acquisition
   separate from offline CI.
3. **Optional 2006 stochastic comparison:** reconstruct the explicit joint annual
   model and compare the named tables with sampling uncertainty. Different random
   paths are acceptable; silently different distributions/rules are not.

Later policy controls: strict equality and
just-over/under guardrails; negative investment return versus withdrawal-induced
wealth decline; no inflation catch-up; one 10% adjustment, not repeated rescue;
the 15-year cutoff; overweight positive/negative sleeves and funding-source order;
nonoverlapping lot reservations; cash return and no double-counted payouts;
unfunded consumption retaining successful sales; exact exhaustion versus $1
terminal success; failed-path exclusion from source medians but inclusion of
known paid zeros in observed populations; and the real CLI on a tiny hand-checked
panel. Use independently calculated amounts, not snapshots copied from the
implementation. None requires a separate financial simulator.
