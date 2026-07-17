# E5 — dense coding-agent baseline: Devstral Small 2 24B FP8, TP2

- **Status:** done — clean dense baseline: effective 128K, best tool-calling of
  the set, but ~2–3× slower decode than the A3B MoEs (the dense penalty).
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> § "First five experiments" → E5

## Goal

The dense counterpoint to the MoEs (E1/E2/E4): quantify the MoE-vs-dense latency
gap at similar VRAM and check whether dense quality + clean tool-calling is worth
the slower decode.

## Configuration

- **Model:** `mistralai/Devstral-Small-2-24B-Instruct-2512`, FP8, arch
  `Mistral3ForConditionalGeneration` (multimodal, vision not exercised). Dense
  24B (all params active per token, unlike the ~3B-active A3B MoEs).
- vLLM 0.25.1, TP2, FP8 KV, max-model-len 131072, gpu-mem-util 0.85 (display on
  GPU0), `mistral` tool parser. Manifest: <deployment.yaml>. Raw: <summary.json>.

## Results

### Capacity / resources

| Metric                 | Value                                     |
| ---------------------- | ----------------------------------------- |
| Allocated context      | 131,072 (128,927-token request)           |
| **Effective context**  | **128K** — needle ✓ at depths 0.1/0.5/0.9 |
| GPU KV cache           | 368,256 tokens                            |
| Max concurrency @ 128K | 2.81×                                     |
| Weights                | 12.3 GiB/GPU                              |
| Peak VRAM              | GPU0 30.7 GB / GPU1 28.7 GB (0.85 util)   |

Needle probe passes cleanly at all depths (Devstral is a coding model, not a
verbose reasoner, so the standard small-budget probe works — contrast E4).

### Latency (single request, temp 0, 256-token output)

| Input ctx | TTFT   | Decode tok/s |
| --------- | ------ | ------------ |
| 8K        | 0.21 s | 96           |
| 32K       | 0.24 s | 89           |
| 128K      | —      | —            |

**Decode ~90–96 tok/s is the headline** — the dense penalty. Compare at similar
VRAM: qwen3-coder-30B-A3B ~200, gpt-oss-20B ~1000–1500, Qwen3.5-35B-A3B ~210.
A dense 24B activates all 24B params per token; the A3B MoEs activate ~3B, so
they decode 2–14× faster. (The 128K latency point failed as a harness quirk —
that phase submits a full-131072-token input, which at ctx == max-model-len
leaves no output room and is rejected; the context ladder, which reserves 4K,
works fine at 128K.)

### Tool calls (`mistral` parser) — best of the set

| Case       | Result       |
| ---------- | ------------ |
| single     | ✅           |
| parallel   | ✅ (2 calls) |
| multi-turn | ✅           |

Devstral is the **only model tested that does parallel tool calls** (gpt-oss
emitted one; Qwen3.5's hermes parser emitted none). Clean single + multi-turn
too. This matters for agentic coding.

### Quality

External Devstral SWE-bench / agentic numbers (`ext`) — Devstral is purpose-built
for coding agents and scores strongly on SWE-bench; not re-run here.

## Verdict

Devstral is the clean **dense coding baseline**: effective 128K, the best
tool-calling of everything tested (parallel works), and strong published agentic
quality. The cost is decode throughput — **~90 tok/s vs the MoEs' 200–1500** at
comparable VRAM. So the choice is explicit: dense Devstral for tool-calling
reliability and coding quality when ~90 tok/s is acceptable; an A3B MoE
(qwen3-coder) when decode speed dominates. For an agent doing many short turns,
the MoE's speed usually wins; for fewer, higher-stakes tool-heavy turns,
Devstral's cleaner tool-calling may be worth the slower decode.

## Notes / anomalies

- **Harness quirk:** the latency phase uses the full `target_ctx` as input, so a
  128K latency point on a 128K-max model fails on the output reserve. Fix: cap
  latency input at `max_model_len − output`. The context ladder already reserves
  correctly.
- 0.85 util (display on GPU0, see E4).
