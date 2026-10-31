# AI usage and quota analysis

[The unexecuted notebook](analysis.ipynb) analyzes weekly model usage, token
categories, quota burn, conditional model capacity and statistical sensitivity.
It reads local aggregates and makes no provider requests. Evidence, generated
results, plots and executed notebook copies stay in ignored local paths.

Quota figures and tables focus on weekly budgets following Claude's Max 20× →
Ultra tier change: [verified Ultra accounts have no five-hour limit](https://support.claude.com/en/articles/17728952-what-is-the-ultra-plan).
Historical five-hour polls can remain in calibration evidence.

## Run locally

Supply these files in `evidence/` before running the notebook:

- `quota-polls.jsonl.gz`, `claude-scoped-quota.jsonl.gz` and
  `raw-accounting.jsonl.gz`: quota and accounting observations from the adjacent
  SQL queries.
- `native-usage-5m.json.gz`, `cloud-usage-5m.json.gz` and
  `langfuse-usage-five-minute.json.gz`: normalized five-minute token aggregates.
- `weekly-model-usage.csv`: provider/model/source/payment-lane weekly totals
  emitted by `report_data.py`.
- `usage-accounting-manifest.json`: an object with `source_watermarks`, mapping
  each source name to its capture cutoff. These appear at the top of the notebook.

Native transcript scanning, Claude Code Web extraction and Langfuse extraction
are separate collection steps. `native_accounting.py`, `cloud_usage_bundle.py`
and `langfuse_usage_extract.py` provide their corresponding command-line tools;
the SQL files supply source and coverage queries. Source access and database
credentials belong to the local collection environment.

From the repository root:

```sh
nix develop .#ai-usage
notebook_runtime="${TMPDIR:-/tmp}/ai-usage-notebook"
mkdir -p "$notebook_runtime"
export JUPYTER_PATH="$notebook_runtime/share/jupyter"
export JUPYTER_RUNTIME_DIR="$notebook_runtime/runtime"
export IPYTHONDIR="$notebook_runtime/ipython"
python -m ipykernel install --prefix "$notebook_runtime" --name ai-usage \
  --display-name 'AI usage (Nix)'
cd x/ai_sub_value
jupyter lab --no-browser --ip=127.0.0.1
```

Open `analysis.ipynb`, edit its parameters and run all cells. It writes fitted
results to `evidence/monotonic-capacity-estimates.json`. The Nix shell supplies
JupyterLab and the scientific Python environment. Parsing and estimation live
in `analyze.py`, `estimate_capacity.py` and `bootstrap.py`.

## Interpretation

Recorded weekly throughput, quota-percent burn and conditional token capacity
are different quantities. Resets and awards can increase observed throughput
without increasing the regular subscription allowance. Source panels share a
Y scale within each provider; sources stay separate because cross-system request
overlap may be unresolved. OpenAI has no Claude Code Web path.

Calibration retains monotonic poll spans below saturation, with consistent
reset metadata, gaps of at most 15 minutes, companion-gauge coverage and stable
paid counters. Only a crossing span is discarded at a detected break. Undetected
between-poll resets remain possible.

The primary regression uses poll-aligned pieces at a 60-minute elapsed-time
target, retaining short tails. Whole-run, 30-, 60- and 120-minute diagnostics
conserve accepted time, quota deltas and allocated tokens. Whole-run fits have
no bootstrap or independent-day claims based on a run's start date.

Bootstrap sensitivity compares independent start-hour groups, six-hour blocks,
one-/three-/seven-day blocks and circular three-/seven-day variants. Intervals
span the 5th–95th percentiles. Circular methods balance calendar-day inclusion
but create artificial adjacency at the record's endpoints. Block lengths are
heuristic; they do not establish nominal coverage. Missing records, billing
attribution, changing policy and the observed cache/output/effort mix remain
separate sources of uncertainty.

Tests use synthetic inputs and run through
`bbr test //x/ai_sub_value:all`.
