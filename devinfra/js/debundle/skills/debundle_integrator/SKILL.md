---
name: debundle_integrator
description: Integrate multiple debundle lane-worker branches through a validated merge train. Use for cherry-picking worker commits, resolving expected spec/generated-output conflicts, running the adapter-provided gate and regen commands, isolating failing branches, and reporting landed versus failed work.
---

# Debundle Integrator

Follow the [integration procedure](references/docs/integration.md) in an isolated
checkout of the receiving revision. Obtain each worker's starting identity,
relative YAML patch and check results, plus the adapter's conflict and push
policy.

Integrate the reviewed patches. A saved [placement plan](references/docs/module_proposals.md)
binds its starting graph and cannot be replayed against another worker's edited
tree. Validate the combined spec and emitted behavior. Isolate failing patches
and return semantic conflicts to their authors.

Report the verified final target SHA, per-branch landed/failed/skipped status,
conflict resolutions, actual check results and follow-ups. Use the
[bug reproduction workflow](references/docs/bug_reproductions.md) for tooling blockers.
