Prompt proposals for 2025-08-18T15-30-00Z

Context and observations
- Current best (A1) mean: 3.07 ± 0.28. Strong signals from: 2025-08-18T12-00-00Z/{template_exploit_1,template_exploit_2,template_explore_2}.txt and 2025-08-17T11-22-00Z/template_explore_A.txt.
- Winning elements to amplify: action-first persistence; tight Plan→Act→Results→Next loop; ≤10-line evidence cap with scratch-file links; turnkey commands; immediate tests-now and iterate-until-green; parallel batching; file_path:line_number citations; concise tone.

Hypotheses and strategies
- Hypothesis H1 (Exploit): Stronger gates (continue-until-green + evidence cap + parallel batching) improve scores without hurting generality.
- Hypothesis H2 (Explore): Leaner narration and clarify-only-if-ambiguous reduce latency and tool churn, improving adherence and with_tools_pct.
- Hypothesis H3: More explicit parallelization guidance yields better performance on discovery-heavy or multi-check tasks.
- Hypothesis H4: Tighten error protocol (≤10-line excerpt + 1-line diagnosis + exact next command) reduces dead-ends and improves recoveries.

Variants
- template_exploit_B1.txt (exploit): Balanced intensification of A1; same semantics, firmer loop and artifacts policy; emphasizes turnkey commands and scratch logs; concise but complete.
- template_exploit_B2.txt (exploit): Stronger “continue-until-green” gate and parallelism; explicit evidence cap; minimal narration; best bet for highest score.
- template_exploit_B3.txt (free-choice): Tighter clarify-only-if-ambiguous plus the same PA-R-N loop; focuses on targeted questions only when necessary; aims to reduce handbacks.
- template_exploit_B4.txt (free-choice): Parallelization/throughput-first; stresses batching, caching findings, and avoiding redundant scans; good for discovery-heavy samples.
- template_exploit_B5.txt (explore): Clarity-first minimalism; ultra-lean narration + same verification/error gates; intended to probe whether even terser scaffolding boosts adherence.

Placeholders: All templates preserve {{toolsBlob}}, {{envGitBlobs}}, {{modelLine}}, {{mcpSection}} exactly once each.

Next steps
- Run eval on all five proposals against the same suite as A1; compare mean, ci95, cluster performance, with_tools_pct, and error traces.
- If B2 wins overall, merge elements into a consolidated A/B for the next round; if clusters split (e.g., B4 wins discovery), consider a hybrid.
- Inspect grades.jsonl for handback/verbosity/tool-churn deltas to tune clarify-only-if-ambiguous wording.
