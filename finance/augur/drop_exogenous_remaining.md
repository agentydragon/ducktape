# Remaining "exogenous" references to clean up

All models in augur are exogenous by invariant, so the adjective adds no information.

## Done (this PR)

- [x] Model labels: `composite_exogenous_model` → `composite`, `independent_exogenous_model` → `independent`
- [x] Test name: `test_product_fails_when_sample_is_missing_required_exogenous_series` → drop "exogenous"
- [x] Log message in server.py: `"exogenous presets"` → `"models"`
- [x] Config field descriptions: "exogenous-only", "exogenous preset" → "models", "model presets"
- [x] calibration_wire.py docstring
- [x] test_calibration_endpoint.py docstring
- [x] export_schema.py docstrings
- [x] product/service.py docstring
- [x] config.py field descriptions (CalibrationCatalogConfig, Config fields)
- [x] gaffer-private: config_test.py, config.yaml, README.md references

## Remaining (future PRs)

### Deep type renames (SampledExogenousBundle etc.)

Touches many files across model/, sim/, product/, calibration/. High-value but large blast radius:

- `SampledExogenousBundle` → `SampledBundle` in `augur/model/exogenous.py`
- `materialize_sampled_exogenous` → `materialize_sampled_bundle` in `augur/sim/external_series.py`
- `ExogenousPathId`, `ExogenousPathSet` in prior_art_audit.md design docs
- All imports and usages across `model/`, `sim/`, `product/`, `calibration/`

### Module docstrings / file-level descriptions

- `augur/model/deterministic.py` — "Deterministic scalar exogenous models."
- `augur/model/gbm.py` — "Geometric Brownian scalar exogenous models."
- `augur/model/composite.py` — "Composite exogenous provider..."
- `augur/model/independent.py` — "Independent-per-series exogenous provider..."
- `augur/model/state_space.py` — "Trained block-shrunk state-space exogenous provider."
- `augur/model/provider_config.py` — "Deployment's choice of exogenous model..."
- `augur/model/testing.py` — "Test-only exogenous path model fixtures."
- `augur/model/vecm.py` — "VECM joint exogenous model..."
- `augur/model/series_model.py` — "A sim-facing bundle of exogenous series trajectories."
- `augur/model/conditioning.py` — "Runtime conditioning observations for trained exogenous providers."
- `augur/model/series.py` — "Typed identifiers for exogenous level series..."
- `augur/model/private_equity_trajectories.py` — "...onto an underlying exogenous bundle..."
- `augur/model/level_series_groups.py`
- `augur/fit/metrics_report.py` — "Score the active exogenous models..."
- `augur/fit/model.py` — "...augur exogenous models."
- `augur/fit/main.py` — "Offline exogenous-model training entry point."
- `augur/fit/data.py` — "Load aligned monthly log-returns for the exogenous factors."
- `augur/fit/private_equity.py` — "...compact private-equity exogenous models..."
- `augur/fit/state_space.py` — "Fit inputs for the block-shrunk state-space exogenous provider."
- `augur/calibration/calibration.py` — "Compare any augur exogenous model's rollouts..."
- `augur/calibration/resolvers.py` — "Resolve prediction markets against...exogenous output."

### Internal code comments

- `augur/model/exogenous.py` — various comments, error messages
- `augur/model/composite.py`, `independent.py` — `label` fields (done above)
- `augur/sim/external_series.py` — "exogenous-event frame", function name
- `augur/product/service.py` — "owns...the exogenous model"
- `augur/product/scenarios.py:865` — "exogenous forced-sale"
- `augur/api/accounting.py:50` — `exogenous_path_id` field name

### Documentation files (SPEC.md, README.md, MODEL_CARD.md, DESIGN.md, etc.)

- `augur/SPEC.md` — multiple references
- `augur/README.md` — multiple references
- `augur/model/MODEL_CARD.md` — many references
- `augur/sim/DESIGN.md` — many references
- `augur/sim/README.md`, `REQUIREMENTS.md`, `TODO.md` — many references
- `augur/fit/evidence_data.py` comments
- `augur/data/SOURCES.md`
- `augur/docs/prior_art_audit.md` — many design-level references
- `augur/plans/roadmap.md`, `typed_series_config.md`
- `augur/calibration/example_openai_catalog.yaml`
