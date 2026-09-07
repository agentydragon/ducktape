# Allocation program

Goal: pick a stock/bond split for a long retirement, and know how much to trust the answer.

Plan 0 is done — `x/allocation_sensitivity.py` measures which of the known gaps actually move
that answer. It changed the order of everything below, so its findings come first and the
lanes follow from them.

## What Plan 0 measured

Two-sleeve monthly-rebalanced portfolio, CPI-indexed monthly withdrawal, no taxes and no
policy, over 3 horizons x 3 withdrawal rates x 2 bond sleeves, on both samplers. Levels are
lower bounds and only the shape is meant to be read.

**1. Survival cannot rank allocations.** In **17 of 18** cells the survival-maximizing
allocation beats its runner-up by 0-2 points against ±0-24 points of sampling error. Above
roughly 40-60% equity survival saturates, so the metric has no gradient exactly where the
decision lives. A study that reports P(ruin) per allocation is not answering the question.

**2. The outcome distribution is what separates them.** 50-year payout, 3.5%, taxable sleeve:
60% equity survives 97% of windows at a 2.1x median real terminal multiple, 80% survives 99%
at 5.1x, 100% survives 97% at 10.3x. The choice is between outcome distributions at equal
survival. That is what #5480's tier ladder expresses and what a single ruin probability cannot.

**3. The missing bond/equity correlation does not decide the coarse answer.** The two samplers
pick the same allocation in 15 of 18 cells, and every disagreement is between cells within
noise of each other. `model/SPEC.md` gap 2 is real, and it is not the binding constraint on
"roughly how much equity".

**4. They disagree sharply on the LEVEL at long horizons, and that is a different gap.** At 50
years, 3%, 100% equity: replay 99% survival, fitted 85%. The fitted arm carries both a fatter
left tail and a fatter right tail than history did (at 40% equity, 2.2x median against the
replay's 1.1x, at lower survival). That is gap 3 — no mean reversion — more than gap 2. It
does not change which allocation wins; it changes what "safe" means, so it binds as soon as
the question is "how safe", not "which".

**5. A 120bp assumption about the bond sleeve moves the answer more than the choice of sampler
does.** 30 years, 3%, 0% equity: 79% survival on a taxable sleeve against 48% on a muni sleeve
priced at -120bp in a harness that models no tax. Same lesson as the Trinity all-bond cell —
a small level difference amplified by a survival boundary. Instrument specification deserves at
least the attention the macro model gets.

**6. The record's resolution degrades exactly where the question lives.** 3.3 independent
windows at 30 years, 2.5 at 40, 2.0 at 50. Retiring early is the case with the most at stake
and the least evidence, and no resampling technique creates observations — so the fine decision
has to come from the fitted model, which is why lane 3 exists.

## Before the lanes

Three things landed or were discovered after Plan 0 ran, and each is cheap enough that it
should happen before any lane starts.

**The published grid ran with drift rebalancing off.** `rebalance_tolerance` ships and
defaults to `None`, which means never. Every grid run that did not set it measured a
never-rebalanced portfolio, whatever the report said. Re-run the grid with it on at two band
widths, against common random numbers, and see whether the answer moves. This is a few hours
and it either validates the existing results or invalidates them; nothing below is worth doing
until it is known which.

**A downstream consumer still imports the deleted JAX entry point.** It cannot run at all, so
the grid it published cannot currently be reproduced. Port it onto `RustEngine.product_metrics`
and re-run the published grid unchanged. If the numbers move, the JAX-to-Rust migration changed
answers, and that outranks everything in this file.

**The tier ladder's rungs were never sensitivity-tested.** Plan 0 varied the withdrawal rate but
held the tier structure fixed. A less-trimmed lower tier, and an intermediate rung between the
extremes, are both cheap to add and plausibly move where the allocation floor sits — a ladder
whose bottom rung is close to its top rung needs less equity than one that steps a long way
down.

## Lanes, in order

1. **A tiered spending ladder (#5480 -> #5481, #5482, #5483, #5484).** Finding 1 says the
   current objective cannot rank the candidates and finding 2 says which one can. #5482 also
   opens the policy seam SPEC gap 10 needs for a rolling ladder, so two lanes share it.

   Two things this lane must get right rather than defer. #5484's output is a **panel of
   metrics over the rollout distribution** — P(holds the top tier) first, alongside P(ruin),
   time-in-tier and terminal quantiles — and explicitly not a fitted scalar utility, which
   would launder a modelling choice into an apparent answer. And #5489 (more than one price
   level) is load-bearing here, not adjacent: a lower tier in a different economy follows a
   different nominal price path, so a single price level is an assumption that the divergence
   is exactly zero, with an error that grows with horizon and cannot be signed a priori.

   #5801 (a continuous flex rule alongside the discrete transition) belongs here too, once
   the discrete ladder works. Real spending is not a step function, and the discrete version
   is the tractable special case rather than the intended model.

2. **Instrument specification.** Finding 5. A duration axis on the bond sleeve, and the muni
   arm credited its exemption rather than only charged its discount. Needs SPEC gap 8 (the
   curve is clamped flat past 10 years) before duration can mean anything past intermediate.

   #5797 (equity pays no distribution, and no tax on one) is the same lane from the equity
   side: a total-return series with no dividend has no tax drag, which flatters equity by an
   amount nobody has measured.

3. **The fitted model (#5509 -> #5487 -> #5488).** Finding 4 for the level, finding 6 for why
   the replay cannot substitute. Fit on 1926-2026 first: it is the cheapest, the data is
   already assembled by `load_macro_history`, and a model class cannot learn the Depression
   from a sample that starts in 1955.

   #5487 is not optional and not a refinement. The model's `rate_beta` fits to zero, so it
   structurally cannot represent flight-to-quality — bonds rallying while equities fall is
   the single mechanism that makes holding bonds worth anything in a drawdown, and a model
   without it cannot price the thing this program exists to price.

   Three issues extend this lane and are ordered after it, since each needs the joint fit
   first: #5798 (the equity premium is treated as known — price the parameter uncertainty),
   #5799 (fit against non-US evidence, not only the US century), #5800 (valuation-conditioned
   returns).

4. **Trading friction then the joint sweep (#5486 -> #5485 -> #5802).** In that order: the
   sweep is meant to choose a rebalancing band, and today tax is charged on a trade while
   commission, spread and slippage are not, so tight bands are cheap for a reason that is an
   artifact. #5486 is now urgent rather than deferred, because drift rebalancing ships and a
   band-width arm trades on drift alone — its turnover is no longer bounded by cashflow that
   was going to happen anyway.

   #5802 closes the lane: a sweep that reports a grid has not answered the question. The
   output is an optimum, a sensitivity around it, and an attribution saying which factors
   moved it.

5. **Glidepath (#5803).** A constant allocation is a modelling convenience, not a strategy
   anyone follows. Last because it multiplies the strategy space, and is only worth searching
   once the objective (lane 1) and the model (lane 3) can tell two strategies apart.

**#5510 (resampling sampler)** gives honest confidence intervals in place of the crude
`sqrt(p(1-p)/n_eff)` Plan 0 prints. It cannot break the information limit in finding 6, so it
is useful rather than urgent.
