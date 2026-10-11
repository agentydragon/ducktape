# Same-storage runner image upgrade: staging evidence

On 2026-10-09 UTC, an existing **Codex** Thread in a staging `runner-ducktape`
Sandbox continued after its runner image changed. This establishes the
pause/patch/resume route as viable for that Thread; it does not make it a
reliable automatic migration or update step. The operator is comfortable
using the guarded procedure manually for chosen Codex Sandboxes.

## Observed sequence

1. The operator paused the Sandbox. Its `spec.operatingMode` was `Suspended`,
   the controller had terminated its Pod, and its state PVC was still `Bound`.
   The runner container image in the stored Sandbox `spec.podTemplate` was the
   older image, without the newly merged `bbr` key-file support.
2. A JSON Patch against the **Sandbox CR** (not the old Pod or the shared
   template) tested the Sandbox UID, suspended mode, runner container name and
   old image before replacing only that container's image. The operator read
   back the new image while the Sandbox was still paused. The state PVC was
   not modified.
3. After resuming, the controller created a **new Pod UID** using the new
   runner image and mounting the **same state PVC UID**. The original Codex
   Thread continued, recognized its configured BuildBuddy key-file path, and
   completed `bbr build //:rbe_linux_x64`. BuildBuddy reported the invocation
   complete with a successful Bazel exit code.

This demonstrates a practical image update without replacing the Thread's
storage or losing the observed conversation's ability to continue. It does
**not** prove exact native-history/reasoning preservation, journal prefix and
cursor equivalence, pending-command behavior during a pause, Claude parity,
or recovery from an incompatible image. Those remain distinct acceptance
checks before automatic runner image rollout is enabled. They do not block
operator-supervised manual Codex upgrades.

The operator performed the mutation with `kubectl patch --type=json`; the
current `kubernetes_admin` MCP tool only offers full server-side apply for
general resources. Its `resources_get` response masks the Sandbox Secret
volume reference, so reapplying that result is unsafe. The task DAG tracks a
narrow, operator-approved JSON Patch Action as a follow-up.
