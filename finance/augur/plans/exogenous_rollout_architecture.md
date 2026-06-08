# Exogenous rollout architecture: state-space macro + LLM-authored, PM-anchored OpenAI

Status: **plan / design note** (2026-06-06). The architecture the `augur/x/pm_reifier` spike converged
on — where the LLM belongs in producing augur's exogenous rollouts, and where it doesn't.

Companion reading:

- `augur/plans/interpolating_prediction_markets.md` — the position: the exogenous `model` is an
  **interpolator** of prediction-market marginals into a sampleable joint over trajectories.
- `augur/x/pm_reifier/README.md` — the throwaway spike (8 findings) this note distills.
- `augur/plans/prediction_market_calibration.md` / `whole_model_calibration.md` — the PM-fit + calibration
  machinery as it stands.

## The goal (restated)

augur is a financial what-if simulator. To roll out an individual world it needs **granular trajectories**
— the level of each specific instrument at each time — which prediction markets do not provide (they give
**marginals only**, on **coarse** quantities, with **sparse** coverage). So:

> Assuming we can trust prediction markets, how do we turn them + the data we have into **realistic,
> calibrated rollouts** (per-instrument trajectories) that **respect the PM marginals**?

## Three layers, and where the LLM belongs

| layer                         | what                                                                  | who models it                                  |
| ----------------------------- | --------------------------------------------------------------------- | ---------------------------------------------- |
| **`sim` (deterministic)**     | tax/mechanics + user policy (spend, rent-vs-buy). Reads world state.  | hand-written rules, not fitted                 |
| **Exogenous macro `Q`**       | liquid, data-rich series (S&P, inflation, rates, home/rent).          | **classical state-space**, fit on history      |
| **Exogenous sparse / events** | OpenAI (valuation path + tender/round/IPO/collapse). Sparse, complex. | **LLM-authored** parametric program, PM-fitted |

The split is the spike's main conclusion: **the LLM's value is highest exactly where there is no data to
fit, and ~zero where classical fitting already works.**

### Why classical for the macro layer

The spike benchmarked LLM-as-`Q` (several elicitation styles) against augur's hand-written state-space
baseline on the same teacher-forced scorecard. The LLM **matched but did not beat** it (state-space:
mean PIT 0.40, tail-escape 23%, one bias axis; the best LLM kernel landed in the same neighbourhood). For
liquid macro there is enough history that classical fitting wins or ties — the LLM adds labour, not
capability. So: **state-space models, fit on history, reweighted to the macro PMs** (the existing reify
path). Don't put the LLM in this loop.

(Spike detail: the calibrated LLM proposal — the "sharp joint kernel" — does work end-to-end, including
the forward reify with healthy ESS. It's just not worth its cost where the state-space already suffices.)

### Why LLM-authored for the sparse layer

OpenAI is the hard part: **sparse data, complex outcomes, and only weakly tethered** to anything we can
calibrate on. Crucially this is an **identification problem, not a modelling problem** — see below. Where
there is no data to fit, you need a **prior**, and the LLM's world knowledge (funding history, the AI-capex
narrative, plausible IPO timelines) is the only sensible source of one. So the LLM authors the structure;
classical machinery fits what's fittable.

## The OpenAI module

### Macro-conditioned generation gives the joint for free

Generate OpenAI **conditioned on each already-sampled macro trajectory**, not alongside it. Because the
OpenAI process reads the S&P path it rides on, the joint (risk-on lifts OpenAI; a crash drags it) is
produced directly — **no copula needed.** Macro trajectory + its OpenAI trajectory = one world. This slots
into augur's existing **PE-issuer** slot (the spike already modelled OpenAI there as discrete events +
valuation).

### What the AI-authored program is

A small **parametric event-and-valuation process**:

- **Event hazards** — tender/round arrivals, IPO timing (hazard rising over time), collapse — each a
  function of time _and_ the conditioning macro path.
- **Valuation dynamics** — log-valuation drift + vol + jumps at events, with drift/vol partly driven by the
  macro path.
- Two kinds of knob:
  - **θ (tunable)** — hazard rates, jump-size distributions, vols, IPO timing. _Fit_ to markets/data.
  - **Authored priors (fixed)** — the **macro-coupling strength** (how hard OpenAI co-moves with the S&P):
    the un-fittable part, left as a documented assumption with hand-set bounds, _not_ touched by the fit.

### Valuation ≠ what you can sell for: the payout layer

The headline post-money valuation is **not** the realizable price of a specific stake — and the realizable
price is what the user actually cares about. Several wedges:

- **Liquidation preferences / the preferred waterfall.** Headline valuation = latest _preferred_ price ×
  fully-diluted shares. Common (employee) stock is junior to the whole preferred stack, so in a modest
  exit preferred recover first and common is crushed: common ≠ valuation ÷ shares except in a clean exit.
- **The instrument.** OpenAI employees hold **PPUs (Profit Participation Units)** — not equity: a capped
  profit-share with its own payoff curve (capped upside, no ordinary liquidation preference), distinct
  from common/preferred and OpenAI-specific.
- **Secondary discount.** Private stock sells _below_ the last round (illiquidity, transfer restrictions),
  and the discount is itself macro-correlated (wide in risk-off, narrow when frothy).
- **Liquidity gating.** You can usually sell only in company-sanctioned **tenders** or post-IPO after
  lockup; each tender's **price** and your **allocation** (how much you may sell) are deal-set, often below
  the headline mark and capped.

This maps onto the layer split:

- **`model` (`Q`, exogenous):** company state (valuation path + events) **plus the frictions you don't
  control** — secondary discount, tender price, whether/how much a tender clears. Genuinely uncertain.
- **`sim` (deterministic):** given a realized company state + your cap-table position (instrument, share
  count, preferences, vesting) + your sell policy, run the **waterfall** to realizable proceeds, then
  taxes. The waterfall is mechanical given a valuation, so it is a sim rule, not a model unknown.

Implications: the OpenAI module must emit the quantities that determine the realizable price (per-instrument
tender/exit price + the liquidity-event structure), not just the headline valuation; **the PMs still anchor
the headline valuation** (your realizable proceeds are derived downstream, so the anchoring layer is
unaffected); the reference class is richer for headline valuations / IPO outcomes than for common-holder
realizable proceeds (secondary/tender prices are opaque), so lean on the deterministic waterfall + a
stochastic discount model for the latter. **Privacy:** the user's specific holdings (PPU counts, grant
terms, personal/Shareworks marks, reported tenders) stay in **gaffer-private**; this public note describes
only the mechanism.

### Anchoring = roll out and match (fit, don't reweight)

PM prices _are_ moments of the trajectory cloud (`price = E[indicator]` over rollouts: P(IPO by d),
P(valuation > v at z), P(tender by q)). So anchoring is **simulation-based moment matching**: choose θ so
the rolled-out indicator marginals match the market prices (black-box opt — CMA-ES / Bayesian opt — over
a low-dimensional θ).

**Fit θ, don't importance-reweight.** OpenAI PMs may sit in the _tail_ of a naive prior, and reweighting
can't lift mass the prior never proposed (the one-way-valve / ESS-collapse failure the spike hit
repeatedly on macro). Fitting moves the actual generative process, so coverage is guaranteed. A light
residual reweight on top is fine.

### Reference class: fit the event model on _past companies_, not just OpenAI

The biggest calibration win for the sparse layer: don't fit the hazards/jumps on OpenAI's ~handful of
resolved points — fit them on a **reference class of comparable companies**. Time-from-last-round to IPO,
late-stage valuation-step distributions, collapse/down-round rates, IPO first-day pop, time-to-exit — these
are estimable across many companies, turning "one data point" into a real sample.

The shape is **empirical Bayes / hierarchical**: the reference class gives the _shape_ of the event and
jump distributions; OpenAI's own funding history + its prediction markets _locate/shift_ it. So:

```
reference-class prior (fit on many companies)  →  OpenAI-specific likelihood (its funding history + PMs)  →  posterior event model
```

Candidate public sources (free / academic, avoid paid where possible): SEC EDGAR S-1 filings (IPO dates,
proceeds, valuations), Jay Ritter's IPO datasets (academic, time-to-IPO + first-day returns), public
unicorn / late-stage round lists, and major tech-IPO histories. (PitchBook/Crunchbase are richer but
licensed — use public proxies first.) This also gives a real **held-out calibration test** for the sparse
layer (PIT/CRPS on resolved companies), which OpenAI alone cannot.

### The gate stack — what "checking it works" means

| gate                                        | strength          | how                                                                |
| ------------------------------------------- | ----------------- | ------------------------------------------------------------------ |
| Match OpenAI PM marginals within ε          | hard, enforceable | rollout → score indicators → fit θ                                 |
| In-distribution on the **reference class**  | **real**          | held-out companies: do resolved IPO/valuation outcomes calibrate?  |
| In-distribution on OpenAI's funding history | weak but real     | run from an earlier date; do the actual public rounds land in-dist |
| Sanity                                      | hard              | valuation markets monotone in threshold; non-negative; event logic |
| Macro-coupling realism                      | **not gateable**  | authored prior + bounds + presets; a documented assumption         |

## Evaluation: goodness of fit on company trajectories

The reference-class layer is the only place the sparse model gets a real held-out test, so it needs real
metrics. A private-company trajectory is a **marked temporal point process with right-censoring and
competing risks** (events = rounds / tender / IPO / acquisition / death; marks = valuations; most
companies are unresolved) — a well-developed corner of stats, and it ports augur's existing calibration
mindset (PIT → uniform, proper scores vs a baseline) straight over.

**The complication that must not be ignored: censoring.** Most companies in any snapshot haven't IPO'd or
died — they are right-censored. The naive metric ("predicted vs actual time-to-IPO") exists only for the
_survivors_, i.e. the success-biased subset, so every metric below must be **censoring-aware** or the eval
silently measures survivorship bias. (Same for dataset entry — Crunchbase/EDGAR over-represent successes;
condition on when a company enters the observable set.)

### Calibration — are the probabilities honest (the point-process analog of our PIT work)

- **Event timing → time-rescaling theorem.** Transform event times by the integrated conditional intensity
  `Λ(t)=∫λ`; if the model is right, the rescaled inter-event gaps are i.i.d. **Exponential(1)** (test by
  KS / QQ-plot). This _is_ the PIT/rank-histogram generalized to event sequences.
- **Marks → valuation PIT.** At each round, the PIT of the realized valuation in the model's predicted
  conditional valuation distribution should be uniform. Identical machinery to the macro series.
- **Survival → D-calibration.** Bucket predicted survival / cumulative-incidence probabilities and check
  the censoring-corrected observed fractions land in each bucket.

### Skill / sharpness — is it better than trivial (always relative to a baseline)

- **Held-out NLL per event** of the marked point process — the proper likelihood-based fit; censored
  companies contribute `P(survived to censoring without resolving)`. Meaningful only **vs a baseline**
  (Kaplan–Meier + constant-hazard). The LLM-authored model earns its keep only if it beats the simple
  hazard model on held-out NLL _while staying calibrated_.
- **CRPS** for the continuous predictions (time-to-event, valuation) — sharpness + calibration in one
  proper number.
- **IPCW-Brier / time-dependent C-index** for the binary horizon questions ("IPO by date `t`") —
  censoring-corrected, and exactly the PM-shaped quantities, so they double as the PM-fit check.

### Population realism — does a simulated cohort look like reality

Per-trajectory scores miss aggregate shape, so also compare **simulated vs held-out distributions**:
Kaplan–Meier survival curves, the valuation **tail exponent** (these are heavy-tailed / power-law),
inter-round-time distribution, unicorn rate by cohort age — via energy distance / Wasserstein / MMD.

### Minimal viable scorecard

Start with **time-rescaling (events) + valuation-PIT (marks)** for calibration, and **held-out NLL +
CRPS vs a Kaplan–Meier / constant-hazard baseline** for skill. Everything relative, everything
censoring-aware.

### Transfer to OpenAI

The OpenAI prediction itself can't be validated (n=1, unresolved), so the **held-out reference-class
calibration is the transferable evidence** ("this model is calibrated on companies it didn't see"). Then
inference on OpenAI = posterior predictive conditioned on OpenAI's own history + its PMs: the
reference-class score warrants the _shape_; OpenAI's data + markets locate it.

## The honest caveat: identification, not modelling

You can make OpenAI's _marginals_ match the markets perfectly and **still** not know whether its _joint_
with the economy is right — there is no observable that constrains the coupling (one company, sparse
events, only a faint tether through S&P levels). No model, LLM- or hand-authored, can _learn_ a correlation
there is no data for. So the design **surfaces the coupling as a first-class, inspectable knob** (with a
couple of presets, e.g. "≈ independent of macro" vs "strongly pro-cyclical") rather than burying it in a
fitted number nothing constrained. The reference class fixes the _marginal_ event/valuation behaviour; the
coupling stays an explicit assumption.

## The agentic authoring loop

The LLM authors / revises the program **structure** (event types, hazard forms, coupling form, priors) in
a sandbox; a harness fits θ, runs the gate stack, and returns a validation report; the LLM iterates until
the gates pass. Run as an isolated agent (z.ai), Docker container, transcripts saved, spec-driven — "here
are the PMs and the data files; produce a program that passes these gates; keep iterating." The θ-fit and
gating are **classical** (optimizer + scoring), no LLM in that inner loop. Once gates pass, the fitted
program is a **standing artifact** augur samples — LLM out of the hot path, revised only when the standing
model starts getting surprised (its realized PITs drift).

## Next steps

1. **Prototype the OpenAI module** — a parametric event+valuation process conditioned on a macro path; a
   moment-matching fit to illustrative OpenAI PMs; scored against `augur/x/pm_reifier/openai_history.json`.
   Stand up the fit-and-gate harness on throwaway data first.
2. **Acquire the IPO / late-stage reference-class data** (SEC EDGAR, Ritter, public unicorn lists);
   define the held-out calibration test for the sparse layer.
3. **Keep / refine the state-space macro `Q`** and the existing PM reweight; no LLM there.
4. (Lower priority) the strong-known-cutoff-model leakage test from the spike — now less load-bearing,
   since the conclusion is "classical for liquid, LLM-as-prior for sparse."

## Pointers

- Spike + findings: `augur/x/pm_reifier/README.md`.
- Position: `augur/plans/interpolating_prediction_markets.md`.
- Existing PE-channel + calibration machinery: `augur/plans/prediction_market_calibration.md`,
  `augur/plans/whole_model_calibration.md`.
