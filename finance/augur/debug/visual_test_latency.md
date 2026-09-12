# Why `//finance/augur:visual_test` takes ~10 minutes

Investigation of the 628 s wall time of `//finance/augur:visual_test` (BuildBuddy invocation
`0ff0b337-d786-49e2-843f-433b9ff529c4`; four comparison cases at 129–137 s each). Measured on RBE
with temporary instrumentation (not committed): an ASGI middleware timing every API request in the
in-process dev server plus per-phase timers in `_render_case` (invocation
`e465bf04-755c-4028-bc48-d6b9c7325c16`, 607.8 s, 8 passed), and a sequential harness that posts the
same request bodies through `TestClient` and profiles `ProductService.projection_summary` under
cProfile (invocation `0029e180-5e11-403b-9815-8ff69b922262`).

## Answer

The time is **server-side simulation, repeated**. One load of `_COMPARISON_URL` costs ~30 s of
which ~29.5 s is the dev server running three 32-rollout × 240-month product simulations back to
back; page waits, the double render's DOM checks, interactions and the screenshot are ≤ 7 s per
case combined. Each comparison case loads the page four times, and `ProductService` has no result
cache, so the four comparison cases run the same three simulations 16 times each (48 identical
simulations, ~470 s of the 608 s).

## How one comparison case turns into simulations

- `test_augur_pages_render` calls `_render_case` twice (first/second render for the determinism
  check), and `_render_case` itself does `page.goto` twice (load, then reload `page.url`), running
  `wait_ready` after each. Every case is **4 page loads**.
- On each load the product workspace fans out one `POST /api/product/projections/summary` per
  scenario (`product.tsx`, the effect over `projectionRequestEntries`), 120 ms after the portfolio
  fetch lands. The comparison URL has 3 scenarios: 3 POSTs per load, **12 per case**, byte-identical
  across loads and across the four comparison cases (middleware saw 6 distinct summary bodies in
  the whole run, 60 summary POSTs).
- `ProductService` (`product/service.py`) has no cache: every `projection_summary` call runs
  `_compile_product_run` (build_scenario → exogenous sample → materialize → `compile_run`) and
  `simulate_product_metrics` (`configured.execute` over 32 worlds × 240 months) from scratch.
  `_projection_lock` serializes the three concurrent POSTs, so a page load's server time is the
  **sum** of the three simulations.
- `page.goto(..., wait_until="networkidle")` does not return while those POSTs are in flight, so
  the simulation cost lands inside the `goto` phase, before `wait_ready` runs.
- Interaction cases also fire `POST /api/product/projections/rollout` (dense capture, one seed):
  0.7–1.2 s, 320 KB response, once per render.

## Per-phase breakdown, `product_distribution_multi` (135.2 s in the instrumented run)

| phase (per `_render_case`)          | first render | second render |
| ----------------------------------- | -----------: | ------------: |
| `goto` #1 (networkidle)             |       30.5 s |        30.3 s |
| `wait_ready` #1                     |       0.22 s |        0.21 s |
| `goto` #2 (reload)                  |       30.5 s |        29.0 s |
| `wait_ready` #2                     |       0.21 s |        0.15 s |
| `interact` (click + rollout fetch)  |       1.97 s |        1.90 s |
| `_wait_for_product_chart_geometry`  |      0.004 s |       0.005 s |
| `_take_stable_full_page_screenshot` |       6.35 s |        3.89 s |
| total                               |       69.8 s |        65.5 s |

Screenshots were stable on the second attempt every time (one 150 ms wait); the 4–6 s is two
full-page captures of a 600 KB PNG. `product_scenario_comparison` (no interaction) shows the same
shape: `goto` 31.2 / 29.7 / 30.0 / 29.9 s, everything else 0.2–1.1 s, total 123.7 s.

Inside one `goto` the middleware saw the three summary POSTs arrive together at +0.6 s and complete
at (cumulative, lock-serialized) ≈ 5.2 s, 15 s, 29–30 s. Differencing consecutive completions over
the 16 comparison-URL loads gives per-scenario server times of:

| scenario                          | server time per request | response |
| --------------------------------- | ----------------------: | -------: |
| Rent (no property)                |               5.1–5.5 s |    31 KB |
| Buy A (mortgage, $900k property)  |              9.6–10.6 s |    31 KB |
| Buy B (mortgage, $520k, HOA dues) |             13.6–14.4 s |    31 KB |

Sum ≈ 29.5 s per load, matching the `goto` phase within 1 s. For comparison the other cases:
`product_cash_runway` (h=48) 0.8–1.1 s per summary, `product_property_lifecycle` (h=240, cash
purchase with lifecycle events) 6.3–7.5 s, `product_distribution_failures` (h=120, spend 9000)
2.0–2.4 s.

## Where the server time goes

Sequential harness on RBE, same bodies the browser sent (byte-compared against the middleware's
logged bodies), `TestClient` end to end:

| request                      |   cold |   warm |
| ---------------------------- | -----: | -----: |
| summary Rent (h=240, n=32)   |  7.3 s |  5.1 s |
| summary Buy A                |  9.4 s |  9.4 s |
| summary Buy B                | 14.0 s | 13.7 s |
| rollout Buy A seed=1 (dense) |  1.6 s |      — |
| rollout Rent seed=1          |  0.4 s |      — |

Phase split of `projection_summary` for Buy A (9.41 s total): `_compile_product_run` 0.32 s
(`_scenario_and_sample` 0.09, `compile_run` 0.19, jurisdictions 0.004), **`configured.execute`
9.23 s**, `project_product_metrics` 0.007 s, `projection_summaries` 0.009 s, `plain_json` 0.001 s,
`json.dumps` < 1 ms. Rent: compile 0.36 s, execute 4.93 s. Response serialization and the
polars/numpy reductions are noise; the cost is the month loop.

Scaling (`projection_summary` wall, no profiler): Rent h=60/120/240 at n=32 → 1.8 / 3.2 / 5.2 s;
Rent n=8/16/32/64 at h=240 → 2.1 / 3.3 / 5.2 / 11.4 s. Buy A h=60/120/240 → 3.5 / 6.5 / 9.8 s;
n=8/16/32/64 → 2.6 / 4.7 / 9.8 / 19.8 s. Linear in horizon and in rollouts (no batching across
rollouts: `execute` steps 32 `World`s one Python call at a time), with ~0.7–0.9 s fixed cost.
Per-scenario cost tracks the number of claim payments: Rent settles 15,253 payments in 4.9 s, Buy A
27,860 in 9.2 s — 0.33 ms per payment in both.

cProfile of Buy A `projection_summary` (71.3 s under the profiler vs 9.4 s without — 97 M function
calls, so the profiler roughly 7× inflates; treat fractions, not seconds, as the finding):

```text
   ncalls  tottime  cumtime  filename:lineno(function)
        1    0.001   71.328  service.py:130(projection_summary)
        1    0.150   70.589  configured.py:77(execute)
     5569    0.037   52.721  world.py:687(settle_claims)
     5569    0.386   52.682  payments.py:245(settle_grouped)
10493769/74933 20.684 52.181 copy.py:119(deepcopy)
    27860    0.117   51.297  payments.py:86(execute)
    27860    0.509   49.100  payments.py:101(post_payment)
    27892    0.238   39.290  accounting.py:131(apply_entries)
  1103176    3.700   30.055  pydantic main.py:990(__deepcopy__)
     5569    0.049    5.229  world.py:277(open_month)
     5569    0.061    4.983  world.py:329(open_mortgages)
     5569    0.059    3.924  world.py:516(prepare_month)
     5569    0.087    3.804  held_bonds.py:92(advance)
     7141    0.012    3.777  configured.py:72(apply)      # allocation-policy sales
     7141    0.024    3.429  holdings.py:219(sell)
     5569    0.025    3.057  configured.py:67(observe)
     5569    0.244    2.018  configured_allocation.py:145(plan)
     5601    0.060    1.110  configured.py:31(product_row)
```

Top by internal time: `deepcopy` 20.7 s, `dict.get` 6.3 s (22 M calls, deepcopy memo), `_deepcopy_dict`
5.6 s, `builtins.id` 3.7 s (15 M), pydantic `__deepcopy__` 3.7 s, `_keep_alive` 2.6 s, pydantic
`hash_func` 1.3 s (1.7 M — `AccountRef` keys being re-hashed into the copied ledger dict).

`copy.deepcopy` is **73 % of cumulative time** and it is called from two places on every claim
payment (`payments.post_payment`, 27,860 calls for Buy A): `tax = deepcopy(accounting.tax)` — the
whole tax state, pydantic models included — and `Accounting.apply_entries`, which does
`candidate = deepcopy(self.ledger)` (the full `AccountRef → int` balances dict) to apply 1–3 postings
atomically. Rent shows the same shape (deepcopy 22.9 of 37.1 s, 61 %; 15,253 `payments.execute`).
Everything else in the month loop (`open_mortgages`, `held_bonds.advance`, the allocation policy,
`product_row`) is single-digit percent each.

The rest of the comparison case is small: `_wait_for_scenario_comparison` 0.13–0.32 s per call,
`_wait_for_product_chart_geometry` after interaction ≤ 5 ms, the interaction itself 1.6–3.6 s (a
dense rollout POST of 320 KB plus the DOM handshakes), the stable-screenshot loop 1–6.4 s (two
captures, always stable on the second).

## Remediation options, ranked by savings / effort

1. **Memoize `projection_summary` / `rollout` results in `ProductService`** (bounded LRU keyed on
   the frozen request model, invalidated never — inputs are pure). The visual test would then run
   3 comparison simulations instead of 48 and 1 of each rollout instead of 2–4: the four comparison
   cases drop from ~130 s to ~10 s each (~440 s saved, total ≈ 170 s), `product_property_lifecycle`
   from 33 s to ~12 s. Production benefits the same way (reload, Focus/Candles toggles, rollout
   re-selection all resend identical bodies). Smallest change; the lock stays.
2. **Drop the inner reload in `_render_case`** (`page.goto(page.url)` immediately after the first
   `wait_ready`). The determinism check already renders each case twice via two `_render_case`
   calls; the inner reload makes it four loads for two screenshots. Halves every case's server time:
   ~60 s per comparison case, ~240 s total. Independent of (1); with (1) in place its value is ~2 s
   per case. Check what the reload was guarding (URL state written back by `replaceSearchParams`
   after decode?) before removing it.
3. **Remove the per-payment `deepcopy` in the sim** (`payments.post_payment`'s
   `deepcopy(accounting.tax)` and `Accounting.apply_entries`'s `deepcopy(self.ledger)`): copy only
   what the transaction mutates (a shallow copy of `_balances`, and of the income map inside `tax`)
   or apply-then-rollback. Under cProfile this is ~2/3 of `execute`; un-profiled share not measured
   separately, but the per-payment cost (0.33 ms) and the linearity in payment count point the same
   way. Expect 2–3× on every product simulation, production included; medium effort, touches the
   ledger atomicity contract, needs `service_scale_test` numbers before/after.
4. **Shrink the comparison workload in the test** (`n=16` instead of 32 → ~halves each simulation;
   or one test loading `_COMPARISON_URL` once and taking the comparison, distribution, candles and
   focus screenshots in sequence). Cheap but weakens the determinism-per-case structure and the
   sampled-population realism; only worth it if (1)–(3) are rejected.
