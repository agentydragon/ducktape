# Optional market-model research

These questions survive the June 2026 prediction-market interpolation and sparse
company-model sketches. They are candidates, not Augur's architecture or a
second implementation queue. Historical replay, structural statistical models,
and other explicit providers remain legitimate experiment inputs.

The [roadmap](roadmap.md) owns sequencing: **SCORE** compares common observables
and held-out windows; **GM** decides which changes earn adoption; **ROBUST** tests
decision consequences; **READY** assesses joint forecast fidelity for the stated
scope. None requires prediction-market fitting, an LLM, or one model family.
Deferred operations and PE questions live in [future work](future_work.md).

## Prediction-market comparison and conditioning

Current [calibration](../docs/calibration.md) compares supplied paths against
typed market questions; it does not force all providers to reproduce those
quotes. A future experiment could compare unconditioned forecasts, market
beliefs and a market-conditioned forecast on the same dated evidence.

- Quotes about thresholds, dates or events do not identify a joint trajectory
  distribution. Any coupling and interpolation assumptions remain attributable
  to the model, including after a fit matches the supplied marginals.
- Reweighting existing samples and fitting generative parameters are different
  candidates. Reweighting cannot create missing sample support; parameter fitting
  does not guarantee support or a useful joint forecast either. Report residuals
  and weight concentration separately from predictive evidence.
- Inconsistent threshold/time ladders, correlated or near-duplicate questions,
  market selection and quality weights need explicit treatment. A soft objective
  must retain the penalty keeping the fitted distribution near its base if it
  claims to implement a minimum-divergence projection; a marginal-error sum
  alone does not specify that projection.
- Matching current quotes is reproduction, not forecast skill. Comparisons need
  frozen information dates, resolved outcomes or held-out series, compatible
  question meanings and dependence-aware uncertainty. Do not interpret an LLM's
  asserted knowledge cutoff alone as proof of leakage-free evaluation.

The [PM-reifier spike](../x/pm_reifier/README.md) records exploratory June 2026
runs, not current adoption evidence. [Loom](../../../loom/PLAN.md) has a separate
forecast-evaluation substrate and proposed WorldSet pipeline; no WorldSet bridge
is required for Augur's present experiments. Reifier retirement is deferred;
keeping it executable is not required. If calibration/reification is later
deleted, preserve the revival task in [future work](future_work.md).

## Sparse company models

Classical, hand-authored, LLM-authored or hybrid event/valuation models are
possible research choices. No authoring method is uniquely justified by sparse
evidence. The implemented [PE model](../docs/private_equity_model.md) is not the
full macro-conditioned program or preferred-holder waterfall the old sketches
proposed.

- Separate company valuation, share issuance, liquidity events and the holder's
  actual sale rights/payoffs. Headline value is not a realizable holder price;
  contract terms, discounts and eligibility need stated ownership and provenance.
- Conditioning company events on a macro path specifies a joint model; it does
  not validate the assumed dependence. Compare plausible coupling assumptions
  when the available company history cannot identify them.
- A reference-class fit is a possible source of evidence, not a dataset presumed
  available. Account for failed and unresolved companies, censoring, selection
  and observation dates before claiming a transferable result for one issuer.
- Compare event timing, valuation marks and no-liquidity tails against simple
  baselines. Keep unresolved outcomes explicit; fitting a handful of current
  market quotes does not establish those dynamics.

Private holdings, grant terms and downstream fitted artifacts remain private.
These questions do not authorize new data-fetch infrastructure, a model default
switch, or retirement of either currently supported PE mode.
