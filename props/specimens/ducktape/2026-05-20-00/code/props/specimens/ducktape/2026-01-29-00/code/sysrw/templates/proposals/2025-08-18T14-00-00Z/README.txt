Prompt batch: amplify best-performing template (run 1755473534 mean=2.90)

Observations
- The top template (2025-08-17T11-22-00Z/template_explore_A.txt) scored 2.90 with higher with_tools_pct (~0.55) and concise, action-first guidance.
- High performers share: action-first persistence, strong continue-until-done clause, Plan/Act/Results/Next scaffolding, explicit evidence caps (≤10 lines) with ./scratch for logs, turnkey commands, and post-edit verification (lint/typecheck/tests). Variants that added heavy extra sections (e.g., long task management examples, URL policies) underperformed.
- Parallel tool batching correlates with better latency and slightly higher scores.

Hypotheses
- Strengthen persistence and reduce narration while keeping structure → incremental gains over 2.90.
- Emphasize verification gates (run tests/lint) and evidence caps to reduce meandering → fewer low scores on long tasks.
- Encourage batching independent tool calls and saving artifacts to ./scratch for long logs → improved tool efficiency without verbosity.

Variants
- template_exploit_A1.txt (Exploit): Closest to winner with explicit batching line; adds citations and turnkey commands. Expect +0–0.1.
- template_exploit_A2.txt (Exploit): Max-persistence loop with parallel discovery-first rule; concise structure. Expect similar or slightly better on long flows.
- template_exploit_A3.txt (Exploit): Adds explicit scratch-file rule for multi-line scripts (robustness) and clarity around Next-step continuation. Expect stability on build/test tasks.
- template_exploit_A4.txt (Exploit): GPT-5 tuned phrasing; minimal narration; same Plan/Actions/Results/Next; stress persistence on “continue”. Expect small gains on persistence-sensitive samples.
- template_exploit_A5.txt (Exploit): Emphasizes evidence cap and batching; compact operating template block for reliability. Expect improved consistency across samples.

Next steps
- Run all five against the eval.
- Compare cluster performance vs prior best; especially look at with_tools_pct, failure rationales citing verbosity, early stop, or lack of verification.
- If ties, blend best elements (A2 persistence + A3 scratch-file rule + A1 turnkey commands) into a composite.
