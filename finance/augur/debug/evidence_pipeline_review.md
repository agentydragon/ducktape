# Augur evidence pipeline review

Reviewed against `devel` at `5532f74740` after PR #13655. This is a source and wiring
review, not a live cluster inspection or a fresh fit. The source code and deployment notes
show the intended data paths; they do not prove that a particular deployment is running or
that its evidence is current.

## Data flow

```text
FRED / Yahoo / Zillow / Ken French / prediction markets
    -> scraper or one-shot study fetch
    -> raw evidence files
    -> parsers and consumer-selected observations
       -> offline fitting -> fitted YAML or model artifact -> runtime sampler
       -> historical studies -> replay or bootstrap
       -> calibration -> market snapshots and macro anchors
    -> sampled paths -> Augur simulation or calibration results
```

[`finance/scraper/fetch.py`](../../scraper/fetch.py) fetches raw bytes, maintains a
per-source last-successful-fetch manifest and commits the evidence repository. Studies can
instead use [`snapshot_evidence`](../study/trinity/evidence_snapshot.py) to fetch the same
source specifications directly into a temporary directory. These are different acquisition
paths: the study snapshot has no repository history or scraper freshness policy.

[`finance/evidence/loading.py`](../../evidence/loading.py) parses the sources into Polars
frames. [`load_exogenous_evidence`](../fit/evidence_data.py) builds aligned return matrices,
latest observations and evidence metadata. [`train.py`](../x/models/train.py) fits selected
models and writes their artifacts. At runtime, providers instantiate configured samplers;
loading new evidence does not by itself refit their models. Historical replay loads and
materializes dated windows separately. The calibration API reads market snapshots and derives
macro anchors through separate paths.

The checked-in [cluster decision](../../../cluster/docs/decisions.md) marks the
`augur-evidence` Flux package as parked. Comments describing a git-sync sidecar explain the
intended integration, not evidence that it is currently deployed. This review found no
checked-in Agentplane code directly loading these finance datasets.

## Findings

### A. Keep calendar gaps through fitting and scoring

**High: model input correctness.** `load_evidence_segments` retains aligned actual monthly
levels and their `YYYY-MM` labels in `DatedHistoricalSeries`. The full and FRED-only paths
derive returns only for adjacent calendar months; `load_evidence` rejects a gapped history
when a caller requires `HistoricalSeries`.

The VECM now keeps every observed level in one global per-factor normalization, but its
likelihood uses only within-segment one-month transitions. Segment boundaries and omitted
months are recorded with the fitted state. Its terminal latent state and absolute factor
anchors use the same final common as-of month. The SP500 model factor is Yahoo SPY adjusted
USD; the separate FRED SP500 price index remains an auxiliary observation for private-equity
coupling. State-space return fitting consumes the dated one-month returns directly, and its
private-equity/SP500 estimate uses an interval only when every calendar month in that interval
has an observed level. Scoring uses the same eligible dated transitions and reports gaps and
multi-step origins excluded across them.

The October 2025 CPI gap remains in the actual evidence: no level or return is inserted for
that month, and the VECM does not treat the surrounding rows as a monthly pair. Coverage is
reported because fewer transitions and forecast origins contribute to fit and scores. The
focused regressions exercise a gapped loader-to-fit/score path and continuous input, but do not
claim a full real-evidence training run or a fitted production artifact.

### B. CPI calibration history loses calendar identity

**High: calibration correctness.** [`resolve_anchors`](../calibration/macro_anchors.py)
chooses the latest observation on or before the model anchor month; `_trailing_levels` takes
the preceding available rows and returns only their values. The
[inflation resolver](../calibration/resolvers.py) interprets those positions as the calendar
months immediately preceding the model's `as_of` month. A missing month shifts the positions;
a lag between the latest CPI observation and `as_of` can also shift them. A year-over-year
market may therefore use the wrong denominator.

Keep dated observations and resolve each lookback by its exact calendar month. If a needed
month is unavailable, surface the resolution as unavailable or unresolved. Cover both an
internal missing month and a publication lag, including the API calibration path.

### C. Generic FRED loading drops valid nonpositive observations

**High: data fidelity.** [`fred_series_frame` and `monthly_last`](../../evidence/loading.py)
filter out values at or below zero. That is necessary for a positive index before logarithms,
but the same helpers load interest rates, for which zero and negative observations can be
meaningful. Removing them can create gaps and bias a sample. This is a code-level finding;
the number of affected rows in current evidence was not measured.

Parse numeric observations without a universal positivity filter. Validate positivity where
an operation actually requires it, such as a logarithm or a positive price index. Test zero and
negative rates as well as invalid index inputs.

### D. Source frames are already consumer-selected views

**Medium: boundary design.** [`EvidenceFrameCache`](../../evidence/loading.py) caches frames
by `EvidenceSource`, but its dispatch returns different consumer-shaped outputs. The French
factors parser retains market total return and the risk-free rate rather than the full factors
table; the international archive parser selects one market column; Yahoo selects adjusted
close; Zillow requires a city and state and cannot be loaded through this cache. A source can
naturally contain multiple tables, columns, locations, currencies or frequencies.

Separate source parsing from named derived views. Preserve the relevant upstream table schema
and semantics at the parse boundary, then select columns, cities, frequencies and units for
each consumer. A source with multiple sections can expose named tables. Prediction-market
snapshot objects need not become dataframes merely for a common interface.

### E. Cache ownership is fragmented and the main fitting path bypasses it

**Medium: duplication and repeated work.** Historical replay and structural-macro fitting
construct their own `EvidenceFrameCache` instances. The main
[`load_exogenous_evidence`](../fit/evidence_data.py) uses separate byte and parser helpers;
each Zillow source is read and parsed once for each selected city because selection is part
of its parser. Calibration's absolute-level loader reads the sources again through
`read_monthly_levels`.

Give one fit, study or calibration operation an evidence-reading context that owns raw-byte and
parsed-table caches. Select multiple city rows from the same parsed table. Start with an
operation-scoped in-memory cache; measure before adding persistence. A persistent parsed cache
must include the source-content identity and parser/version identity, including any
selection-specific parameters.

### F. Source labels do not identify the evidence used for a fit

**Medium: reproducibility and consistency.** The scraper's
[`EvidenceManifest`](../../scraper/fetch.py) records fetch times, and Augur's
[`EvidenceMetadata`](../model/evidence.py) records useful source labels, dates and coverage.
The paths reviewed do not bind a fit to a particular evidence-repository commit or source-byte
hashes. Two runs naming the same source and date range may use different source revisions.
A cache that reads individual files lazily from a moving checkout could also combine revisions
if the checkout changes during an operation.

Pin one snapshot for an operation and carry its identity, source identities/content hashes,
observation spans and transformations into the fitted artifact or study result. Explicitly
identify one-shot upstream fetches too. A snapshot identity and cache key are related but
separate from a freshness policy.

### G. Intermediate shapes repeatedly discard and rebuild structure

**Medium: maintainability and correctness.** The pipeline uses Polars frames,
`list[MonthlyLevel]`, dictionaries of lists, NumPy matrices with separate month strings, and
reconstructed level matrices. For example, the
[structural macro fit](../x/models/structural_macro_fit.py) aligns frames, converts columns to
lists, and hands them to a fitter that assembles dated states. The conversions make it easy to
lose calendar information and make source-specific units implicit in a generic `value`
column.

Keep dated Polars tables through parsing, selection and alignment. Convert once at a model's
numerical boundary to the arrays and typed inputs that model requires. Carry typed series
identity, units, observation semantics and provenance with the table; validate the model's
calendar assumptions at the boundary. NumPy and JAX remain appropriate for numerical work.

### H. Read and freshness behavior varies by entry point

**Medium: operational semantics.** `AUGUR_EVIDENCE_DIR` is read directly, while
[`ensure_checkout`](../../evidence/checkout.py) clones a workstation fallback once and then
reuses it without refreshing. [Prediction-market readers](../calibration/evidence_clients.py)
read snapshot files individually. The scraper maintains freshness information, but the Augur
read paths reviewed do not use that manifest to establish a freshness policy. A successful
read can therefore be old without a consistent indication to its consumer.

Expose snapshot identity and source observation/fetch times. Let each operation select its
freshness requirements: a reproducible historical fit and a current market calibration need
different policies. Avoid silently refreshing a pinned fit or silently calling a stale market
snapshot current.

## Proposed composition

A source is approximately a Polars table, or a small set of named tables when an upstream
archive naturally contains several. A consumer selects the data it needs and decides when to
align it. The following is illustrative; these APIs do not exist yet:

```python
# One stable snapshot and cache for this operation.
snapshot = EvidenceSnapshot.open(evidence_dir, revision="...")

# Each source/table is parsed once within the snapshot.
factors = snapshot.table(FRENCH_FACTORS, table="monthly")
zillow = snapshot.table(ZILLOW_ZHVI, table="cities")

us_equity = market_total_return(factors)
sf_home_values = city_monthly_values(zillow, city="San Francisco", state="CA")

# The consumer owns its required fields and calendar policy.
training = align_for_fit(
    {"equity": us_equity, "home_value": sf_home_values},
    missing="preserve",
)
artifact = fit_model(training, provenance=snapshot.provenance())
```

Keep source tables, consumer-selected observations and sampled runtime paths distinct. The
existing [`LevelFrames`](../model/exogenous.py) are long-form sampled output keyed by rollout
and month; they need not have the same schema as historical evidence. A typed source identity,
units and observation semantics should accompany frames, without turning every dataset into a
single universal table.

Fix A–C first so a shared interface does not propagate the existing calendar and value
mistakes. Then establish snapshot identity and a source-table boundary, migrate one complete
fitting path, and use that experience to migrate the remaining consumers. A broader cache,
additional source coverage and multi-equity modeling are separate decisions.
