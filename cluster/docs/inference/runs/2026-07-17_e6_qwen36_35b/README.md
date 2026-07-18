# E6 — Qwen3.6-35B-A3B FP8, TP2 (resident upgrade of E4)

- **Status:** done — serves at 262K; same GDN linear-attention story as E4
  (1.4M-token KV cache), same verbose-reasoner cost. Newer weights (73.4 vs 69.2
  SWE-bench) but **not** a latency or tool-calling upgrade over E4.
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> § "First five experiments" → follow-up to E4

## Goal

Current-gen refresh of E4's Qwen3.5-35B-A3B: same family, same arch, one minor
version newer. Does the newer checkpoint move any of the `{context, intelligence,
latency}` axes, or is it E4 with a better leaderboard number?

## Configuration

- **Model:** `Qwen/Qwen3.6-35B-A3B-FP8` — same `Qwen3_5Moe` arch as E4: **GDN
  (Gated Delta Net) linear attention** in most layers, FP8 weights, FP8 KV.
- vLLM 0.25.1, TP2, max-model-len 262144, **gpu-mem-util 0.85** (display shares
  GPU0, see E4). Manifest: <deployment.yaml>.
- **Deviation from E4:** added `--reasoning-parser=qwen3` (E4 ran only `hermes`),
  to have vLLM split the verbose chain-of-thought into `reasoning` deltas so the
  bench can (a) measure decode including reasoning tokens and (b) have a chance at
  parsing tool calls out of the non-reasoning content.

## Results

### Capacity / resources — identical to E4 (same GDN arch)

| Metric                 | Value                                   |
| ---------------------- | --------------------------------------- |
| Allocated context      | 262,144                                 |
| **GPU KV cache**       | **1,416,390 tokens** (E4: 1,404,197)    |
| Max concurrency @ 262K | **5.40×** (E4: 5.36×)                   |
| Peak VRAM              | GPU0 29.5 GB / GPU1 27.2 GB (0.85 util) |
| Available KV memory    | 6.99 GiB/GPU (linear-attn = tiny KV)    |

GDN linear attention again holds a **1.4M-token** KV cache at 0.85 util — the
architecture to watch for cheap long context (cf. E3, E4).

### Latency (cold prefill; thinking **on** — decode counts reasoning tokens)

| Input ctx | TTFT (cold) | Decode tok/s (incl. reasoning) |
| --------- | ----------- | ------------------------------ |
| 8K        | 0.68 s      | 220                            |
| 32K       | 2.4 s       | 228                            |
| 128K      | 14.4 s      | 209                            |

Within noise of E4 (231/226/211). `ttfc` (time-to-first-**content**) was `None`
at every rung: with a 256-token latency budget the model is still emitting
`reasoning` deltas and never reaches non-reasoning content — a direct measurement
of the reasoner's verbosity.

### Needle / effective context — **262K confirmed, content-only**

Unlike E4 (where the small probe budget couldn't tell retrieval from reasoning),
this run used a **principled content-only needle**: the code counts as retrieved
only if it appears in the model's **final answer** (`content`), not its reasoning,
within a **4096-token** budget (thinking on). Result — every depth passes at both
rungs:

| Rung | Allocated       | Needle depths passed | Failed |
| ---- | --------------- | -------------------- | ------ |
| 131K | ✓ (124,821 tok) | 0.1 / 0.5 / 0.9      | —      |
| 262K | ✓ (255,671 tok) | 0.1 / 0.5 / 0.9      | —      |

So at full 262K, the model **finishes reasoning and emits the retrieved code in
its answer within 4096 tokens** — effective long-context retrieval is confirmed
here (the caveat E4 couldn't clear). Marked `local~` in <../../results.md>
(needle check, not a full long-context eval) but now a definite pass. See
<summary.json>.

### Tool calls — still fail, even with the reasoning parser

`n_calls=0` on single / parallel / multi-turn, same as E4. Adding
`--reasoning-parser=qwen3` did **not** fix it: with thinking on, the model spends
its budget reasoning in the `reasoning` channel and emits no `hermes`-formatted
tool call in `content`. This is the same verbose-reasoner failure mode as E4, and
it is the honest agent-relevant limitation of this model on this stack: no
reliable tool calling out of the box.

### Verdict

Qwen3.6-35B is E4 with a newer checkpoint (higher SWE-bench on paper) and **the
same practical profile on wyrm2**: excellent KV/long-context efficiency (1.4M-token
cache), ~210–228 tok/s decode dominated by reasoning verbosity, and no working
tool calls. Not a free coding-agent upgrade over E1; a long-context/generalist
candidate that still needs harness/parser work before it's agent-usable.

## Notes

- Display shares GPU0 (~2.7–3 GB); 0.85 util mandatory on wyrm2 (see E4).
- E1–E5 model weights were deleted from the `hf-cache` PVC to make room for this
  download (PVC expanded to 250Gi); re-pull from HF if those runs need re-serving.
