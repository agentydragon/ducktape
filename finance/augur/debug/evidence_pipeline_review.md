# Augur evidence pipeline review

Reviewed against `devel` at `98602a49b7`. This is a source and wiring review, not a live
cluster inspection or a fresh fit. The checked-in code and deployment notes do not prove that
a particular deployment is running or that its evidence is current.

## Data flow

```text
FRED / Yahoo / Zillow / Ken French / prediction markets
    -> scraper or one-shot study fetch
    -> raw evidence files
    -> parsed tables and consumer-selected observations
       -> offline fitting -> fitted YAML or model artifact -> runtime sampler
       -> historical studies -> replay or bootstrap
       -> calibration -> market snapshots and macro anchors
    -> sampled paths -> Augur simulation or calibration results
```

[`finance/scraper/fetch.py`](../../scraper/fetch.py) fetches raw bytes, maintains a
per-source last-successful-fetch manifest and commits the evidence repository. Studies can
instead use [`snapshot_evidence`](../study/trinity/evidence_snapshot.py) to fetch the same
source specifications directly into a temporary directory. The study fetch has no repository
history or scraper freshness policy.

[`finance/evidence/loading.py`](../../evidence/loading.py) parses sources into Polars frames.
[`load_exogenous_evidence`](../fit/evidence_data.py) builds aligned level and return matrices
and latest observations. [`train.py`](../x/models/train.py) fits selected models and writes
their artifacts. At runtime, providers instantiate configured samplers; loading new evidence
does not by itself refit them. Historical replay loads and materializes dated windows
separately. Calibration reads market snapshots and derives macro anchors through separate paths.

The checked-in [cluster decision](../../../cluster/docs/decisions.md) marks the
`augur-evidence` Flux package as parked. Comments describing a git-sync sidecar explain the
intended integration, not evidence that it is currently deployed. This review found no
checked-in Agentplane code directly loading these finance datasets.

## Findings

### A. Calendar gaps in fitting and scoring: addressed in code

[`load_evidence_segments`](../fit/data.py) retains aligned observed monthly levels and their
calendar labels in `DatedHistoricalSeries`. The full and FRED-only paths derive returns only
for adjacent calendar months; `load_evidence` rejects a gapped history when a caller requires
`HistoricalSeries`.

The VECM keeps all observed levels in one per-factor normalization and uses only within-segment
one-month transitions. It records segments and gaps in its fitted state; the terminal latent
state and absolute factor anchors use the same final common as-of month. The main SP500 model
factor is Yahoo SPY adjusted USD. State-space fitting uses dated one-month returns, and its
private-equity/SP500 coupling excludes intervals missing any calendar month. Scoring reports
coverage and excludes cross-gap transitions and multi-step origins.

The focused regressions cover gapped loader-to-fit/score paths and continuous input. The
October 2025 CPI gap observed in the earlier evidence review motivated this work; this refresh
did not recheck the current evidence or run a full real-evidence fit. A fresh fit and coverage
review remain useful before relying on a new artifact.

### B. CPI history and publication lag: partially addressed

[`resolve_anchors`](../calibration/macro_anchors.py) now retains CPI history by its actual
calendar month, and the [inflation resolver](../calibration/resolvers.py) looks up an exact
pre-anchor month. A missing denominator makes the relevant outcome unresolved rather than
shifting adjacent observations. Tests cover an internal gap, a publication lag in the loaded
history, and the missing-month API path.

The starting anchor is a separate approximation: `resolve_anchors` selects the latest CPI
observation on or before the model anchor month, and calibration uses that value at month zero
even if it was observed earlier. Decide whether to start the CPI path at the observed month,
model the intervening months, or make affected outcomes unavailable; test the chosen policy
through calibration. Dated lookbacks alone do not resolve this starting-point question.

### C. Nonpositive FRED observations: addressed in code

[`fred_series_frame` and `monthly_last`](../../evidence/loading.py) retain finite zero and
negative values, which can be valid rates. Operations requiring a positive level, such as
logarithms or price/index anchors, validate positivity at their own boundaries. Tests cover
nonpositive rates and invalid index inputs.

### D. Source tables versus selected views: partially addressed

[`EvidenceFrameCache`](../../evidence/loading.py) caches parsed frames by `EvidenceSource`.
The French factors parser retains its four factor columns, and the Zillow parser retains the
city table before selecting a region; both can be loaded through the cache. Other sources are
still parsed directly into consumer-selected views: Yahoo selects adjusted close, and the
international French archive parser selects one section and market column. A source can
naturally contain multiple tables, columns, locations, currencies or frequencies.

Separate source parsing from named derived views where the source warrants it. Preserve the
relevant upstream table schema and semantics at the parse boundary, then select columns,
cities, frequencies and units for each consumer. A source with multiple sections can expose
named tables. Prediction-market snapshot objects need not become dataframes merely for a
common interface.

### E. Cache ownership remains fragmented

Historical replay and structural-macro fitting create operation-scoped `EvidenceFrameCache`
instances. The main [`load_exogenous_evidence`](../fit/evidence_data.py) still uses separate
byte and parser helpers. It now parses each Zillow source table once per load and selects both
cities from that table; it no longer reparses the source for each city. Calibration's
absolute-level loader reads through `read_monthly_levels` separately.

Migrate one complete fit or calibration operation to a shared operation-scoped source cache,
keeping consumer-specific validation and selection explicit. Measure before adding persistent
caching; a persistent parsed cache would need to account for source contents, parser version
and selection parameters.

### F. Fit inputs remain with the operator

Fitted artifacts retain numerical state and runtime anchors needed to sample, not
training-source manifests or source hashes. The caller chooses the input to a fit; an operator
who needs an exact-input record can save notes alongside the run. The scraper's
[`EvidenceManifest`](../../scraper/fetch.py) records fetch times for the evidence repository.
The operation-scoped cache does not pin Git or promise an atomic multi-file read; a caller
needing a fixed checkout must arrange it.

### G. Intermediate shapes still change between layers

The pipeline uses Polars frames, `list[MonthlyLevel]`, NumPy matrices with separate month
strings, and model-specific state. For example, the
[structural macro fit](../x/models/structural_macro_fit.py) selects and validates dated frames
and converts columns to lists for the macro fitter. The main exogenous loader converts its
aligned frame to arrays and separate month labels. These boundaries require care to preserve
calendar identity and make units clear.

Keep dated tables through source parsing, selection and alignment where practical. Convert
at the numerical boundary to the arrays and typed inputs a model needs, and validate its
calendar and unit assumptions there. NumPy and JAX remain appropriate for numerical work;
this does not require a new provenance framework.

### H. Read and freshness behavior varies by entry point

`AUGUR_EVIDENCE_DIR` is read directly, while
[`ensure_checkout`](../../evidence/checkout.py) clones a workstation fallback once and then
reuses it without refreshing. [Prediction-market readers](../calibration/evidence_clients.py)
read snapshot files individually. The scraper maintains fetch timestamps, but the calibration
read path on this reviewed commit does not use them to enforce freshness. A successful read
can therefore be old without a consistent indication to its consumer.

Let each operation choose its freshness requirements: a historical fit and a current market
calibration have different needs. Avoid silently refreshing a caller's input or treating a stale
market snapshot as current. Fetch time is not necessarily observation time and does not by
itself prove which snapshot bytes were read.

## Proposed composition

A source is approximately a Polars table, or a small set of named tables when an upstream
archive naturally contains several. A consumer selects the data it needs and decides when to
align it. The following illustrates existing loading functions and a hypothetical fit boundary:

```python
cache = EvidenceFrameCache(evidence_dir)
factors = cache.frame(FRENCH_FACTORS)
zillow = cache.frame(ZILLOW_ZHVI)

us_equity = compound_monthly_returns(french_market_returns(factors), return_column="market_total_return")
sf_home_values = zillow_city_series_frame(zillow, ZILLOW_ZHVI, region_name="San Francisco", state="CA")

# The consumer chooses the series and alignment; the model owns its numerical requirements.
training = align_monthly_frames({"equity": us_equity, "home_value": sf_home_values}, value_column="value")
artifact = fit_model(training)  # illustrative, not an existing API
```

Keep source tables, consumer-selected observations and sampled runtime paths distinct. The
existing [`LevelFrames`](../model/exogenous.py) are long-form sampled output keyed by rollout
and month; they need not have the same schema as historical evidence. Preserve series identity,
units and observation semantics without turning every dataset into one universal table or
embedding training-source notes in every fitted artifact.

Next, settle the CPI starting-anchor policy, then migrate one complete fitting path through the
source-table and operation-scoped cache boundary. Compare the output with the existing path,
including gaps and nonpositive rates, before migrating the remaining consumers. A real-evidence
fit, additional source coverage and multi-equity modeling are separate follow-ups.
