# Interpolating prediction markets into trajectories (position)

Status: **position / framing doc** (2026-06-04). States what augur's exogenous `model` _is_
conceptually. The current execution target is the standalone `loom/` program.

Execution home (2026-06-09): this position is being built out as the standalone `loom/`
program — see `loom/PLAN.md`. augur consumes its WorldSet artifacts through a
bridge on augur's side.

Companion reading:

- `loom/PLAN.md` — how this framing is being built as a WorldSet-producing program.
- `augur/plans/whole_model_calibration.md` / `augur/plans/prediction_market_calibration.md` — the
  calibration machinery and PE-channel fit as they stand today.

## Position

augur's exogenous `model` is not an a-priori forecast that we sanity-check against markets. It is
an **interpolator**: it takes a pile of prediction-market _marginals_ — "when will OpenAI IPO",
"what will the S&P be on date Z", "P(CPI YoY ∈ [2%, 3%])" — and reifies them into a single **joint
generative process over world trajectories you can sample**.

The crowd supplies the **marginals**. augur supplies the **coupling** — the cross-correlations, the
time dynamics, and the typed numeric series the deterministic `sim` consumes — and turns the two
into worlds you can roll out. It is an operation that converts the wisdom of the prediction-market
crowd into a specific, sampleable generative model.

## Why this framing (the failure it fixes)

Hand-built a-priori models of exogenous unknowns — whether/when OpenAI IPOs, how large it gets —
produce nonsense, because we were **inventing the marginals** from thin structural guesses. The
markets already encode those marginals, far better than our priors do. So: stop guessing the
marginals; only supply the joint structure.

This demotes the structural "boxes" (an inflation process, an S&P process, a Markov mess for OpenAI)
from _source of truth_ to _a choice of coupling_. They are how we tie the marginals together into
coherent paths — not where the marginal beliefs come from.

## What a prediction market actually gives us

- A single market is **one marginal scalar**: `P(event)` or `P(quantity ∈ range)` at a horizon. It
  is not a trajectory, not a joint, and (for open markets) not a realized outcome.
- augur's value-add over "just the markets" is precisely that a market is a number while augur
  returns **worlds you can sample** — coherent joint paths consistent with _all_ the markets at
  once, which is what a downstream simulator needs and a pile of disconnected scalars cannot be.

## The boundary: `sim` vs `model` (non-negotiable)

- **`sim`** is deterministic mechanics written from the legal/financial **rules** — tax, mortgage,
  property, lifecycle. Never fitted, never learned; correct by construction.
- **`model`** is the exogenous stochastic world — markets, macro, companies. This is the thing being
  interpolated from prediction markets.
- The interpolator emits exogenous trajectories; `sim` runs your finances deterministically over
  each. These stay strictly separate: we never "fit" the tax code, and we never hand-author the
  exogenous marginals when the crowd has priced them.

## The core operation: marginals → joint as a min-KL projection

"Find a joint whose marginals are these markets" is underdetermined — infinitely many joints share a
marginal set. The principled choice is the joint of **maximum entropy** subject to the marginal
constraints, equivalently the **minimum-KL projection onto the constraint set from a base measure
`Q`**:

```
P*  =  argmin_P  KL(P ‖ Q)   subject to   marginals(P) ≈ market prices
```

`Q` is the base measure — _where the coupling lives_. Two natural choices, and they are the **same
operation with different `Q`**:

- `Q` = structured dynamics (the state-space macro + the PE Markov model): "stay as close as
  possible to my mechanistic priors while matching the crowd."
- `Q` = an LLM's world-prior: "stay as close as possible to what a generalist thinks plausible,
  while matching the crowd" — a much richer coupling than a hand-wired block diagram, at the cost of
  auditability and exact control.

Mechanically it is the same in both cases: draw `N ≈ 10k` trajectories from `Q`, then reweight /
exponentially tilt them so the reweighted empirical marginals land on the market prices. It is just
importance-reweighting an empirical sample to hit targets.

**The key consequence — a factorization of roles, not a tradeoff.** The coupling comes from `Q`
(history / structure / LLM); the marginals come from the markets; the min-KL projection fuses them.
It is _not_ "how much do I trust history versus the crowd." History/structure answers "how do these
quantities move _together_ and _over time_"; the markets answer "where do the individual quantities
land." Different questions, different sources.

## Markets disagree — so match marginals _softly_ (KL), not exactly

Prediction markets across platforms (Manifold, Kalshi, Polymarket) and across time **disagree**, and
a set of marginals can be **mutually inconsistent** — meaning _no_ joint reproduces all of them. The
inconsistencies are mundane and real:

- **monotone in threshold**: `P(S&P > 4000 by 2027)` must be ≥ `P(S&P > 5000 by 2027)`; noise /
  cross-platform pricing can report it backwards.
- **monotone in time**: `P(IPO by 2027)` must be ≤ `P(IPO by 2028)`.
- **partition sums**: an inflation bucket family should sum to ≤ 1.

This is a purely **mathematical** coherence question — does a joint with these marginals exist — and
has nothing to do with arbitrage or pricing (we never set or trade anything). When the input set is
incoherent, exact reification is _infeasible_, so we match **softly**:

```
minimize   Σ_m  w_m · D( p̂_model(m) ‖ price(m) )
```

a weighted divergence `D` (KL, or its quadratic approximation) per market, with weights `w_m` for
market quality (liquidity, time-to-resolve, platform). This is the soft / Lagrangian form of the
min-KL projection above, and it is why the framing was KL from the start: it degrades gracefully on
incoherent inputs — landing on a least-divergence compromise rather than failing — and lets
confident, liquid markets pull harder than thin ones.

(Optionally, pre-reconcile blatant violations — isotonic/monotone projection of a threshold or time
ladder — before reification, so the soft objective isn't spending its budget undoing obvious noise.
Open question below.)

## What this is _not_

- **Not market-making or pricing.** We never set or trade. "Coherence" means "a joint with these
  marginals exists," not "no arbitrage."
- **Not a skill backtest.** Matching current prices is _agreeing with the crowd by construction_.
  Whether the model is _right_ is a separate question only resolved markets can answer (below).
- **Not a replacement for the mechanistic `sim`.** The legal/financial machinery stays rule-derived.

## Validation — three honest and different things

1. **Reproduction.** Do the model's marginals land on the market prices? Where the inputs were
   mutually inconsistent, _how_ did it compromise (per-market residuals, weighted)? This is an
   in-distribution fit check, not a test of correctness.
2. **Trajectory sanity.** Are sampled joint paths plausible — cross-series sensible, no degenerate
   blowups? Time-consistency is _free_ from the base measure (a trajectory is a coherent path by
   construction), not a constraint we impose. The held-out **historical series tail** tests the
   dynamics the model invents _between_ the market-pinned points.
3. **Skill (future, data-poor).** As catalog markets resolve, score the model's
   probability-as-of-a-past-date against the realized outcome (a proper scoring rule), alongside the
   market price as of that date. The catalog is mostly open and far-future today, so the valuable
   investment is the plumbing to capture **resolutions + price-as-of-date** so this test set
   accumulates over time. It is the only PM evaluation that ever measures skill rather than mimicry.

## Implementation routes (sketch — current mechanics live in `loom/PLAN.md`)

- **Base measure `Q`.** Either the structured models we already have (`state_space` / `vecm` +
  `private_equity_risk`), or LLM-proposed trajectories. An LLM base must emit **typed numeric
  monthly series** (what `sim` ingests, not prose) at scale, and its raw marginals must be reweighted
  to calibrate — its strength is the coupling, not calibrated probabilities. A hybrid (LLM coupling,
  numeric reification layer) is plausible.
- **Reification.** Monte-Carlo sample `Q`; reweight/tilt to the market marginals under the
  weighted-KL objective. No differentiability of the rollout is required (score-function /
  gradient-free fit); closed-form marginals, where a base exposes them, are an optional
  variance-reduction shortcut.
- **Provenance.** The artifact records which markets and which historical split it was built from,
  surfaced on the calibration page (which markets shaped this model; what data, through what date).

## Open questions

- Which base measure — structured, LLM, or hybrid (LLM coupling + numeric snap)?
- Market-quality weights `w_m` (liquidity, horizon, platform) — how to set, and how to keep a single
  thin far-future market from dominating.
- **Correlated / near-duplicate markets** — many catalog markets are functions of the same latent
  (multiple S&P thresholds, BTC at several levels/dates). Don't double-count the same underlying;
  decide how to weight or merge them.
- Consistency repair — pre-project incoherent marginal ladders (isotonic in threshold/time) before
  reification, or let the soft objective absorb the incoherence?
- Which markets to admit at all, and how to keep the catalog's provenance honest.
