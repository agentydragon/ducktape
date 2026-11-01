# Study map: capabilities, not a closed list of FIRE rules

The code sketches are a first varied review set, not a boundary around what Augur
should support. Select further examples for the new behavior they demand. Links
below are research leads, not endorsements of their numerical recommendations.

## Spending, allocation, and household outcomes

| Study                                                                                                                                                                                                                                                                                                         | Why it matters here                                                                                 | Example status                                                                                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Cooley–Hubbard–Walz, Trinity (1998)                                                                                                                                                                                                                                                                           | Historical withdrawal/allocation grid; an external reference for engine conventions                 | [Historical shell](trinity.md)                                                                                   |
| Guyton–Klinger (2006)                                                                                                                                                                                                                                                                                         | Coordinated path-dependent spending and portfolio rules                                             | [Rule-combination shell](guyton_klinger.md)                                                                      |
| Vanguard dynamic spending (2021)                                                                                                                                                                                                                                                                              | Budget flexibility bounded relative to last year's spending                                         | [Shell using our forecast](vanguard.md)                                                                          |
| Pfau–Kitces (2014), Blanchett (2015)                                                                                                                                                                                                                                                                          | Allocation trajectories and sensitivity to starting market conditions                               | [Glide-path shell](glide_paths.md)                                                                               |
| Frank–Mitchell–Blanchett (2011)                                                                                                                                                                                                                                                                               | Reassessing failure risk as time and financial state change                                         | [Our forecast-feedback variant](forecast_feedback.md)                                                            |
| [Finke–Pfau–Williams (2012), Spending Flexibility and Safe Withdrawal Rates](https://www.financialplanningassociation.org/article/journal/MAR12-spending-flexibility-and-safe-withdrawal-rates)                                                                                                               | Trade-offs between consumption, shortfall, outside income and risk preferences                      | Next distinct shell: income-floor and flexible-upside portfolios, with a supplied preference criterion           |
| [Scott–Sharpe–Watson, The 4% Rule—At What Price?](https://web.stanford.edu/~wfsharpe/retecon/4percent.pdf)                                                                                                                                                                                                    | Costs of financing fixed spending with risky assets, including unused surpluses                     | Research lead for liability matching and pricing, not another success-rate grid                                  |
| [Blanchett (2014), Exploring the Retirement Consumption Puzzle](https://www.financialplanningassociation.org/article/journal/MAY14-exploring-retirement-consumption-puzzle)                                                                                                                                   | Spending need not track one CPI-adjusted constant                                                   | Next variant: age/category-specific spending paths; a population average need not describe this household        |
| [Anarkulova et al. (2025), The safe withdrawal rate: evidence from a broad sample of developed markets](https://www.cambridge.org/core/journals/journal-of-pension-economics-and-finance/article/abs/safe-withdrawal-rate-evidence-from-a-broad-sample-of-developed-markets/5D6C1EBBAFE135FC27D236C9F46E677F) | Challenges dependence on a favorable U.S. historical record using broader developed-market evidence | Next evidence variant: international panel and sampling assumptions; no claim that its dataset is available here |

The [personal](spending_allocation.md) and [housing](housing.md) shells add actual
tax lots, contracts, spending anchors, and backstop transitions. Future floor-income
experiments would distinguish bond ladders from annuities: finite dated cashflows,
longevity-contingent payouts, counterparty assumptions, and residual wealth are not
interchangeable. A long fixed horizon can precede stochastic longevity without
pretending it models lifetime risk exactly.

## Market and macroeconomic models deserve their own study lane

The [model-comparison shell](model_comparison.md) separates held-out forecasting
performance from policy consequences. These research directions exercise distinct
mechanics; they are candidates to compare, not modules all required in one model.

| Research direction                              | Concrete source                                                                                                                                                                            | What an Augur experiment would compare                                                                                                         |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Dependence changes in adverse regimes           | [Ang–Bekaert, How do Regimes Affect Asset Allocation?](https://www.nber.org/papers/w10080)                                                                                                 | Fixed joint distribution versus regime-dependent returns/correlations; held-out forecasts and allocation choices                               |
| Macro variables and yield-curve dynamics        | [Ang–Piazzesi, A No-Arbitrage Vector Autoregression of Term Structure Dynamics with Macroeconomic and Latent Variables](https://www.nber.org/papers/w8363)                                 | A macro VAR/VECM baseline versus a term-structure-consistent model; yield and bond-return forecasts at relevant durations                      |
| Nominal/real yields and inflation risk premiums | [Christensen–Lopez–Rudebusch, Inflation Expectations and Risk Premiums in an Arbitrage-Free Model of Nominal and Real Bond Yields](https://www.frbsf.org/wp-content/uploads/wp08-34bk.pdf) | Joint inflation/nominal/real-rate models and implications for Treasuries versus inflation-linked bonds; separate expectations from priced risk |
| Probabilistic forecast evaluation               | [Gneiting–Raftery](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf), [Scheuerer–Hamill](https://repository.library.noaa.gov/view/noaa/22327)            | Calibration and proper scoring of marginal behavior and dependence on shared held-out observations                                             |

An IID baseline and joint block bootstrap are useful controls even if neither is
ultimately adequate. Fit-window, data-vintage, measurement/splice, and model-family
changes should be isolated when possible; changing them all at once cannot identify
which change explains a result. Long-horizon planning also needs multi-year tests:
one-step forecasts can hide important disagreement about persistence.

This selection makes four separate questions visible: financial mechanics
correctness; evidence/model quality; consequences of policy choices under each
model; and what an individual considers acceptable. No paper, language rewrite,
or single "best fit" score answers all four.
