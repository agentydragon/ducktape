# Results

Hand-maintained comparison of `wyrm2` inference configurations. This is the
current-numbers table the program in <PLAN.md> produces; it is not generated.

Each row cites where its number comes from and carries a **trust mark**:

- `ext` — external number (leaderboard / model card) at similar quant/config;
  no reason to doubt.
- `ext?` — external number, but our quant/runtime differs enough that it may
  not transfer; candidate for local deepening.
- `local` — measured here; the row links its `runs/<run-id>/` record.
- `local~` — quick local probe (e.g. needle checks standing in for a full
  long-context eval); indicative, not definitive.

Rules: don't edit an accepted run's numbers in place — add a new run directory
and repoint the row. Keep configurations that failed or underperformed in the
table; a known dead end is a result.

## September agentic task completion

| Model and configuration                                          | Task / agent                                           | Outcome                              | Agent time | Compactions | Trust |
| ---------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------ | ---------: | ----------: | ----- |
| Qwen3.8 Flash Next UD-Q4_K_XL, SSD, two GPUs, 128K, Q8 KV, xhigh | Terminal-Bench 4.0 `interleaved-vigenere` / Terminus-2 | Reward 1.0; 6/6 verifier tests       | 7h 39m 40s |           2 | local |
| Qwen3.8 Flash Next UD-IQ4_XS, SSD, two GPUs, 128K, Q8 KV, xhigh  | Terminal-Bench 4.0 `interleaved-vigenere` / Terminus-2 | Reward 1.0; 6/6 tests; agent timeout |         8h |           3 | local |

One predetermined task, one attempt; not a suite score. The
[result record](runs/2026-09-26_qwen38_q4_terminus_result/README.md) includes committed
verifier evidence, full-run token/timing totals, two compaction boundaries, download
overlap and a wrapper guard failure after the passing result was already saved.
The [IQ4 result](runs/2026-09-26_qwen38_iq4_terminus_result/README.md) passed the same
verifier after three compactions, but reached the eight-hour agent deadline while
still testing. Its wrapper exited cleanly. Decode averaged 40.79 versus Q4's 24.74
tokens/s; different memory caps, trajectories and download overlap confound causal
attribution. These are two attempts on one shared task, not two distinct tasks.

## September resident control (initial screen)

Qwen3.8-27B Q8, CUDA llama.cpp build 11151, Q8 KV, one slot. These are one-sample
server token timings; coding quality is not scored. Filled context contains unrelated
source text, not a retrieval test. Full inputs, caveats and artifacts are in the
[SSD run](runs/2026-09-24_qwen38_ssd/README.md).

| Placement             | Configured context | Actual input tokens | Prefill tokens/s | Decode tokens/s | Trust  |
| --------------------- | -----------------: | ------------------: | ---------------: | --------------: | ------ |
| GPU1                  |               8192 |                 102 |           826.21 |           51.09 | local~ |
| Two GPUs, layer split |               8192 |                 102 |           733.12 |           50.00 | local~ |
| GPU1                  |              32768 |               24132 |          3363.97 |           47.74 | local~ |
| Two GPUs, layer split |              32768 |               24132 |          5013.31 |           46.18 | local~ |

Tensor splitting initially failed with the base image's NCCL 2.25.1. Replacing only
that container library with pinned NCCL 2.27.7 yielded 71.85 tokens/s on the short
input and 68.23 at 24,132 input tokens, with 32K configured context (`local~`).
Output lengths changed, so this is not a matched-output task-speed comparison.
See the same run for the build recipe, failure logs and measurements.

The GPU1 reasoning-enabled synthetic tool roundtrip passed. Real coding-task
screening remains pending. See PLAN for limitations of historical measurements below;
their numbers are not directly comparable with this screen.

## September larger-model feasibility

Flash-Next Q4 runs on both GPUs with CPU offload and lazy mmap embeddings under a
38 GiB container cap. At 8K configured context and 102 input tokens, a 1,776-token
coding generation decoded at 32.73 tokens/s (`local~`). Coding correctness is unscored.
At 32K configured context with 24,132 actual input tokens, prefill took 126.703 s
and decode was 30.72 tokens/s (1,697 output tokens, normal stop, zero cached input).
This is not a 128K measurement. Its first synthetic tool-result answer invented file content; two seeded repeats
returned grounded answers. This is a retained failure, not a passed agent-quality
gate. [Exact inputs, responses, launch and limits](runs/2026-09-24_qwen38_ssd/README.md).

## September cluster Ollama serving screen

Qwen3.8-Flash-Next UD-IQ4_XS, Ollama 0.34.4 / llama-server 0.4.1-dev, Q8 K/V,
one slot, two RTX 5090s. Short throughput requests used 51 prompt tokens and generated
1,024 tokens; values are one cold and one warm sample, not a repeated benchmark suite.
The measured configuration was served from the SSD-backed PV with fit targets shown
below.

| Fit targets | Configured context | Actual input/history  |  Decode tok/s | Result                                                | Trust  |
| ----------- | -----------------: | --------------------- | ------------: | ----------------------------------------------------- | ------ |
| 2/0 GiB     |               128K | 51 tokens             | 55.74 / 71.89 | Short requests pass                                   | local~ |
| 2/0 GiB     |               256K | 51 tokens             | 49.66 / 60.82 | Short requests pass; 145K prefill later OOMed         | local~ |
| 4/2 GiB     |               128K | 51 tokens             | 50.21 / 60.01 | Short requests pass                                   | local~ |
| 4/2 GiB     |               256K | 51 tokens             | 44.39 / 49.48 | Short requests pass                                   | local~ |
| 4/2 GiB     |               256K | 145,110-token history |         29.07 | 1,024-token continuation passes; 145K marker recalled | local~ |

Paired rates are the first / warm-repeat samples; 29.07 is one continuation sample.

The 145,048-token marker-recall request took 190.765 seconds of prefill (760.35
tokens/s) and returned the exact marker. Its ten output tokens are too few for a stable
decode estimate. The 1,024-token continuation reused that history, processed 53 fresh
prompt tokens, and decoded in 35.22 seconds. At the 2/0 GiB target, the same long
request instead failed during prefill with a CUDA allocation error; the 4/2 GiB target
passed it.

Across the 128K and 256K LiteLLM routes, the matrix passed 24/24 text and structured
tool-call shape checks across Chat Completions, streamed Responses, and Anthropic
Messages. These are adapter checks, not an agent-quality score. The run proves recall
at 145K under a 256K setting; a full 256K input, parallel slots, and coding quality
remain untested. The rates cannot establish a speedup over the September 24 host
llama.cpp run because quant, runtime, context, cache state, prompt, and placement differ.
[Full run record and raw counters](runs/2026-09-27_ollama_ssd/README.md).

## Historical coding-agent configurations

| Config                       | Runtime          | Quant                        | Allocated ctx | Effective ctx       | Decode tok/s @128K           | Peak VRAM              | Coding quality          | Tool calls                         | Run                                                           |
| ---------------------------- | ---------------- | ---------------------------- | ------------- | ------------------- | ---------------------------- | ---------------------- | ----------------------- | ---------------------------------- | ------------------------------------------------------------- |
| Qwen3-Coder-30B-A3B          | vLLM 0.25.1 TP2  | AWQ 4-bit + FP8 KV           | 262K `local`  | 262K `local~`       | 199 `local`                  | 30.7/29.9 GB `local`   | leaderboard `ext`       | pass `local`                       | [E1](runs/2026-07-17_e1_qwen3coder_awq/README.md)             |
| gpt-oss-20b                  | vLLM 0.25.1 TP1  | native MXFP4                 | 128K `local`  | 128K `ext?`         | ~1000–1500 `local`           | 15 GB `local`          | HumanEval sat. `local`  | single/multi ✓, parallel ✗ `local` | [E2](runs/2026-07-17_e2_gptoss_vllm_vs_ollama/README.md)      |
| gpt-oss-20b                  | Ollama (GGUF)    | MXFP4→bf16 compute           | 128K `local`  | 128K `ext?`         | ~600–1150 `local`            | 15 GB `local`          | HumanEval sat. `local`  | single/multi ✓, parallel ✗ `local` | [E2](runs/2026-07-17_e2_gptoss_vllm_vs_ollama/README.md)      |
| Qwen3.5-35B-A3B (VL, GDN)    | vLLM 0.25.1 TP2  | FP8 + FP8 KV                 | 262K `local`  | unverified `local~` | ~210 `local`                 | 29.0/27.0 GB `local`   | verbose reasoner `ext?` | ✗ hermes parser `local`            | [E4](runs/2026-07-17_e4_qwen35_35b/README.md)                 |
| Qwen3.6-35B-A3B (GDN)        | vLLM 0.25.1 TP2  | FP8 + FP8 KV                 | 262K `local`  | 262K `local~`       | ~209 `local`                 | 29.5/27.2 GB `local`   | SWE 73.4 `ext?`         | ✗ reasoning+hermes `local`         | [E6](runs/2026-07-17_e6_qwen36_35b/README.md)                 |
| Devstral-Small-2-24B (dense) | vLLM 0.25.1 TP2  | FP8 + FP8 KV                 | 128K `local`  | 128K `local~`       | ~90 `local`                  | 30.7/28.7 GB `local`   | SWE-bench strong `ext`  | single/parallel/multi ✓ `local`    | [E5](runs/2026-07-17_e5_devstral_24b/README.md)               |
| gpt-oss-120b (offload)       | vLLM 0.25.1 TP2  | MXFP4 + 12GB/GPU CPU offload | 16K `local`   | 16K `local~`        | ~12 (@8K) `local`            | 29.9/27.9 GB `local`   | SWE 62.4 `ext`          | single/multi ✓, parallel ✗ `local` | [E7](runs/2026-07-17_e7_gptoss120b/README.md)                 |
| DeepSeek-V4-Flash (offload)  | llama.cpp master | IQ2_XXS GGUF (2.06 bpw)      | 1M `ext`      | — (E9 wip)          | 2.9 Vulkan / 1.1 CPU `local` | attn on 2×5090 `local` | SWE 79.0 `ext`          | untested (E9 wip)                  | [E9](runs/2026-07-18_e9_deepseek_v4_flash_llamacpp/README.md) |

## Long-context attempts

| Config                  | Runtime         | Advertised ctx | Allocated ctx | Effective ctx | Notes                                                                                                                                                                                                                          | Run                                                  |
| ----------------------- | --------------- | -------------- | ------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| Qwen2.5-7B-Instruct-1M  | vLLM 0.25.1 TP2 | 1,010,000      | **blocked**   | —             | dual-chunk attention needs flash-attn (no sm_120 kernel; FlashInfer errors on `layer_idx`). Memory fits (~28 GB KV); kernel doesn't. `local`                                                                                   | [E3](runs/2026-07-17_e3_qwen25_7b_1m/README.md)      |
| DeepSeek-V4-Flash W4A16 | vLLM 0.25.1 TP2 | 1,000,000      | **won't fit** | —             | CSA arch **runs** on sm_120 (Marlin W4A16 + fp8_ds_mla + Lightning Indexer init) — kernel not the blocker (cf. Qwen2.5-1M). But 80 GB caught between GPU OOM (needs more offload) and host OOM (load-time pinned ~2×). `local` | [E8](runs/2026-07-17_e8_deepseek_v4_flash/README.md) |
| _ceiling today_         | vLLM 0.25.1     | —              | ~256K         | 262K (E1)     | standard attention tops out ~256K; true 1M awaits newer vLLM/flash-attn-sm120 or SGLang                                                                                                                                        | [E1](runs/2026-07-17_e1_qwen3coder_awq/README.md)    |

## Historical (pre-program)

Numbers from before this program are in <benchmarks.md> and the dated
`runs/` records. They predate the current conventions and are not directly
comparable; treat them as `local~`/historical context, not baseline rows here.
