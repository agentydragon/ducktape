Proposal set 2025-08-17T11-22-00Z

Summary of findings from prior runs
- Best mean score: explore_C (2.53) > explore_A (2.35) > baseline (2.27) > explore_B (2.05).
- Common failure modes in grader rationales and reports:
  - Status-only replies instead of continuing actions (especially after “continue without stopping”).
  - Not running tests/triaging broader failures when user complains about failing suites.
  - Over-terse answers where options/trade-offs were expected; missing turnkey commands (e.g., macOS install snippets).
  - Promising next steps rather than delivering immediate results when feasible.

Templates
- template_explore_A.txt (Exploit): Strong “action-first, continue-until-done” bias; forbids status-only replies; mandates running tests and computing results now; turnkey commands when relevant; self-verify.
- template_explore_B.txt (Balanced): Clarify-first on ambiguity; otherwise act to completion; structured replies; provide actionable commands; light self-check.
- template_explore_C.txt (Explore): Stopless + turnkey + evidence-first; emphasizes computing results now, showing short evidence, and using structured Plan/Actions/Results/Next.
