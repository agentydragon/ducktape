---
name: debundle_orchestrator
description: Drive debundle specs toward readable, runnable output and improve the engine. Choose among passing-baseline extraction, atomic residual decomposition, gate-driven coarsening, targeted spec/tool repair, general inference, sound recognizers or audited annotations. Use for prioritizing blockers, coordinating authorized workers, and verifying downstream progress.
---

# Debundle Orchestrator

Own progress on the configured specs and the tool's correctness, usability,
speed and memory use. Choose work by its expected downstream result and total
cost, including author audit, review and repeated iteration. Respect the user's
architecture requirements and execution limits.

Read [drive specs and tool improvements](references/skills/shared/workflow.md#drive-specs-and-tool-improvements)
before selecting substantial work. It owns prioritization, the three solution
routes, evidence and reconsideration criteria. A sound specific recognizer or
an audited annotation can be the right lasting solution; general interpretation
is not the default prerequisite for progress.

Combine [forward extraction and repair](references/skills/shared/workflow.md#combine-forward-and-repair-work)
as needed within a task: extend a passing partial spec, reduce a failing spec
or source case, and jointly repair the spec/tool while preserving useful
reverse-engineered structure. These are composable tactics. Keep a reproducible
passing residual-only baseline for each bundle. For a rejection without an
obvious inexpensive repair, use the shared workflow's
[divide-and-conquer strategy](references/skills/shared/workflow.md#start-from-passing-residual-output-and-isolate-failing-assignments)
to retain useful passing extractions and isolate conflicting additions before
committing to a long semantic repair.

When separability is unclear, ask planning for
[atomic residual decomposition and gate-driven coarsening](references/docs/cli.md#refining-existing-modules-modules-decompose),
or a worker for an isolated
[`per_atomic_group` emission probe](references/SPEC.md#unassigned-source).
Use the resulting constraints to choose work; generated fragments are not a
semantic architecture proposal or proof that rejected splits are impossible.

For delegated rounds, read [coordinate a round](references/skills/shared/workflow.md#coordinate-a-round)
and the [adapter contract](references/skills/shared/workflow.md#adapter-contract).
Use the specialist roles when useful and authorized; dispatch writers with
isolated complete inputs and explicit scope. Require completed execution
evidence and validation of the combined result. The
[task chooser](references/README.md#choose-a-task) routes individual authoring
operations.

If Debundle tooling blocks a lane, follow the
[bug reproduction workflow](references/docs/bug_reproductions.md).
