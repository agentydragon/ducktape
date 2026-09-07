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

## Lanes, in order

1. **A tiered spending ladder (#5480 -> #5481, #5482, #5483, #5484).** Finding 1 says the
   current objective cannot rank the candidates and finding 2 says which one can. #5482 also
   opens the policy seam SPEC gap 10 needs for a rolling ladder, so two lanes share it.

2. **Instrument specification.** Finding 5. A duration axis on the bond sleeve, and the muni
   arm credited its exemption rather than only charged its discount. Needs SPEC gap 8 (the
   curve is clamped flat past 10 years) before duration can mean anything past intermediate.

3. **The fitted model (#5509 -> #5487 -> #5488).** Finding 4 for the level, finding 6 for why
   the replay cannot substitute. Fit on 1926-2026 first: it is the cheapest, the data is
   already assembled by `load_macro_history`, and a model class cannot learn the Depression
   from a sample that starts in 1955.

4. **Trading friction then the joint sweep (#5486 -> #5485).** In that order: the sweep is
   meant to choose a rebalancing band, and today tax is charged on a trade while commission,
   spread and slippage are not, so tight bands are cheap for a reason that is an artifact.

**#5510 (resampling sampler)** gives honest confidence intervals in place of the crude
`sqrt(p(1-p)/n_eff)` Plan 0 prints. It cannot break the information limit in finding 6, so it
is useful rather than urgent.

**#3738 and #3740** predate the #5480 children and are subsumed by them. Close or link.
