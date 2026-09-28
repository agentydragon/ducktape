# Cost-efficient agent delegation

Research reviewed **2026-09-24**. Operational synthesis lives in
[AGENTS.md](../../AGENTS.md#delegation-and-model-selection); this folder records
the evidence, limitations, and choices behind it, not an installed skill.

- [Research report](research.md): decision model, dated costs, agentic routing and
  reasoning-effort studies, practical examples, and remaining evidence gaps.
- [Model-by-task evidence](task_evidence.md): where individual results live,
  concrete repeated-run examples, and Terminal-Bench version differences.
- [Reusable skills and prompts](reusable_skills.md): inspected implementations,
  what seems worth borrowing, and what conflicts with this repo's workflow.

Bottom line: high-effort inexpensive workers are a reasonable default for substantive
work with clear acceptance criteria. The decision unit is the **worker plus its
verification and recovery process**, not a model name in isolation. Public evals and
the owner's positive Luna experience support testing that default; they do not
establish a GPT-6 Luna success rate on ducktape tasks.

No third-party skills were installed, no eval workloads were run, and no trained
router was built. Sources below are research inputs, not additional agent instructions.
