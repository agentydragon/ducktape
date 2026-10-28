---
name: debundle_integrator
description: Integrate multiple debundle lane-worker branches through a validated merge train. Use for cherry-picking worker commits, resolving expected spec/generated-output conflicts, running the adapter-provided gate and regen commands, isolating failing branches, and reporting landed versus failed work.
---

# Debundle Integrator

Combine worker results in dedicated isolated integration state. Inputs are the
receiving revision and graph/input identity, worker baselines and relative YAML
patches, completed check records, adapter checks, conflict policy, and push
permission.

Read [integrate worker results](references/docs/integration.md) before applying
patches. That guide owns baseline comparison, merge-train validation, conflict
policy, and isolation of failing branches. Individual green workers do not prove
the combined spec; never substitute an edited snapshot's starting verification
for integration checks.

Return final SHA, per-branch landed/failed/skipped status, conflicts resolved,
actual gate/runtime results, and follow-up assignments. Preserve worker intent;
report semantic conflicts instead of silently redesigning the change. Push only
when explicitly requested.

The [task chooser](references/README.md#choose-a-task) links to command recipes;
use the [bug reproduction workflow](references/docs/bug_reproductions.md) when
tooling blocks integration.
