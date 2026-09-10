# Private-equity valuation and issuance models

This describes existing conventions in <../model/private_equity_risk.py>, not
validated cap-table economics or a recommendation of a deployment preset.
The [calibration contract](calibration.md) owns market resolution and reporting;
[future work](../plans/future_work.md) owns deferred model changes and retirement.

## Model modes

Without company valuation and initial shares, the provider samples its independent
latent-mark process. Configuring both enables a valuation/issuance channel whose
mark is anchored by `current_mark * (V / V0) / (shares / shares0)`; the opening
holder mark need not equal headline valuation divided by shares.

- **Smooth dilution:** each rollout samples an annual dilution rate and applies
  `(1 + rate) ** (month / 12)`. Its configured rate is median-anchored, not the
  arithmetic mean of the dispersed rates.
- **Mint streams:** `PrimaryRoundConfig` and `EmployeeMintConfig` are required
  together, along with the valuation/share anchors; nonzero smooth-dilution
  parameters are rejected in this mode. Valuation, rounds and mint innovations
  use separate derived random streams. Their realized paths can still depend on
  each other through the model's state-dependent rates.

The mint sampler advances valuation drift/shocks, then effective annual employee
mint via `(1 + mint_rate) ** (1/12)`, then primary rounds. Round occurrence is a
monthly Bernoulli draw against the clipped configured/state-dependent probability:
at most one round per month, not a continuous-time Poisson event simulation.
Optional IPO anticipation uses the marginal public-opening CDF to reduce that
probability, not each rollout's realized IPO date.

At a round, the current sampler multiplies valuation by `(1 + cash_ratio) * step_up`
and shares by `1 + cash_ratio / step_up`. These are the implemented model rules;
the old plan's claimed derivation of a post-round unit price as simply the
pre-round price times `step_up` does not follow for general non-unit step-up.
Do not treat this description as an endorsement or silently change its arithmetic
in a documentation cleanup. Tender/exit realization is a separate channel; none
of these latent marks establishes a particular holder's liquidity or waterfall.

## Existing fit and controls

<../fit/bayes_mint_streams.py> and <../fit/fit_mint_streams_report.py> already
implement the annotated-primary-round fit/report path. Round dates/cash are
observed inputs. The fit uses fixed unit step-up and fixed drift-shape priors,
NUTS for its remaining latent-path/issuance parameters, and a Gamma-Poisson event
count posterior for the reported monthly hazard. It does not fit every runtime
knob or an arbitrary macro-conditioned company program.

That Poisson-rate fit and the runtime's direct monthly Bernoulli probability are
different conventions. Their reconciliation, non-unit step-up economics,
posterior propagation and predictive adequacy belong to deferred PE research,
not a claim that the old plan has certified a complete model.

Adjacent sampler and fitter tests pin configuration rejection, opening anchors,
round/mint behavior and synthetic fitting controls. The preset-shaped central
trajectory tests describe selected parameters; their broad bands are not held-out
evidence or authority to switch the deployment default. Neither mode is selected
for retirement here; the owner's smooth-dilution fidelity concern remains open.
