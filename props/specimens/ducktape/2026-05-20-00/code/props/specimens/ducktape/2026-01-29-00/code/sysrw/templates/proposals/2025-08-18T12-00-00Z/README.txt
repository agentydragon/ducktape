Prompt proposals for GPT-5 agent (system_rewriter)
Timestamp: 2025-08-18T12:00:00Z
Dir: .

Observations from runs
- Baseline (baseline-1755469421): mean=2.27, with_tools=36%. Very terse rules + conflicting sections (one-word replies vs tool narration) likely harm.
- 1755469503: mean=2.53. Emphasized agent loop and brevity; modest tool use (41%).
- 1755473546: mean=2.66. “Balanced mode” with clarify-first improved; tools 41%.
- 1755473552: mean=2.64. Similar to balanced; slight grader noise.
- 1755473534: best so far mean=2.90, with_tools=55%. Strong action-first loop, verification, and concise evidence. Hypothesis: explicit Plan→Act→Verify loop, parallel batching, and evidence caps correlate with higher scores.
- 1755469475: mean=2.05. Overly strict/terse variant underperformed (likely guidance conflicts + too little acting).

Failure clusters from grades (sampled via grep)
- Stopping early after status update instead of continuing to completion.
- Excess narration/preamble or policy text where not helpful.
- Not verifying (tests/lint) after edits; missing reruns.
- Redundant searches and lack of batching/parallel calls.
- Weak error triage (no excerpt/next-command), poor use of ./scratch for long logs.

Design hypotheses
1) Make the Plan→Act→Verify loop explicit; require continuing until done or blocked. Cap evidence length; save long outputs to ./scratch with absolute paths.
2) Encourage parallel/batched tool calls and targeted rg/Read to reduce latency and redundancy.
3) Require verification after edits and specify when to ask vs proceed (single clarifying question only for ambiguity/risk).
4) Keep responses concise (2–5 sentences/bullets), remove one‑word reply allowances, and avoid preambles unless valuable.
5) Add concrete error discipline: ≤10-line excerpt + one-line diagnosis + next exact command.

Variants
- template_exploit_1.txt (exploit): Best-shot Plan→Act→Verify with strong batching and evidence caps; DRY + citations; TodoWrite for ≥3 steps.
- template_exploit_2.txt (exploit): Even leaner text with stronger artifact pathing and copy/paste commands; aggressive continuation until green.
- template_balanced_1.txt (balanced): Clarify-first guard; similar loop but softer tone for breadth; keeps evidence/verification rules.
- template_explore_1.txt (explore): Tight brevity + strict 2-round context budget before acting; probes whether lower search improves scores.
- template_explore_2.txt (explore): Aggressive persistence + self-verification; probes whether fewer questions and more action boosts difficult samples.

Placeholders: All templates include exactly once each: {{toolsBlob}}, {{envGitBlobs}}, {{modelLine}}, {{mcpSection}}.

Next steps
- Run all five templates through the eval harness.
- Compare mean/CI and with_tools_pct vs 1755473534. Expect exploit_1/2 to meet or exceed ~2.90; explore_1 may regress on deep-context items but inform optimal search budget; explore_2 may help early-stop failures.
