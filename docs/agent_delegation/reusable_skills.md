# Reusable delegation skills and prompts

Inspected 2026-09-24. [Research report](research.md).

Recommendation: borrow small mechanisms, not an entire orchestration framework.
None of the inspected skill texts establishes cost-efficient GPT-6 delegation on
this repository through a matched worker-plus-parent evaluation. No skills were
installed or their scripts executed. Before copying implementation text, inspect
the license and pin a reviewed revision; license compatibility was not audited here.

## Superpowers: subagent-driven-development

[Inspected revision](https://github.com/obra/superpowers/blob/5bf4e78011075bcfc0dc295f0724994cd123ee71/skills/subagent-driven-development/SKILL.md).
Includes implementer/reviewer prompt templates, explicit model selection, scoped
task briefs and result reports, batching, repair loops, and persistent progress.

**Worth borrowing:** clear acceptance contracts; distinguish missing context from
reasoning failure; resume useful worker context; narrow re-review to changed fixes;
avoid replaying a whole session into each worker. Report concerns separately from
completed work.

**Do not import wholesale:** mandatory per-task and final model reviews can cost
more than parent verification for small changes. Its prohibition on parallel
implementers conflicts with this repo's explicit overlapping-PR policy. Fixed
repair-round thresholds, tier floors, and broad claims about cheaper models' extra
turns are heuristics, not measured constants for our harness and GPT-6 family.

Best fit: inspiration for a dispatch/report contract, not a replacement for AGENTS.

## Model Hierarchy skill

[Inspected revision](https://github.com/zscole/model-hierarchy-skill/blob/9095f8303847a60de9f564659e561f3f6fd0cdc4/SKILL.md).
Directly addresses economical model delegation: cheaper tiers for routine work,
batching, and escalation to the parent.

**Worth borrowing:** make the inexpensive default explicit and account for dispatch
overhead. The model should not silently inherit the most expensive session tier.

**Do not import wholesale:** its pricing/model examples are dated, and its proposed
work split and savings are not accompanied by a matched evaluation in the inspected
skill. Keyword-style “debugging/architecture implies premium” routing is too coarse.
Present-day Luna should not be confined to old cheap-model capabilities; verification
and recovery costs belong in the choice.

Best fit: a compact starting idea already captured in our synthesis, not a maintained
source for current prices or demonstrated task competence.

## Subagent Dispatch Economics

[Author's published skill text and discussion](https://www.reddit.com/r/PromptEngineering/comments/1wguwyh/subagent_dispatch_economics_skillmd/).
The post contains a procedure for deciding whether to delegate, whether to use fresh
or inherited context, and how to size concurrent work. It links a skill-generation
platform/repository; that is not evidence this exact skill is independently packaged
and tested there.

**Worth borrowing:** require an actual reason to spawn; consider whether the parent
needs the exploration itself or only a distilled result. An independent audit may
need fresh context to avoid inheriting the parent's conclusion.

**Do not import wholesale:** fork cost/availability is harness-specific. In our Codex
tool contract, full-history inheritance prevents explicit model overrides. Its
same-file serialization rule conflicts with our isolated-worktree/parallel-PR policy;
shared live resources still need safe handling. The post's author-reported testing
does not establish GPT-6 task-level reliability or measured cost savings.

Best fit: a pre-dispatch sanity check, not a long mandatory questionnaire.

## Research implementations versus operational skills

[Agent-as-a-Router](https://github.com/LanceZPF/agent-as-a-router) and
[SkillOrchestra](https://github.com/jiayuww/SkillOrchestra) offer research code, not
drop-in harness instructions. Their useful common idea is to retain verified,
task-specific experience. Installing a learned router or maintaining a separate
skill database is not justified by the evidence gathered for this change.

The lean contract we want from any future reusable implementation is:

```text
Task: deliverable, boundaries, relevant context, and acceptance checks.
Worker: supported model/effort; brief reason if departing from the cheap default.
Return: artifact/revision, exact checks, assumptions, concerns, remaining blocker.
Parent: verify the acceptance claim; repair/escalate according to the actual failure.
```

This is a description of the common pattern, not a second policy to maintain beside
[AGENTS.md](../../AGENTS.md#delegation-and-model-selection).
