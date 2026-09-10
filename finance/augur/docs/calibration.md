# Prediction-market calibration

Calibration compares supplied model paths with a typed prediction-market catalog.
It does not change the model, fit tax rules, or certify forecast accuracy. Matching
current quotes and predicting later resolved outcomes are different evaluations.

## Inputs and ownership

<../calibration/catalog.py> binds each exact market to an issuer or level series;
the catalog determines the union of targets, not a single implicit PE issuer.
Correlated and unmappable markets are surfaced separately from exact resolutions.
Private catalogs, holder evidence, fitted artifacts and deployment choices remain
downstream; generic mappings and public examples live here.

The caller samples once and supplies the PE bundle, anchored level paths, dates,
rollout seeds and platform price clients to `run_calibration` in
<../calibration/calibration.py>. The API also derives its requested fan summaries
from those sampled paths. Financial settlement is not rerun to score a market.

Production clients in <../calibration/default_clients.py> use
`EvidenceMarketReader` over the evidence checkout. Requests read mirrored quotes,
not platform HTTP APIs or a TTL read-through cache. Missing mirrored markets or
uninformative quotes are logged and omitted; malformed snapshots propagate as
errors. An outage can leave old quotes available, but does not guarantee fresh or
complete evidence. Deployment checkout ownership is described in <../README.md>.

## Resolution and reporting

- PE mappings distinguish IPO by a deadline, failure before IPO, and valuation
  crossing a threshold by a deadline. They use the bound issuer's event/valuation
  channels, not a household holding's realizable sale proceeds.
- Level mappings distinguish a threshold **on** a date from a crossing **by** a
  date. Inflation year-over-year questions need the corresponding twelve-month
  denominator, including pre-start observations for near-term questions.
- Macro thresholds require levels anchored to the catalog's observation date.
  The economic series must match the question: a price index is not an equity
  total-return proxy. `build_anchored_level_paths` and
  <../calibration/macro_anchors.py> supply the current anchoring path.
- Results retain unresolved counts and surface unavailable model channels as
  `unmodeled`. A horizon beyond the supplied paths is not a negative outcome.
- Binary resolutions carry model probabilities, Wilson intervals and unresolved
  share. Categorical families compare normalized quoted bucket probabilities with
  model outcomes. Missing bucket quotes prevent that family from being scored;
  ladder fits have their own interpolation/quality-weighting behavior.

These sampling intervals do not measure model uncertainty, establish independent
historical windows, or validate a joint forecast. Per-market divergence is not an
aggregate decision-quality score.

<../calibration/resolvers.py> owns event meaning; its adjacent tests pin deadline,
unresolved-horizon, inflation-history and bucket behavior. Calibration tests cover
missing channels/quotes and family fitting. API endpoint tests exercise the
same library with hermetic price clients.

## Issuer model configuration

<../model/private_equity_risk.py> supports explicit public-market CDF anchors,
converted to monthly hazards, with a configured tail hazard after the last anchor.
<../calibration/ipo_prior.py> derives paste-ready anchors from catalog quotes; its
duplicate/decreasing-point treatment is separate from calibration's weighted
ladder fitting. Producing anchors does not deploy them or validate competing risks.

Company valuation and initial shares must be configured together. The enabled
channel relates the latent per-unit mark to the valuation ratio and dilution;
leaving it disabled selects the independent latent-mark process. Smooth dilution
and the discrete primary-round/employee-mint process are separate supported modes,
with configuration validation preventing their simultaneous use. A sampled latent
mark is not a guarantee of liquidity or proceeds for a particular holder.

Scale-dependent valuation drift is an optional model assumption, not a financial
rule or proof of predictive quality. <../fit/bayes_dilution.py> separates fixed
shape priors from fitted parameters. Single-issuer observations confined to a
narrow size regime do not establish the full maturation shape; any fit or claimed
improvement needs its evidence window, priors and held-out evaluation identified.
