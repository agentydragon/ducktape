Observations
- Top prompts win by: action-first persistence, Plan→Act→Results→Next scaffolding, strict test gates, parallel batched tool calls, minimal narration, saving long logs to ./scratch with absolute paths, citing file_path:line_number, and asking only if destructive/ambiguous.

Hypotheses per variant
- template_insane_1.txt (exploit): Max-enforced operating mode with explicit checklists should drive consistent Plan→Act discipline and higher with_tools_pct, improving pass rates.
- template_insane_2.txt (exploit-lite): Ultra-compact structure plus explicit Next step mandate should reduce text-only replies and increase decisive iteration speed.
- template_insane_3.txt (strict test gate): Hard “tests-green or blocked” contract should increase test runs and fix loops, boosting scores on debugging/repair tasks.
- template_insane_4.txt (aggressive parallelism): Emphasizes batching parallel tool calls to cut latency and increase tool utilization on multi-check tasks.
- template_insane_5.txt (continue-until-green extreme): Zero-preamble + continue-until-green bias should maximize persistence on multi-step fixes.

Next steps
- Run all 5; compare mean, with_tools_pct, failure clusters. If strict gates reduce score on info-only prompts, blend 1+3 with slightly softer early-exit in next round.
