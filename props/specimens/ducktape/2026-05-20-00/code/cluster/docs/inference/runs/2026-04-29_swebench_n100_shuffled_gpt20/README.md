# 2026-04-29 SWE-bench Verified N=100 (shuffled) on gpt-oss:20b

> **Status:** paused — two aborted attempts, no headline yet. See
> [What happened](#what-happened) and [Next steps](#next-steps).

Replacement for the earlier N=100 attempt at
<../2026-04-29_swebench_n100_gpt20/> which was started before we
noticed the `--sample-shuffle` flag was missing. The earlier run
loaded samples in alpha-by-repo order, so the first 90 (it stopped
at 90/100) were 22× `astropy` + 68× `django` — not a representative
cross-section of Verified's 12+ repos.

This run uses `--sample-shuffle 42` so the 100 sampled problems are
drawn at random from the 500 in Verified.

## What happened

Two attempts so far, both aborted before producing a usable headline.
Each is preserved under `attempts/` with its own README.

| #   | Dir                                 | Outcome              | Reason                                                                                                                                                       |
| --- | ----------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | `attempts/bash_session_partial/`    | 4/30 = 0.133 partial | Default solver's `bash_session` (TTY-typing) was confusing `gpt-oss:20b`; model issued `type` with no `type_submit`.                                         |
| 2   | `attempts/run2_react_num_ctx_500s/` | 0/100, every req 500 | Switched to `swe_bench_react_agent` (stateless `bash`); Ollama then rejected requests because Inspect AI sized `num_ctx=262144` (model trained for 131 072). |

The current scaffold (`swebench_react_task.py@swe_bench_react`)
remains the right shape — the bash-vs-bash_session change is sound
and we want to keep it. Only the Ollama context-sizing problem from
attempt #2 is still open.

## Next steps

1. Pin a small `max_tokens` on the `inspect eval` invocation so
   Ollama's KV-cache rounding stays inside `n_ctx_train`. Add
   `--max-tokens 8192` (gpt-oss:20b generations on SWE-bench react
   are ~1–3 K tokens; 8 K is plenty of headroom). With that capped,
   `prompt_tokens + max_tokens` will round up to ≤131 072 except
   for genuinely large sample inputs (a few `sympy` / `django`
   issues with very long traces — flag those if they recur).
2. Re-launch via `./run_swebench.py`. Don't bother with another N=1
   sanity run — the wrapper task itself is verified (`inspect list
tasks` discovers it, instantiates 500 samples). The unknown is
   the Ollama 500-rate after the cap, which we'll see in the first
   30 min of the real run.
3. If 500s persist, check `kubectl -n ollama logs … | grep num_ctx`
   for the actual requested size — the round-up rule may need a
   smaller `max_tokens` or a tightened SWE-bench input prompt.

Optional: file an upstream issue against `inspect_ai` asking for an
explicit `max_tokens` default for openai-compat / Ollama backends.
Without it, every long-prompt eval on Ollama hits the same wall.

## What runs

- `run_swebench.py` points at the local `swebench_react_task.py@swe_bench_react`
  wrapper (not the canonical `inspect_evals/swe_bench`), which swaps the
  default `bash_session` solver for `swe_bench_react_agent` (stateless
  `bash` + `python` + `think`). `gpt-oss:20b` was getting confused by
  `bash_session`'s `type` / `type_submit` semantics in the previous
  attempt — issuing `action: "type"` without a follow-up submit, leaving
  the shell waiting on input.
- Flags: `--sample-shuffle 42`, `--display plain`,
  `INSPECT_SANDBOX_MAX_EXEC_OUTPUT_SIZE=1 GiB`, `--limit 100`,
  `--message-limit 1000`, `--max-connections 2`.

## Why we expect a different headline

The partial unshuffled run gave **0.122** (11/90), heavily
django-weighted (django 15%, astropy 5%). At N=100 spread across all
~12 Verified repos, the aggregate could land lower (if django was an
outlier for this model) or higher (unlikely; published 20B-class
baselines are 5–15%). Stderr at N=100 is ~0.03.

## Estimated cost

- **Wall:** ~3.5 h, based on the partial run's ~28 samples/h throughput.
- **Disk:** the shuffled set will pull ~30+ new images we haven't seen
  before (sympy, scikit-learn, sphinx, matplotlib, pylint, requests,
  pytest, …); start with ~120 GB free, expect ~30–50 GB of net pulls.
- **Tokens:** ~50 M input + ~400 K completion (per pilot extrapolation).

## Caveats / risks

- **Same Inspect bug exposure** as the pilot (CircularByteBuffer in
  `bash_session`'s docker-exec wire — see <upstream_issue.md>). The
  react agent uses `bash` (one-shot exec, no persistent TTY), so the
  1 GiB cap is mostly insurance now; the long-running wire reads that
  triggered the bug aren't on this code path.
- **Disk pressure.** New repos = new image base layers. Watch `df` and
  prune if needed; the hourly monitor will alert.
- **No effort sweep.** SWE-bench's solver doesn't propagate
  `--reasoning-effort` to the underlying generate calls; model uses
  its default.

## Reproducing

```bash
cd cluster/docs/inference/runs/2026-04-29_swebench_n100_shuffled_gpt20
./run_swebench.py
```

## Results

TBD.
