# Allocation and spending experiments

This is the experiment backlog accompanying the
[modularization landing plan](roadmap.md), which owns dependencies and decision
gates. The question is how spending flexibility and allocation jointly change
the distribution of acceptable lives—not which allocation wins a single ruin
probability. The experiment author chooses preferences and any selection rule.

## Acceptance consumers

| Consumer                                                                                                                                                                            | What to implement or retain                                                                                                                                                       | Seam it exercises                                                                                                                   |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Trinity                                                                                                                                                                             | Retain the existing replay and explicit deviations as a numerical control for remaining refactors.                                                                                | Canonical execution, annual timing and a stable numerical control.                                                                  |
| [Guyton–Klinger (2006)](https://www.financialplanningassociation.org/article/journal/MAR06-decision-rules-and-maximum-initial-withdrawal-rates)                                     | A runnable rule-combination experiment, including portfolio management as well as spending. Settle rule order, source data and conditional purchasing-power statistics at GS.     | Existing batch policies, path-local memory and consumption receipts; GS pins study-specific ordering and metrics.                   |
| [Vanguard-style dynamic spending (2021)](https://www.vanguard.co.uk/content/dam/intl/europe/documents/en/whitepapers/sustainable-spending-rates-in-turbulent-markets-uk-en-pro.pdf) | Extend the existing bounded-spending example over allocation/flex settings, with our own paths and declared substitutions.                                                        | Executable spending already exists; add spending-quality measurements. Proprietary forecast replication is not required.            |
| [Pfau–Kitces rising glide paths (2014)](https://www.financialplanningassociation.org/article/journal/JAN14-reducing-retirement-risk-rising-equity-glide-path)                       | Compare static, rising and falling allocation on shared paths, with the chosen rebalance convention.                                                                              | Existing batch allocation helpers and zero-target exits; GS pins study conventions. Report shortfall severity as well as frequency. |
| Personal spending × allocation                                                                                                                                                      | Synthetic public book, private downstream actual lots; current/trimmed spending anchors, continuous flexibility, fixed and changing allocation, explicit trading costs and taxes. | RUN, TAX and ROBUST; HOUSE/BOND/MOVE only for arms using those capabilities.                                                        |
| Dated-bond hold/sell/roll                                                                                                                                                           | Retain the supplied-curve example as a control; add a native-position version after BOND.                                                                                         | Distinguishes instrument cashflows from investment strategy and from the constant-maturity proxy.                                   |
| Market-model comparison                                                                                                                                                             | Predictive evaluation plus policy-selection/evaluation across models on separate draws.                                                                                           | SCORE and ROBUST; fitting/scoring can run without any household simulation.                                                         |

These are evidence and composition tests, not a promise to reproduce every
published number. Paper-specific financial simplifications belong in each
experiment's configuration and documentation, never in a new competing engine.

## Broader experiments, selected by the next missing capability

- **Income floor plus flexible upside:**
  [Finke–Pfau–Williams (2012)](https://www.financialplanningassociation.org/article/journal/MAR12-spending-flexibility-and-safe-withdrawal-rates)
  motivates varying outside income and consumption preferences.
  [Scott–Sharpe–Watson](https://web.stanford.edu/~wfsharpe/retecon/4percent.pdf)
  motivates examining spending shortfalls and unused surpluses, not only success
  rates. Start with scheduled outside income and existing supported par-held
  bond cashflows; trading/off-par holdings require BOND. Review the existing
  indexed-bond slice's TIPS fidelity before expanding it. Annuities additionally
  need longevity and counterparty contracts. A ladder is not an annuity.
- **Nonconstant spending needs:**
  [Blanchett (2014)](https://www.financialplanningassociation.org/article/journal/MAY14-exploring-retirement-consumption-puzzle)
  motivates age/category-specific budgets. Author those functions in a shell;
  a population spending pattern is not automatically this household's preference.
- **Broader evidence and market mechanics:** international records, joint
  resampling, equity-premium uncertainty, valuation-conditioned returns and
  adverse regimes are independently testable candidates under GM. For example,
  [Ang–Piazzesi's term-structure model](https://www.nber.org/papers/w8363) motivates
  comparing curve/macro dynamics; it does not prescribe Augur's implementation.
- **Forecast feedback:** consider only when simple authored rules cannot express
  the desired decision. It additionally requires complete continuation state,
  conditional forecasts from information available at the decision date, and
  separate inner random streams. Do not restart taxes or replay opening trades.

## Household experiment protocol

1. Start from supplied holdings, basis, cash and tax state; do not reset every
   allocation cell to a fictional already-rebalanced tax-free book. Record the
   trades and costs required to establish each target, or explicitly study
   gradual deployment.
2. Vary spending-anchor costs and allowed cuts jointly with allocation. Make
   reversible flexibility, irreversible transitions, cash bands, surplus
   reinvestment and rebalancing cadence explicit. Do not bake a one-way tier
   ladder or automatic backstop into engine semantics.
3. Use common paths within a model; distinguish historical overlapping-window
   frequencies from independent draws. Report uncertainty in differences and
   do not rank statistically unresolved cells. More bootstrap draws are not
   more observed history.
4. Report realized consumption distributions, frequency/depth/duration of cuts,
   time at each anchor, backstop use, shortfall/default, tax cashflows and terminal
   wealth. Mark stopped paths and conditioning denominators. Keep a selected
   trajectory's decisions and settlements inspectable.
5. Preserve a reproduction record: named dataset snapshots/vintages, fit
   window/artifact, path identities, product constructions, calendar/tax
   assumptions, opening book, code revision and policy parameters. Capture
   materialized paths or a reproducible generator; do not rely on browser caches
   or serialization of arbitrary closures.
6. Evaluate candidate policies on fresh draws and across model, instrument,
   tax/future-law, horizon and spending assumptions. Show feasible trade-offs or
   that no candidate meets the selected constraints. Do not invent a scalar
   utility function or treat a model's winner as an unconditional recommendation.

The existing `x/allocation_sensitivity.py` recurrence remains a deliberately
simplified tax-free control. Its old rankings and causal interpretations do not
set the implementation order; comparisons with canonical RUN must hold timing,
instruments, rebalancing and withdrawal conventions constant before attributing
differences to taxes or a refactor.
