# OpenClaw and OpenShell retirement

The original OpenClaw gateway, its operator, and the OpenShell sandbox stack were
removed from the active cluster configuration on 2026-07-31. `public-coder-agent`
became the reference agent: it runs the same OpenClaw image as a plain Deployment
with `sandbox.mode: "off"`. The evaluated alternatives and retirement rationale
remain in <../../docs/personal_agents/verdicts.md>.

## Teardown history

The first removal left an `OpenClawInstance/openclaw` custom resource behind. Its
controller was already gone, so its dangling finalizer retained a scaled-to-zero
StatefulSet and 21 GiB of PVCs. Clearing the finalizer with a merge patch
(`finalizers: null`) let garbage collection remove the StatefulSet, PVCs, Service,
NetworkPolicy, PDB, RBAC, ServiceAccount, ConfigMap, and gateway token. The three
remaining `openclaw.rocks` CRDs were then deleted.

A second layer from OpenShell was also removed: its OIDC JWKS ConfigMap,
`openshell-gateway-certgen` ServiceAccount and RBAC, and four unmanaged TLS/JWT
Secrets. The `openshell-sandboxes` and `openshell-system` namespaces and the
`openshell.lenshq.io` CRDs were already gone.

Server-side apply could not clear the custom resource finalizer because
`metadata.finalizers` is a set-type list owned by another field manager. A merge
patch was required.

## Final namespace retirement

The otherwise empty `openclaw-gateway` and `openclaw-sandbox` namespaces were
retained temporarily because three unique credentials were still pinned to
`openclaw-gateway` by SOPS-protected Kubernetes metadata. In August 2026 those
credentials were re-encrypted into `claude-sandbox` and moved to
`cluster/k8s/agents/shared-secrets/`:

- `openclaw-anthropic-api-key`
- `openclaw-openai-api-key`
- `openclaw-telegram-bot-token`

They remain valid, GitOps-managed credentials but are not mounted by a workload or
reflected into another namespace. The obsolete reflector destinations were removed
from their source Secrets, and the two empty namespaces and their Flux
Kustomizations were retired.

The `openclaw` ImageRepository and ImagePolicy under
`cluster/k8s/flux-image-automation-ghcr/` remain active because
`public-coder-agent` still consumes that image. The retired gateway's LiteLLM key remained only through the time-boxed `agent-lab` experiment and was
removed with that namespace after the experiment ended.
