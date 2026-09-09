# Executable bounded-spending experiment

A first working composition, not a published-study reproduction or financial advice:
Python loads/materializes paths and compiles a portfolio; an ordinary Rust closure
chooses annual spending; the existing engine funds it and records compact outcomes.

The rule withdraws a percentage of current cash plus public holdings at months
0, 12, …, bounded by cut/raise limits relative to the last withdrawal adjusted
for CPI. A zero-cut, zero-raise control holds real spending constant. Money is
rounded at each annual CPI reset and limit calculation. Rules live in `policy.rs`,
not an engine enum. Each rollout gets fresh closure state.

The shell reuses the Trinity experiment's historical windows and instruments:
Ken French equity; a synthetic 20-year Moody's Aaa constant-maturity bond fund;
1926–1995 record intent with actual coverage and the ordered historical start
date for every rollout retained in `paths.json`.
See <../../study/trinity/README.md> and its `replay.py` for source/construction gaps.
It also inherits **no taxes**, no fees beyond those in the input series,
cashflow-only rebalancing, a zero cash band, and **purchases disabled** (surplus
distributions remain cash). This is not the user's taxable portfolio model.

With an evidence checkout containing the Trinity source files, run from the repo root:

```bash
bb run //finance/augur/x/bounded_spending:compare_bin -- \
  --evidence-dir /path/to/evidence --output-dir /tmp/spending-comparison \
  --equity-share 0.60 --rate-bps 400 --max-cut-bps 1000 --max-raise-bps 500 \
  --trace-rollout 0
```

Use a new output directory. It retains the prepared `execution-input.json`, experiment
parameters in `policies.json`, and compact `fixed_real.json` / `bounded.json` summaries.
Each contains per-path requested/paid `annual_consumption`, plus existing wealth,
shortfall and failure metrics. The component is not total household consumption.
Consumption arrays contain event months (no opening snapshot): live zero requests
are explicit, the failure month is included, and post-stop months are absent.
Product wealth metrics retain their separate snapshot layout and zeroed-failure convention;
their `failed_month` is `-1` for a path that completes the horizon (null in a forensic trace).

`fixed_real.consumption.json` / `bounded.consumption.json` report monthly 5th/50th/95th
percentiles in nominal currency quanta, with currency/quantum, observed path count
and failure months. These distributions condition on paths still observed in that
month, including paths that fail then; no observations produces null percentiles,
not zero consumption. They do not describe all original paths' future lifestyles.
Overlapping historical windows are not independent Monte Carlo draws; no independent-
sampling error bars or probability claims are made.

Omit `--trace-rollout` for compact output only; repeat it to retain selected full
`fixed_real.trace-N.json` / `bounded.trace-N.json` timelines. Replay reconstructs
fresh policy state using the original path identity, without capturing the rest
of the population. This does not benchmark or introduce batched policy callbacks.

`compare_test` uses three stipulated price/CPI paths, without network access, to
check cut/raise limits, an interior target, subsequent resets and the fixed-real control
through the full compiler/function/funding composition and reconcile compact
requests/payments to selected forensic receipts. A depletion control distinguishes
live zeros from unmet consumption and post-stop absence. Engine tests separately
exercise sales, tax settlement, component identity and other-account failure.
