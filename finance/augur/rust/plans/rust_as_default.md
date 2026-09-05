# Serving the web app from Rust, without calling JAX

The goal is that no request the web app makes reaches JAX. Every product endpoint now runs on
the engine `SimulationBackend` names, the selected-rollout trace included, so what is left is
one decision: whether that name defaults to Rust. That is where the 16x lives, and the gates
below are what it waits on.

**The exogenous model stays on JAX, deliberately.** `model/gbm.py` runs `jax.vmap` and
`jax.random.normal`, and `state_space.py`, `vecm.py` and `independent.py` import JAX; sampling
happens in `_scenario_and_sample` on every request whichever backend serves it. That is fine
and is not a gate. The line this plan draws is around the **simulation engine and the read
models over its output**, not around the process: JAX may sample the paths, and must not run
the simulation or shape what the product reads back.

Keeping it there also keeps a contract that porting it would break: a seed maps to sampled
paths, and re-implementing the PRNG elsewhere would change every number a stored seed
produces.

The plan compiler is likewise clear — `sim/compiler/` imports only `jaxtyping`, which is
annotations, and produces numpy.

## What the differential harness is and is not

JAX is not a known-good oracle. This branch found a frozen-rollout over-report and a
cross-rollout leak in JAX itself, and #5588 found _both_ engines applying the §63/§1(h)
deduction rule the same wrong way — floor ordinary taxable income at zero, then rate the whole
long-term gain on top, discarding an unused standard deduction. The engines agreed perfectly
and were both wrong. Two implementations agreeing catches divergent bugs and is blind to
shared ones by construction.

So the harness is a bug finder, and a productive one — two of this branch's four real bugs
came out of it — but it is not the confidence argument, and keeping JAX alive is not a gate on
the flip. The tests that can catch a shared bug are the ones that assert the statute instead of
the other engine: `differential/tax_law_test.py`, `sim/test_tax_statute_e2e.py`,
`sim/compiler/tax_test.py`. Growing those is worth more per hour than keeping a second
implementation, and it is worth the same whichever engine ends up serving.

`sim/testing/engine_acceptance.py` is the same argument one level up: it states what an engine
must answer and runs against each one alone, so both engines can fail it together. Rust rides
it through `rust/backend_test.py`, which asserts nothing of its own.

## Coverage: which refusals a real request can reach

`encode_fixture` raises `UnsupportedScenarioError` at eleven sites. One is still reachable from
what the product API accepts:

- **`MortgageFinancing.annual_rate_pct`**, via `_exact_ppb` on `annual_rate_pct / 100.0`.
  Refused past roughly eight decimal places in the percent, so reachable from an API client
  rather than a UI. The wire-side answer that closed `closing_cost_pct` applies here too, at
  parts-per-billion resolution rather than basis points — but see the gate below: this refusal
  is currently load-bearing as a test.

The rest cannot fire on a product request, because the wire is narrower than the scenario
model underneath it:

- `SpendIndex` offers only `none` and `inflation`, so no amount can be indexed by a series
  the fixture does not carry;
- the scenario builder never fixes a `price_per_unit` on a scheduled sale;
- it never tags transfer income as interest;
- all three property-expense obligations (HOA, insurance, maintenance) carry a
  `property_id`, so the partial-`deductible_fraction` refusal cannot fire.

Three more are portfolio- and catalog-side rather than request-side — exact per-unit lot
basis, bond coupon rate, TIPS period rate — and need their own pass over what the portfolio
source can produce, not over what a request can ask for.

**These reachability facts are the plan's own working notes, not guarantees.** Each one is a
property of code that can change; the flip needs the reachable refusal closed, and needs the
unreachable ones held by something other than this list.

## Gates

1. Find another proof that `SummaryBackend.RUST` really dispatches to Rust, then close
   `annual_rate_pct`. It is the last refusal a product request can reach, and
   `product_scenario_test` uses exactly that to show the RUST path is not quietly answering
   with JAX — the two backends agree by construction, so agreement cannot show it. Closing
   the refusal before replacing the proof trades a reachable 500 for an unnoticed
   misconfiguration, which is the worse of the two. A portfolio-side refusal (an initial lot
   whose per-unit basis is not exact) is the likeliest replacement, and it overlaps the next
   gate.
2. Establish that the portfolio source cannot produce a lot, bond or TIPS the encoder
   refuses.
3. Decide the within-failure-month phase ordering. Rust's answer is the chosen one in both
   channels, so the work is in the JAX scan: a phase's position inside the failing month has
   to become observable. No longer a red target — the harness stops comparing event frames in
   a failed rollout's failure month, so `known_divergence_test.py` is the only thing holding
   both engines' answers there, and a new divergence confined to that month will not be found
   for you. That trade is why this stays a gate rather than leaving with the red.
4. Grow the statute-level suites over the tax surface the product actually exercises. This is
   the gate that carries the confidence, and the only one that would have caught #5588. It
   does not block the flip, and it is not finished by it.

   Stated against the statute so far: §63 with §1(h) (an unused deduction shelters a gain),
   §1(h) stacking across the 0%/15% boundary, and the §1211(b) ordinary-offset cap. Asserted
   only as a bracket walk or as engine agreement, and therefore still open: §121 with §1250
   recapture and their ordering, §163(h)(3) acquisition-debt caps and the home-equity
   exclusion, the SALT cap and its year-indexed schedule, 27.5-year rental depreciation, the
   §6654 safe harbor, and the federal/state/own-issue interest exemptions. Judge each by
   whether a wrong reading would show up as a different number, not by whether a walk exists
   for it.

5. Flip the default.

## What "done" means

Every endpoint under `/api/product/projections/` answered without `sim/engine/jax_engine.py`
executing. All four simulator-backed ones — `metric_fan`, `terminal_distribution`, `summary`
and `rollout` — already run on whichever engine is named, so this is now a question about the
default and not about coverage. `portfolio`, `catalog`, `settings`, `deployment`, `calibration`
and the budget routes do not run the simulator.

JAX stays in the image and in the request path, sampling the exogenous paths. So the 16x is a
statement about the engine, and the speedup a request actually sees is smaller by whatever
share sampling takes — a share nothing here has measured. Measure it before quoting an
end-to-end number.

## Why it is worth it

500 rollouts x 60 months, dense output on both sides, one BuildBuddy runner class: warm
median 0.4768 s against 7.6724 s, peak RSS 2.92 GiB against 4.46 GiB. Cold is 5.1 s against
74.1 s, because JAX pays XLA compilation before it computes anything. Measurements and their
caveats: [../benchmark/README.md](../benchmark/README.md).
