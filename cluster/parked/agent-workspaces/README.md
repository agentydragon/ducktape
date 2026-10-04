# Parked standalone Codex workspaces

The generator stays in `cluster/cdk8s/agent_workspaces.py`; its reproducible
namespace, quota, Codex SandboxTemplate, warm pool, and janitor are rendered here.
The last image pin is preserved in `image-pins/`. Flux no longer deploys this tree
or advances that pin. The workspace image and `devinfra/ws` client are retained.

## Retirement

Retirement first reconciled the existing `agent-workspaces-app` Flux owner against
an empty directory to prune its namespace and warm pool. Merely suspending that
owner would have left existing Pods alive. The retirement deliberately did not
depend on a successful sandbox-controller upgrade.

Read-only preflight on 2026-10-02 at 21:18 UTC found no SandboxClaims;
`codex-xqxds` was an unclaimed `codex` warm-pool Sandbox. Its 10 GiB PVC,
`workspace-codex-xqxds`, was owned by that Sandbox. **The operator explicitly
approved deleting this workspace's data.** `shutdownPolicy: Retain` does not
protect a PVC from owner/namespace deletion. Other namespaces and their PVCs are
not part of this retirement.

Read-only live checks on 2026-10-02 at 21:57 UTC confirmed the owner Ready on the
#8806 merge (`93ee322b`) with an empty inventory, no workspace namespace or Pods,
and an empty PVC list. The verified-empty retirement owner and directory are now
removed. The shared `agent-sandbox-system` controller remains active for Agentplane.

The canonical LiteLLM key in `tf/gitops/litellm-keys` is deliberately retained for
revival; its namespace reflection becomes unused. Consider retiring that key
separately after confirming no surviving consumers. This change does not claim
that backing storage has been securely erased.

## Revival

Restore the call to `agent_workspaces.agent_workspaces_app` in manifest generation
and move this output/pin back under the active roots.
Restore `agent-workspaces` in the Forgejo image SecretStore consumer list and
`agent-workspace` in the image-automation roster. Regenerate and validate before
merging; reviving the namespace creates a fresh warm workspace, not the deleted data.

**Gotcha: an idle warm Sandbox can outlive an image bump.** Under `updateStrategy: Recreate`, an
image-pin-only template change did not replace the pool's idle Sandbox (observed on the retired
Haku pool, 2026-07-25), so a claim made after the bump adopted a pod still on the previous image;
a change that also edited other `podTemplate` fields did replace it (2026-08-26). Whether an
image-only bump ever recycles an idle pod is unsettled. The
`agents.x-k8s.io/sandbox-template-ref-hash` label cannot tell: it hashes the template reference,
not its content, so only the pod spec shows whether a pod carries the current template.
