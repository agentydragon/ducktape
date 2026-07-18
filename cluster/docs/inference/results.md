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

## Coding-agent configurations

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
