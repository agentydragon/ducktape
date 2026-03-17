# Ollama Cluster Benchmarks

Results from benchmarking `gpt-oss` models on the k8s Ollama cluster.

Hardware: 2× NVIDIA RTX 5090 (64 GB VRAM total).
Cluster config: `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`.
Endpoint: `https://litellm.allegedly.works/v1` (LiteLLM proxy).

Scripts:

- Throughput: `benchmark_ollama.py` → `bazel run //hack/benchmark_ollama:benchmark_ollama`
- NIAH recall: `niah.py` → `bazel run //hack/benchmark_ollama:niah`

Raw JSONL logs are next to this file: `niah_results_*.jsonl`.

---

## Throughput — gpt-oss:20b (2026-02-22)

**Methodology**: streaming completions for tg (rate = tokens between first/last
chunk); non-streaming with `max_tokens=1` for pp (rate = prompt_tokens /
wall_clock). Wall-clock includes ~10–50 ms network RTT. Each config ran for up
to 60 s or 10 samples, whichever came first.

| Metric | Mean (t/s) |   Stdev | n   |
| ------ | ---------: | ------: | --- |
| tg128  |      249.1 |    48.2 | 3   |
| pp1k   |     3561.8 |   713.2 | 10  |
| pp4k   |    11173.8 |  2086.9 | 10  |
| pp16k  |    36489.9 | 10030.9 | 10  |
| pp32k  |    59229.8 | 17154.2 | 10  |
| pp64k  |    92310.2 | 30682.4 | 10  |
| pp128k |   133050.7 | 47100.8 | 10  |

Prompt processing scales strongly with context length as the GPU amortises
attention: ~3.5k t/s at 1k tokens → ~133k t/s at 128k tokens.

Contexts beyond 128k were tested but at benchmark time `OLLAMA_NUM_CTX=131072`
was still active, causing per-request KV cache reloads (~28 s each). Those
numbers are not valid steady-state throughput. Re-run after the `OLLAMA_NUM_CTX`
update to `1048576` is applied by Flux.

### GPU vs CPU

| Metric | CPU gVisor | GPU 2×5090 | Speedup |
| ------ | ---------: | ---------: | ------: |
| tg128  |    ~12 t/s |   ~249 t/s |    ~21× |
| pp512  |    ~64 t/s |  ~95k t/s† |  ~1500× |

† Interpolated from pp64k/pp128k.

## Throughput — gpt-oss:120b

Pending. Model pull job is in progress. Re-run:

```
bazel run //hack/benchmark_ollama:benchmark_ollama -- --models gpt-oss-120b-128k
```

## NIAH (Needle-in-Haystack) — gpt-oss:20b

See `niah_results_*.jsonl` for raw data and per-sample outputs.

### NIAH Recall — gpt-oss-20b-128k (2026-02-22)

**Methodology**: random 8-char hex needle (`IMPORTANT: The secret passcode hidden
in this document is [<code>].`) embedded at a random depth within a haystack of
repetitive prose. Model prompted to return only the code. Streaming used to
capture reasoning tokens (non-streaming strips them for this model family).
5 samples per (context_size × depth_bucket) cell; seed=42.

|  ctx | p10 (0–20%) | p30 (20–40%) | p50 (40–60%) | p70 (60–80%) | p90 (80–100%) | mean |
| ---: | ----------: | -----------: | -----------: | -----------: | ------------: | ---: |
|   4k |  100% (5/5) |   100% (5/5) |   100% (5/5) |   100% (5/5) |    100% (5/5) | 100% |
|  16k |  100% (5/5) |   100% (5/5) |   100% (5/5) |   100% (5/5) |    100% (5/5) | 100% |
|  32k |  100% (5/5) |   100% (5/5) |   100% (5/5) |   100% (5/5) |    100% (5/5) | 100% |
|  64k |  100% (5/5) |   100% (5/5) |   100% (5/5) |   100% (5/5) |    100% (5/5) | 100% |
| 128k |  100% (5/5) |   100% (5/5) |   100% (5/5) |   100% (5/5) |    100% (5/5) | 100% |

**Result: perfect recall (125/125) across all context sizes and positions.**
The model retrieves the needle reliably at all tested depths.

Note: the model is a reasoning model — it processes the full context in its
thinking stream. The score counts any occurrence of the code in the streamed
output (reasoning + final answer), so this measures whether the model _sees_
the needle, not just whether it echoes it cleanly at the end.

Raw data: `niah_results_gpt-oss-20b-128k_20260222_011419.jsonl` (125 samples).
(`_005853.jsonl` is a discarded pilot run that used non-streaming and got all zeros
due to LiteLLM stripping reasoning tokens from non-streaming responses.)

### NIAH Extended Context — gpt-oss-20b-128k at 256k tokens (2026-02-22)

**Context**: `OLLAMA_NUM_CTX=131072` (128k) was active at benchmark time. Per-request
`num_ctx=295040` override was sent, but the server default caps effective KV cache size.
3 samples per cell; seed=100.

|  ctx | p10 (0–20%) | p30 (20–40%) | p50 (40–60%) | p70 (60–80%) | p90 (80–100%) | mean |
| ---: | ----------: | -----------: | -----------: | -----------: | ------------: | ---: |
| 256k |    0% (0/3) |     0% (0/3) |     0% (0/3) |    67% (2/3) |    100% (3/3) |  33% |

**Finding: severe recall degradation — only the last ~35% of the 256k context is accessible.**
Needles at depths 0–60% are invisible to the model; only needles near the end are found.

This confirms the user's hypothesis: Ollama caps effective context at `OLLAMA_NUM_CTX`
even when a larger `num_ctx` is requested per-query. The model sees at most the last 128k
tokens of a 256k prompt. After the `OLLAMA_NUM_CTX=1048576` deployment (pending Flux merge),
this should recover to 100% recall up to 1M tokens.

Raw data: `niah_results_gpt-oss-20b-128k_20260222_020916.jsonl` (15 samples).

### NIAH Extended Context — gpt-oss-20b-128k at 512k tokens (2026-02-22)

**Context**: `OLLAMA_NUM_CTX=131072` (128k) active; effective KV cache truncates input.
3 samples per cell; seed=101.

|  ctx | p10 (0–20%) | p30 (20–40%) | p50 (40–60%) | p70 (60–80%) | p90 (80–100%) | mean |
| ---: | ----------: | -----------: | -----------: | -----------: | ------------: | ---: |
| 512k |    0% (0/3) |     0% (0/3) |     0% (0/3) |     0% (0/3) |     33% (1/3) |   7% |

**Finding: at 512k context, only the deepest p90 needles are accessible (~7% recall).**
The kept region is the last 128k/512k = 25% of the document.
Needles at depth <75% are invisible; only the very end of the document is processed.

Raw data: `niah_results_gpt-oss-20b-128k_20260222_022504.jsonl` (15 samples).

### Summary: OLLAMA_NUM_CTX truncation effect

With `OLLAMA_NUM_CTX=131072` (128k), Ollama hard-truncates long prompts to the last 128k tokens
regardless of the per-request `num_ctx` override:

| Context size | Accessible fraction | Mean NIAH recall |
| -----------: | ------------------: | ---------------: |
|         128k |                100% |             100% |
|         256k |                ~50% |              33% |
|         512k |                ~25% |               7% |

`OLLAMA_NUM_CTX=1048576` has been deployed. The default context sizes now include 256k
and 512k. Re-run to validate recall at extended context:

```
bazel run //hack/benchmark_ollama:niah -- \
  --log-dir /path/to/hack/benchmark_ollama \
  --benchmarks-md /path/to/hack/benchmark_ollama/benchmarks.md \
  --seed 200
```

<!-- Results appended by niah.py -->
