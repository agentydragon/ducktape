---
name: debundle_architect
description: Audit a debundle spec's named modules for idiomatic JavaScript structure, infer project conventions from source behavior, and maintain current-state architecture notes and reorganization recommendations. Use for structural review, convention discovery, module-boundary cleanup, and reorg planning in any debundle target.
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
