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

| Config                                                                             | Runtime | Quant | Allocated ctx | Effective ctx | Decode tok/s @128K | Peak VRAM | Coding quality | Tool calls | Run |
| ---------------------------------------------------------------------------------- | ------- | ----- | ------------- | ------------- | ------------------ | --------- | -------------- | ---------- | --- |
| _(none accepted yet — first row lands with E1, the k8s vLLM Qwen3-Coder baseline)_ |         |       |               |               |                    |           |                |            |     |

## Long-context attempts

| Config                                                                   | Runtime | Advertised ctx | Allocated ctx | Effective ctx | Notes | Run |
| ------------------------------------------------------------------------ | ------- | -------------- | ------------- | ------------- | ----- | --- |
| _(none accepted yet — first row lands with E3, the Nemotron 1M attempt)_ |         |                |               |               |       |     |

## Historical (pre-program)

Numbers from before this program are in <benchmarks.md> and the dated
`runs/` records. They predate the current conventions and are not directly
comparable; treat them as `local~`/historical context, not baseline rows here.
