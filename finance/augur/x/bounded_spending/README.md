# Executable bounded-spending experiment

A first working composition, not a published-study reproduction or financial advice:
Python loads/materializes paths and compiles a portfolio; an ordinary Rust closure
chooses annual spending; the existing engine funds it and records compact outcomes.

The rule withdraws a percentage of current cash plus public holdings at months
0, 12, …, bounded by cut/raise limits relative to the last withdrawal adjusted
for CPI. A zero-cut, zero-raise control holds real spending constant. Money is
rounded at each annual CPI reset and limit calculation. Rules live in `policy.rs`,
not an engine enum. Each rollout gets fresh closure state.

Prepared-input and output I/O use the shared
[native invocation helpers](../../rust/docs/execution_boundary.md#native-experiment-invocation).
The example owns policy parameters, account bindings, selected traces and analysis.

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
Product metrics use a separate snapshot layout: failure in event month `f` ends at
snapshot `f+1`, valued at the observed month `f` marks. Later compact slots are unobserved
padding, not zero wealth. `failed_month` is `-1` for a completed path (null in a forensic
trace); see <../../rust/docs/product_metrics.md> for observation masks and outcome bases.

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
of the population. This native shell does not benchmark or introduce batched policy callbacks.

## Python-owned monthly loop prototype

`python_policy.py` runs the same financial control through the existing extension,
with two editable policy forms: `ScalarAdapter` creates an ordinary stateful
`ScalarPolicy` per original path; `BatchPolicy` authors the annual rule over a batch
and gathers/scatters independent memory by original path ID. Neither calculates
settlement or taxes. `run` accepts any callable of the experiment's `Observations`,
so the rule can be replaced in a notebook without rebuilding Rust:

```python
import json
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, run
from finance.augur.x.bounded_spending.stress_paths import prepare

prepared = prepare(rollout_count=3, horizon_months=36)
ids = list(range(prepared.execution_input["rollout_count"]))
policy = BatchPolicy(Parameters(400, 1000, 500), len(ids))
summary = run(json.dumps(prepared.execution_input), policy, ids)
```

`prepared` is a compiled run with experiment-owned exogenous paths and no scheduled
consumption component; for a runnable stipulated control, use
`stress_paths.prepare(rollout_count=3, horizon_months=36)`. The Python shell supplies
one opening-month decision per live path, then advances that path exactly once.
`chunk_size` and `reverse` exercise routing independently of policy semantics.
Use a fresh policy instance for each run or selected `forensic=True` replay.
Policies must not use neighboring batch rows as economic information: arbitrary
Python code is not sandboxed into path independence. Native observed holdings
exclude future paths; the experiment owns the sampled paths outside that view.

The batch implementation uses NumPy **object arrays** to preserve exact Python
integer intermediates and native half-away-from-zero rounding. It is batch-authored,
not a compiled NumPy integer kernel. Silent int64 overflow is not an optimization.
The extension copies integer columns and uses JSON for final output; these are
prototype costs to measure, not conclusions about Python or the final interface.
Control errors abort the prototype session without resubmission; insufficient
funding stops only its path. This does not implement general actor actions.

`python_policy_test` compares complete compact outputs and selected forensic traces
against the native rule on identical up/down/interior equity paths, including
fixed-real spending, reordered/chunked decisions, depletion and live zeros.

## Profiling the concrete prototype

Run one workload per fresh process, with an unused output directory:

```bash
bbr run -c opt //finance/augur/x/bounded_spending:profile_bin -- \
  --rollouts 1000 --horizon-months 60 --native-threads 4 --authoring batch --capture summary \
  --output-dir /home/buildbuddy/workspace/artifacts/command-0/batch-1000x60
```

Compare `--authoring scalar`, `batch`, and `native-cli` with equal dimensions and
`summary` capture; matching input/output hashes check that the compared trajectories
and metrics agree. Use smaller `forensic` Python runs separately to expose capture
cost; the native CLI compact control is not comparable to those trace runs.
The harness writes the compiled input, `execution.prof` and `report.json`, and
prints cumulative cProfile rows. Reports include profiled path-month throughput,
separate Python/native-child RSS high-water marks, CPU/thread configuration and
the stipulated path/funding assumptions. Compilation is outside cProfile; memory
includes preparation. Native CLI timing includes process startup/file I/O and is
not isolated native compute. cProfile overhead is part of these measurements;
callback tail latency, hardware-normalized speedups and acceptable budgets are not
inferred. No market probability estimates come from these repeated stress paths.

The [matched-workload investigation](../../../../debug/augur_python_policy_batches_20260909.md)
records 1,000/100,000-path compact profiles and a separate forensic capture run,
including exact input/output agreement and remaining measurement limits.

`compare_test` uses three stipulated price/CPI paths, without network access, to
check cut/raise limits, an interior target, subsequent resets and the fixed-real control
through the full compiler/function/funding composition and reconcile compact
requests/payments to selected forensic receipts. A depletion control distinguishes
live zeros from unmet consumption and post-stop absence. Engine tests separately
exercise sales, tax settlement, component identity and other-account failure.
