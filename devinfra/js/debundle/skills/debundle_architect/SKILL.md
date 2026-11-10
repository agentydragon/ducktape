---
name: debundle_architect
description: Audit debundled modules for idiomatic JavaScript structure and infer conventions from behavior. Use for architecture notes, module-boundary cleanup, and reorganization that distinguishes semantic ownership from atomic groups and gate constraints. Route separability probes to planning; this role does not edit the spec or run gates.
---

# Debundle Architect

Use this role to audit the emitted module tree and record source-backed
architecture conventions and reorganization recommendations. Read the
[architecture review guide](references/docs/architecture_review.md) for the
full audit, report snippets, metric rules, and output format. If tooling blocks
the task, follow [bug reproductions](references/docs/bug_reproductions.md).

Required inputs are the active modules tree, matching emitted JavaScript and
dependency graph, available directory reports and project convention docs, and
the paths for architecture notes and module-reorganization output. Current
names and paths are fallible evidence; ground conclusions in implementation
behavior and graph neighborhoods. The architect is read-only with respect to
spec YAML: do not edit modules, run gates, or regenerate output. Do not read
large minified residual bodies; ask intake to ground them. Produce current-state
notes plus worker-ready proposals, and route pure symbol naming to the
[naming task guide](references/docs/naming.md). Use [module-shape guidance](references/skills/shared/module_shape.md)
for seam and layer criteria and the [shared workflow](references/skills/shared/workflow.md)
when coordinating a round.

Use planning's
[atomic decomposition and coarsening reports](references/docs/cli.md#refining-existing-modules-modules-decompose)
to understand which proposed seams the current gate accepts. Small generated
fragments need not be good modules; large atomic or coarsened groups may reflect
conservative analysis. Ground semantic homes in source behavior and callers.
