# Inference TODO — prioritized by information gain

Each item is phrased as "what we'd learn." Higher items deliver more
signal toward the goal in <README.md#goal> per unit of effort. Re-rank
freely as we run things and learn what surprises us.

> **Note:** this is the older Ollama-era signal-ranked backlog. The active
> sequence is now E1–E5 in <PLAN.md> § "First five experiments", which makes
> k8s vLLM the starting point (folding in P2 #5 below). Items here that a PLAN
> experiment subsumes get checked off against the corresponding run.

## P0 — high signal, low cost, hits the main use case

### 1. Standard coding benchmark on `gpt-oss:20b` — _HumanEval done; saturated_

**Done at HumanEval:** see <runs/2026-04-29_humaneval_gpt20/README.md>.
pass@1 = 0.957 / 0.970 / 0.970 at low / medium / high. Saturation; no
discrimination across efforts.

**SWE-bench Verified N=100 (shuffled): paused, no headline yet** — see
<runs/2026-04-29_swebench_n100_shuffled_gpt20/README.md>. Two aborted
attempts; the `swe_bench_react` scaffold is the right shape, but Ollama
context-sizing (`num_ctx=262144` vs the model's 131072) still needs
fixing before a usable run. BigCodeBench / LiveCodeBench still untouched.

Pick one off-the-shelf coding eval that's already in Inspect AI; mirror
the AIME structure (sweep `reasoning_effort` ∈ {low, medium, high}).

Candidates:

- **HumanEval+** — 164 problems, ~30 min, pass@1.
- **MBPP+** — 378 problems, ~1.5 h.
- **LiveCodeBench** — ~400 problems, contamination-resistant, ~3 h.
- **BigCodeBench** — 1 140 problems, broader stdlib coverage, ~6+ h.

Recommend HumanEval+ first (smallest, fastest, most directly comparable
to public leaderboards).

What we'd learn:

- Is the model we're already running actually good at the main use case
  (AI-powered coding) — i.e. should we keep it as the default?
- Does the AIME inverted-U on `reasoning_effort` hold for coding too,
  or does coding prefer a different effort level?

This is the single biggest gap in our current evidence base. We do a
lot of AI-powered coding through this deployment but have no quality
number on it.

### 2. AIME-2025 contamination test on `gpt-oss:20b`

Same script as <runs/2026-04-29_aime_gpt20_n30/>; swap the dataset arg.
~30 min if Inspect ships `aime2025`; otherwise small port.

What we'd learn:

- Is the inverted-U real, or partly memorization of 2024 problems? If
  2025 looks similar, the headline is robust. If 2025 drops sharply,
  the claim becomes "medium dominates _on memorized problems_."
- Calibration on how much we trust the N=30 finding when generalizing.

## P1 — cheapest follow-ons once P0 lands

### 3. Same coding eval on 1–2 alternative models

Once the coding eval from P0#1 is wired, the marginal cost of another
model is just `--model openai/<name>` and a wall-time wait.

Candidates:

- **Qwen3-Coder-30B-A3B** in some quant — needs loading; mind the VRAM
  math from <runs/2026-04-28_initial/README.md#followup-cpu-offload-confirmed-for-gpt-oss120b>.
- **DeepSeek-R1-Distill-Qwen-32B** — reasoning + coding mix.
- **Llama 3.3 70B Instruct** — if VRAM headroom allows.

What we'd learn: is `gpt-oss:20b` good enough, or is there a free
upgrade in the model zoo? Model-choice decision made with data instead
of vibes.

### 4. Format-compliance probe

Custom Inspect task: 50 trivial arithmetic problems with `ANSWER: N`
instruction, swept across efforts and 1–2 models. ~1 h to build, ~30
min to run.

What we'd learn:

- Is the format-violation pattern from the N=30 run (`\boxed{}` even
  when forbidden) gpt-oss-20b-specific or a class behavior?
- Does it correlate with effort, prompt template, or neither?

Mostly diagnostic — sharpens our trust in strict scores on other evals.

## P2 — structural / longer horizon

### 5. vLLM in-cluster PoC

Bigger lift (image, k8s manifest, model PVC, auth, …). Doesn't produce
a finding directly but unlocks:

- Tensor parallel for `gpt-oss:120b` and 70B-class models — currently
  CPU-offload-bound on Ollama (see
  <runs/2026-04-28_initial/README.md#followup-cpu-offload-confirmed-for-gpt-oss120b>).
- Native Blackwell FP4/MXFP4 kernels — Ollama dequantizes to bf16/fp16.
- Real `reasoning_effort` semantics for models that respect it.
- Real concurrent decode (Ollama defaults to `NUM_PARALLEL=1`).

Worth doing once P0–P1 evidence either confirms `gpt-oss:20b on Ollama`
is enough (low priority for vLLM) or shows a bigger model would be
materially better at our jobs (high priority).

### 6. Quant variants

Only matters if a specific bottleneck points at quant. Examples:

- Q4 vs Q8 KV cache on `gpt-oss:20b` — effect on AIME quality at fixed
  decode rate.
- 70B model at AWQ vs FP8 (post-vLLM).

Lowest priority unless P0–P1 surface a quant-shaped question.

### 7. Live eval reporting / dashboard

When watching a long Inspect run today we relied on `--display plain`
(now wired into the run scripts) plus poking `docker ps` and Ollama
logs. That works but is rudimentary. Inspect AI ships some better
options worth evaluating when we want them:

- `inspect view start --log-dir <dir>` — local web UI, ships with
  the `inspect_ai` Python package. Reads the same `.eval` zip logs,
  auto-refreshes as new evals run (per upstream docs). Best for
  browsing across multiple completed runs (HumanEval / AIME /
  SWE-bench at once). Could be a one-liner `nix run` / shell helper
  in the inference docs hub.
- `log_realtime: true` (already on by default) writes per-sample
  data to the `.eval` zip incrementally; in our N=100 run the
  central directory wasn't finalized mid-run, so the viewer would
  only see headers — but per upstream docs the viewer auto-refreshes
  as evals run, so that may have been a false alarm. Worth re-verifying
  against a finished run by pointing the viewer at `runs/*/eval_logs/`.
- `inspect_ai.hooks` event API — programmatic subscriber to Score /
  Sample events. Could push to Prometheus, JSONL sidecar, log
  aggregator. Custom code, but cheap.

Everything is open-source and self-hosted; there's no separate AISI
cloud service to evaluate.

Triggers for picking this up: (a) we run more multi-hour evals where
mid-run visibility matters; (b) we want a single dashboard across all
runs in `runs/*/eval_logs/`; (c) live-reporting becomes a blocker for
some operational workflow.

## Out of scope here

- **AIME on `gpt-oss:120b` via Ollama** — at 1.5 tok/s it would take
  ~30 h. Wait for vLLM.
- **Re-run gemma4 on AIME** — Ollama's gemma streaming is finicky (see
  <runs/2026-04-28_gemma_followup/>); not worth the yak-shave.
- **Custom RE / FreeCAD evals as a primary path** — those are a
  separate workstream tracked under
  <../../../skills/reverse_engineer/evals/> and the freecad skill
  eval dir. Off-the-shelf proxies first; custom tasks are an orthogonal
  scaffolding investment.

## How to claim an item

Follow <PLAN.md>. A new run gets a `runs/<run-id>/` directory holding the
manifests/scripts that ran and a `README.md` with the numbers; add a row to
<results.md> (with a source/trust mark), not to <benchmarks.md>. Link the run
here when the item is complete.
