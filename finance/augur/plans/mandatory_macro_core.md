# Mandatory exogenous macro core

Making CPI, a nominal term structure, and a muni/Treasury ratio things every exogenous
sample carries, rather than things a scenario may request. Prerequisite for bonds: a
nominal dollar in month 240 means nothing without CPI, and a bond cannot be marked
without a curve.

The framing is Rai's:

> augur is supposed to insofar as possible simulate the real financial mechanics once all
> the messy unpredictable stuff of the real world is in the "exogenous" bucket. […]
> inflation and probably fed rates too should become proper baked in things the exogenous
> part has to provide

This does not conflict with the `REQUIREMENTS.md` non-goal. That entry puts inflation
modeling in `augur/model` as an exogenous path input rather than a simulator-owned
stochastic layer, and mandatory-exogenous is exactly that shape — inflation stays
exogenous, it just stops being optional. Nominal simulation and real-terms-as-
postprocessing both survive untouched.

## The finding that reorders the work

**A raw signed rate cannot be a `LevelSeriesKind`.** The level stack is multiplicative and
log-based end to end, and positivity is enforced — not assumed — in at least ten places:

| Site                                                    | What it enforces                                                                                                      |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| <../model/path_models/scenarios.py> `:27`               | `HistoricalSeries` rejects non-positive levels outright                                                               |
| <../model/vecm.py> `:263`                               | `fit` works on `np.log(levels)`; the whole generative model is stated in log space                                    |
| <../model/state_space.py> `:91`, `:208`, `:245`, `:388` | artifact validator, additional-factor validator, `math.log(latest)`, post-sample `levels <= 0` check                  |
| <../model/exogenous.py> `:466`, `:486`                  | anchoring is a multiplicative rescale by month-0, so a zero month-0 is unanchorable and a negative one flips the path |
| <../model/sample_sanity.py> `:404`, `:424`, `:598`      | ratio bands divide by month-0; `require_positive` defaults to `True` and raises rather than reporting a band miss     |
| <../fit/metrics.py> `:133`, `:227`, `:337`              | every scorer takes `np.log(historical.levels)`                                                                        |

A 0.00% nominal yield is unanchorable and a negative real yield raises. So there are two
honest routes, and the choice belongs before any code:

- **Positive transform.** Carry a discount factor, a bond price, or `1 + y` instead of the
  yield. Touches nothing in the table above and stays inside the existing joint fit — which
  is the property the whole exercise depends on (see below). Costs: breakevens and the
  muni/Treasury ratio have to be derived at the consumer rather than read off a factor, and
  a negative real yield is only representable through the transform.
- **Sibling bundle block.** A `MacroCoreBundle` field on `SampledExogenousBundle`, modeled
  on `PrivateEquityBundle` (<../model/private_equity_bundle.py> `:112`-`:175`) — the one
  existing pattern in this codebase that is **mandatory by construction**: every channel is
  a required column and producers go through an all-keyword constructor, so a missing
  channel is a `TypeError` rather than a validation error later. Sidesteps every positivity
  constraint because it defines its own per-channel validators. Costs: it is outside the
  factor basis, so keeping it jointly distributed with equities takes deliberate work.

**Recommendation: the positive transform**, because of the correlation requirement below.
A sibling block that is sampled independently would be worse than no rates at all.

## The correlation is the whole ballgame

If rates are sampled independently of the equity/inflation block, the model cannot produce
2022 — equities and bonds falling together because rates rose with inflation. That is
precisely the state a floor exists to survive, so an independent rate sampler would
systematically understate the risk the entire allocation question is about, and every
answer it produced would be optimistic in the one regime that matters.

<../model/state_space_factor.py> is the existing precedent: it is the one place a
covariance basis already spans heterogeneous factors (level series **and** private-equity
marks), fitted as one joint monthly-log-return distribution.

Two landmines there, both easy to miss:

- **`state_space_factor.py:55`** hand-spreads the four level-key classes instead of using
  `LevelSeriesKey`. A new level kind is therefore dropped from the state-space basis **with
  no type error**. This is the single highest-miss-risk line in the change.
- **`state_space.py:528`-`:537` (`_coupling_allowed`)** — a rate factor falls through to
  `return True` and picks up the 0.5 on-block shrinkage against every macro factor by
  default. Crypto and PE marks short-circuit above it; rates would not. Whether a rate
  couples with home values and rents at 0.5 is a modeling decision and belongs in an
  explicit branch next to `_CRYPTO_SYMBOLS`.

## What "mandatory" has to mean structurally

`SampledExogenousBundle` (<../model/exogenous.py> `:222`-`:232`) has **no `__post_init__`
and every field defaults to empty**, so nothing today can express a producer-side
obligation. The only validation is `validate_sample_satisfies_request`, which is a
per-request consumer contract. Adding the obligation collides with three things:

- **PE-only samplers emit no level series at all** — `trained_private_equity.py:106`,
  `private_equity_risk.py:389`, `private_equity_trajectories.py:177`. A bundle-level
  invariant breaks all three unless it lives only at the `CompositeModel` boundary.
- **VECM is pull-only** (<../model/vecm.py> `:328`-`:333`): it emits nothing that was not
  requested, so a mandatory core is never emitted by it unless something requests it.
- **The demand path is derived, not declared.** `product/service.py:213`-`:220` builds the
  request from `scenario_level_series_keys(scenario)`, deliberately so there is "one answer
  to what does this need, not two that must agree". Mandatory means _not_ derived, which
  contradicts that comment — so the comment gets rewritten or the core gets unioned in at
  that one site.

## Evidence

`DGS2`, `DGS10`, `DGS30`, `DFII10` are **absent** — zero hits repo-wide. The only
rate-adjacent series is `MORTGAGE30US` (<../../evidence/sources.py> `:93`), and it is not a
level factor at all: it rides as a scalar anchor on the provider configs.

Adding them is mechanical (a `_fred(...)` constant, an `EVIDENCE_SOURCES` entry, a loader,
an `_ABSOLUTE_LEVEL_SOURCES` row, an anchor branch in `fit/state_space.py:86`-`:105`) with
one non-obvious cost: <../fit/evidence_data.py> `:187` aligns factors with an **inner**
join under `MINIMUM_ALIGNED_MONTHS = 36`. DFII10 starts in 2003 and DGS30 has a 2002–2006
gap, so adding either **truncates the fit window for every existing factor**. Decide
whether the rate factors join the main aligned frame or are fitted on their own window and
bordered in via `StateSpaceAdditionalFactor`.

## Order of work

1. Choose the representation (positive transform vs sibling block). Everything else depends
   on it and nothing else is worth writing first.
2. Evidence: add the FRED series, measure what the inner join does to the aligned window,
   and decide main-frame vs bordered-in before fitting anything.
3. The factor basis: `state_space_factor.py:55`, then a deliberate `_coupling_allowed`
   branch, then refit and score with <../fit/metrics.py>.
4. `sample_sanity` bands that would actually catch a wrong sign — P(10y nominal < 0) ≈ 0,
   and the equity/rate correlation in a high-inflation slice having the right sign. A band
   that only checks marginals would pass a model that cannot produce 2022.
5. Only then the contract change: the required block, the `CompositeModel` boundary, and
   the consumers in `sim/compiler/series.py` and `sim/external_series.py`.

`fit/train_round_trip_test.py:61` asserts emitted keys `==` requested keys and will need
updating at step 5; it is the canary for the whole contract change.

## Not in scope

Deriving real yields as nominal − expected inflation rather than modeling them separately —
worth doing (it enforces no-arbitrage between TIPS and nominals and yields breakevens for
free) but it is a consumer-side derivation, so it waits for the bond instrument.
