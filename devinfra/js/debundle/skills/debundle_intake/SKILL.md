---
name: debundle_intake
description: Ground a checked debundle placement plan in source meaning and prepare one named worker packet. Use for reviewing discovered groups, semantic destinations and binding names without editing the spec or running gates.
---

# Debundle Intake

Turn a checked residual-placement plan into a source-backed worker packet.
Inputs are the complete plan, matching graph/modules/source, project conventions
and a scratch directory. Do not edit YAML, run gate/regeneration commands or
integrate branches.

Read [prepare seeds](references/docs/planning.md#prepare-seeds) for source
review and the `seeds.json` format. Use the
[task chooser](references/README.md#choose-a-task) for inspection commands.
Keep the snapshot-bound plan together: jointly required atoms and destinations
are not independently accepted tasks. Attach semantic review and naming
recommendations separately. Return destination changes or combined plans to
planning for a complete check.

Use focused implementation and caller evidence. Route destination decisions
needing broad architectural discovery to the architect. Report ambiguous meaning
or selector gaps explicitly. Graph acceptance still needs selector realization,
normal materialization and behavioral evidence; `incomplete` proves no negative.
If tooling blocks the task, follow
[bug reproductions](references/docs/bug_reproductions.md).
