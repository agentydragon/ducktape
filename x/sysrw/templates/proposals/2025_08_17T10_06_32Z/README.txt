Rationale summary

Newest run analyzed: runs/run-1755424062
- Summary: mean=2.30 over n=115; grader errors due to context_length_exceeded present in progress logs.
- Observed issues from samples/grader context hints:
  • Overlong/verbose assistant responses and meta-preamble competing with token budget.
  • Inconsistent tool-first behavior (advice before acting; not batching/parallelizing simple calls).
  • Failure to continue action loop to completion; stopping after a single status line.
  • Handling of hooks/blocks not adaptive; no fallback then ask.
  • URL invention prohibition must be reiterated to avoid doc-guessing.

Templates

A) template_explore_A.txt — Tool-first & continuity
- Prioritizes tool invocation on verbs: check/run/fetch/search/list/diff/test/build.
- Enforces after-action one-line status + next step; proceed until done.
- Tightens output to ≤4 lines; saves long logs to files and cites paths.
- Encourages batching/parallelism; MCP-first for repo-aware ops.
- Keeps TodoWrite for 3+ step tasks.

B) template_explore_B.txt — Strict compliance & guardrails
- Bans preambles and policy restatements; direct answers only.
- Single-question clarification for conflicts/destructive ops.
- Stepwise loop: decide → do → one-line status → continue; error recipe with ≤10-line excerpt.
- Minimal refusal style with alternative; hard URL invention ban.
- TodoWrite for multi-step tracking.

C) template_explore_C.txt — Execution-focused agent loop
- Commit-execute-verify-iterate with tools-first discipline.
- Built-in error recovery: one safe fallback before asking.
- Minimal narration; store long outputs and cite paths.
- Single-line diagnosis + next command on errors.
- TodoWrite for ≥3 steps; code citations include file_path:line_number.

Tradeoffs
- A may over-trigger tools; good for action-heavy tasks, risk: extra calls on simple Q&A.
- B maximizes grading adherence and brevity; risk: too terse for nuanced design unless needed.
- C emphasizes robustness via fallback; slightly longer logic but higher fix rate on flaky tools.

Placeholders preserved: {{toolsBlob}}, {{envGitBlobs}}, {{modelLine}}, {{mcpSection}}
