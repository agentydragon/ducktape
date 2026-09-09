# Executable bounded-spending experiment

A first working composition, not a published-study reproduction or financial advice:
Python loads/materializes paths and compiles a portfolio; an ordinary Rust closure
chooses annual spending; the existing engine funds it and records the timeline.

The rule withdraws a percentage of current cash plus public holdings at months
0, 12, …, bounded by cut/raise limits relative to the last withdrawal adjusted
for CPI. A zero-cut, zero-raise control holds real spending constant. Money is
rounded at each annual CPI reset and limit calculation. Rules live in `policy.rs`,
not an engine enum. Each rollout gets fresh closure state.

The shell reuses the Trinity experiment's historical windows and instruments:
Ken French equity; a synthetic 20-year Moody's Aaa constant-maturity bond fund;
1926–1995 record intent with actual coverage retained in `paths.json`.
See <../../study/trinity/README.md> and its `replay.py` for source/construction gaps.
It also inherits **no taxes**, no fees beyond those in the input series,
cashflow-only rebalancing, a zero cash band, and **purchases disabled** (surplus
distributions remain cash). This is not the user's taxable portfolio model.

With an evidence checkout containing the Trinity source files, run from the repo root:

```bash
bb run //finance/augur/x/bounded_spending:compare_bin -- \
  --evidence-dir /path/to/evidence --output-dir /tmp/spending-comparison \
  --equity-share 0.60 --rate-bps 400 --max-cut-bps 1000 --max-raise-bps 500
```

Use a new output directory. It retains the prepared `execution-input.json`, experiment
parameters in `policies.json`, and full `fixed_real.json` / `bounded.json` timelines, including
requested versus paid spending, sales, and failure month. Forensic capture is
deliberately small-scale: these files can be large. This does not benchmark or
solve batched execution. Overlapping historical windows are not independent
Monte Carlo draws; the shell does not label their fractions as probabilities.

`compare_test` uses three stipulated price/CPI paths, without network access, to
check cut/raise limits, an interior target, subsequent resets and the fixed-real control
through the full compiler/function/funding composition.
The engine's spending tests separately exercise sales and tax settlement.
