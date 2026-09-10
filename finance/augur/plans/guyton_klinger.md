# Guyton–Klinger experiment implementation

Proposed STUDY consumer, not an implemented reproduction. The
[source contract](../docs/guyton_klinger.md) owns primary evidence, study versions
and remaining convention decisions. This plan owns the proposed code and leaves
as its slices land; the [roadmap](roadmap.md) owns cross-component dependencies.

## Composition on current Augur primitives

- <../study/trinity/replay.py> demonstrates materialize-once paths, annual cadence,
  typed `compile_run` / `ActionSession`, original-ID replay and CLI artifacts.
  Its sales-only funding, coupon cash, success boundary and monthly windows are
  **not** GK defaults.
- <../sim/external_series.py> accepts typed multi-asset level blocks directly.
  The current <../model/historical_windows.py> macro/product composition has one
  equity index; do not force six equity histories into it or synthesize bonds
  from yields when the study source is an observed total-return series.
- <../rust/simulator.pyi> supplies current lots/prices/basis, cash, CPI and typed
  receipts; exact sell/buy/consume actions own canonical settlement. Policies
  see observations, never the prepared future path arrays.
- <../policy/sleeves.py> supplies FIFO funding proposals and exact quantity
  budgeting, not PMR. One PMR proposal must reserve each lot/cash quantum once
  across its ordered stages. Independent helper calls do not share reservations.
  Extract a small reusable quote/lot-selection operation only where this real
  consumer needs it; no mutable ledger, native policy enum or new action system.
- <../sim/results.py> retains compact payments/holdings and selected detailed
  receipts/books. <../x/joint_spending_allocation/policy.py> demonstrates separate
  author-owned intention records; study measurements must not replay accounting.

## Desired experiment shell

**Sketch only.** Imports below exist; `load_history`, `annual_paths`,
`make_scenario`, `annual_actions` and `Memory` are proposed ordinary study-local
code, not new library APIs. The small module split is evidence/path preparation,
annual policy, and run/report. Configuration names the resolved conventions.

```python
from finance.augur.rust.simulator import ActionSession, Decision, DecisionActions
from finance.augur.sim.backend import compile_run
from finance.augur.sim.results import Finished

history = load_history(source_files)  # Named, validated annual returns and CPI.
paths = annual_paths(history, start_years=start_years, years=years)

for cell in cells:
    prepared = compile_run(
        make_scenario(cell),
        rollout_count=len(start_years),
        external_series=paths,
        jurisdictions={},
        locations={},
    )
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

    session = ActionSession(prepared, "retiree", selected_ids, capture="summary")
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        # Analyze typed batch.rollouts plus this cell's decision records.
    finally:
        session.close()
```

For an annual-only control, the proposed path embedding holds prices/CPI within
the year and applies the annual move at month `12, 24, …`. Consumption occurs
only at `0, 12, …, 12*(years-1)`; the terminal `12*years` mark earns the final
return without another withdrawal. This is explicit annual arithmetic, not a
claimed monthly market path. Represent all sleeves, including earning cash, as
tax-free total-return proxy units; checking is only settlement cash/dust. Do not
add dividends again or imply the proxies support taxable stock simulation.

`annual_actions` returns an empty list between reviews but can record the next
month's canonical post-withdrawal book as the investment-return denominator.
With no interim cashflows, the next annual opening value relative to that book
measures investment return without counting withdrawal as loss. Per-sleeve
returns come from saved/current observed unit prices. On review, one plan emits
PMR sales, consumption and any cash-sleeve reinvestment in explicit caller order;
the executor adds nothing. Fresh memory and the same paths drive selected replay.

## Next independently reviewable slices

1. **Annual evidence/path consumer:** named panel loader and explicit windows;
   same CLI accepts a generated placeholder panel in CI. Hand-check one full
   year, final-year mark and earning cash. No published-rate assertion yet.
2. **Complete annual policy:** settle ORDER/PORTFOLIO/OPENING, add spending and
   PMR together with actual session receipts. If split, label the spending-only
   control as such. Reuse/extract only the proposal pieces this consumer needs.
3. **Historical report:** retain original/reordered path identity, source and
   unconditional spending metrics; run the pinned sourced panel and explain
   substitutions/discrepancies. Keep source acquisition separate from offline CI.
4. **Optional 2006 stochastic comparison:** reconstruct the explicit joint annual
   model and compare the named tables with sampling uncertainty. Different random
   paths are acceptable; silently different distributions/rules are not.

The first slice's actual CLI can use three generated paths with flat CPI and
annual withdrawal 10, starting wealth 100:

| Path | Returns                        | Independently calculated control                                                                                                                                                         |
| ---- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | +10%, then 0%                  | Post-withdrawal wealth 90; next opening 99; terminal 89 after the second withdrawal. No third withdrawal. The first investment return is positive despite wealth falling from 100 to 99. |
| 1    | +5%, then 0%, cash sleeve only | First year closes at 94.5 and the second at 84.5; cash is earning, not zero-return checking.                                                                                             |
| 2    | 0%, then 0%                    | Terminal 80. Selected replay `[2, 0]` returns these original paths, not rematerialized columns.                                                                                          |

Use a fine declared quantity scale and currency quantum; bound/report proxy
rounding rather than altering these independent arithmetic expectations. All
three are explicit annual controls, not claims of GK rule/table reproduction.

Later policy controls: strict equality and
just-over/under guardrails; negative investment return versus withdrawal-induced
wealth decline; no inflation catch-up; one 10% adjustment, not repeated rescue;
the 15-year cutoff; overweight positive/negative sleeves and funding-source order;
nonoverlapping lot reservations; cash return and no double-counted payouts;
unfunded consumption retaining successful sales; exact exhaustion versus $1
terminal success; failed-path exclusion from source medians but inclusion of
known paid zeros in observed populations; and the real
CLI on the same tiny panel. Use independently calculated amounts, not snapshots
copied from the implementation. None requires a separate financial simulator.
