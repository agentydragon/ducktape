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

| Config              | Runtime         | Quant              | Allocated ctx | Effective ctx | Decode tok/s @128K | Peak VRAM            | Coding quality         | Tool calls                         | Run                                                      |
| ------------------- | --------------- | ------------------ | ------------- | ------------- | ------------------ | -------------------- | ---------------------- | ---------------------------------- | -------------------------------------------------------- |
| Qwen3-Coder-30B-A3B | vLLM 0.25.1 TP2 | AWQ 4-bit + FP8 KV | 262K `local`  | 262K `local~` | 199 `local`        | 30.7/29.9 GB `local` | leaderboard `ext`      | pass `local`                       | [E1](runs/2026-07-17_e1_qwen3coder_awq/README.md)        |
| gpt-oss-20b         | vLLM 0.25.1 TP1 | native MXFP4       | 128K `local`  | 128K `ext?`   | ~1000–1500 `local` | 15 GB `local`        | HumanEval sat. `local` | single/multi ✓, parallel ✗ `local` | [E2](runs/2026-07-17_e2_gptoss_vllm_vs_ollama/README.md) |
| gpt-oss-20b         | Ollama (GGUF)   | MXFP4→bf16 compute | 128K `local`  | 128K `ext?`   | ~600–1150 `local`  | 15 GB `local`        | HumanEval sat. `local` | single/multi ✓, parallel ✗ `local` | [E2](runs/2026-07-17_e2_gptoss_vllm_vs_ollama/README.md) |

## Long-context attempts

| Config                                                                   | Runtime | Advertised ctx | Allocated ctx | Effective ctx | Notes | Run |
| ------------------------------------------------------------------------ | ------- | -------------- | ------------- | ------------- | ----- | --- |
| _(none accepted yet — first row lands with E3, the Nemotron 1M attempt)_ |         |                |               |               |       |     |

## Historical (pre-program)

Numbers from before this program are in <benchmarks.md> and the dated
`runs/` records. They predate the current conventions and are not directly
comparable; treat them as `local~`/historical context, not baseline rows here.
