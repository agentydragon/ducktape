# agent-sandbox — disposable agent workspaces

[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
controller plus a Codex LLM-lane template in `agent-workspaces`: click-a-command
disposable dev workspaces for agents — the agent-box workflow (a machine you go
to, creds already wired, Claude CLI installed) minus the persistence.

## Layout

- `controller/` — the upstream v0.5.5 combined release asset, fetched directly
  by Kustomize from the documented GitOps install URL. The asset contains one
  controller Deployment, core and extension CRDs, RBAC, Services, and the
  webhook configuration. `patches.yaml` keeps Ducktape's namespace label,
  OVH-only controller placement, restricted security context, and health probes.

  v0.5.5 is the first release used here after the documented release gate: its
  `sandbox-with-extensions.yaml` asset is a single collision-free install for
  GitOps engines. The old v0.5.1 GitRepository + HelmRelease workaround is
  removed; CRDs now remain owned by this Flux Kustomization and are upgraded
  from the pinned upstream release asset (SHA-256
  `aaa9d931acb8af90a0a458b6d72bd245d224faac7117e8a241f9e7086acc24e9`).

- `workspace-image/` — the dedicated
  `git.allegedly.works/ducktape-ci/agent-workspace` image (Claude Code + Codex
  CLIs, dev basics, no baked credentials, no haku coupling). WebFetch/WebSearch
  are denied in baked Claude settings. Built by
  `.github/workflows/agent-workspace-image.yml` on `devel` pushes into the
  Forgejo registry (pull credential provisioned in code by
  `tf/gitops/forgejo-images` — no GHCR visibility clicking); Flux image
  automation rolls the template's tag. Until the first post-merge build lands
  the warm pod sits in `ImagePullBackOff` and self-heals when the image
  appears. Tradeoff (same as codex-pod): a Forgejo outage blocks image pulls.

- `workspaces/{namespace,app}/` — the dedicated `agent-workspaces` namespace
  (own ResourceQuota/LimitRange) and the LLM-lane `SandboxTemplate`s + warm
  pools + janitor `CleanupPolicy`. Templates are named by lane like the haku
  zones — `codex` (OpenAI Codex-account models, `codex` CLI) via LiteLLM.
  Deliberately **not** `claude-sandbox` — that
  namespace is Claude's own disposable in-cluster scratch space, and hosting
  workspaces there would mix tenants and quotas.

## Operating a workspace

One object drives everything: a `SandboxClaim` named after your task. The claim
adopts a pre-warmed `Sandbox` from the pool — **the sandbox keeps its
pool-generated name** (`workspace-xxxxx`), which is also the pod name; the
claim's `status.sandbox.name` tells you which one you got (verified live —
upstream's "claim name becomes the pod name" example does not hold for
warm-pool adoption). All commands assume `-n agent-workspaces`. The namespace is operator-only by design: no
agent group has any RBAC in it, so stamping and disposing workspaces takes
your admin kubeconfig or Headlamp, and the workspace pods themselves mount no
ServiceAccount token at all.

### Create

```bash
kubectl -n agent-workspaces apply -f - <<EOF
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: ws-mytask
spec:
  warmPoolRef:
    name: codex
  lifecycle:
    shutdownPolicy: Delete
    shutdownTime: "$(date -u -d '+8 hours' +%Y-%m-%dT%H:%M:%SZ)"
EOF
```

Always set `shutdownTime` — the default policy is `Retain`, and an unclaimed
deadline means the workspace lives until the 7-day janitor gets it.

Then resolve the adopted sandbox once and reuse it as the handle:

```bash
WS=$(kubectl -n agent-workspaces get sandboxclaim ws-mytask -o jsonpath='{.status.sandbox.name}')
```

### Inspect

```bash
kubectl -n agent-workspaces get sandboxclaims,sandboxes,pods   # what exists
kubectl -n agent-workspaces get sandbox "$WS" -o yaml          # conditions (Ready/Suspended/Finished), nodeName, podIPs
kubectl -n agent-workspaces describe sandboxwarmpool codex   # pool readiness (readyReplicas)
```

### Work in it

```bash
kubectl -n agent-workspaces exec -it "$WS" -c workspace -- bash
```

Inside: `/workspace` is the PVC (survives pod restarts and suspend/resume),
`claude` and `codex` are on `PATH`, and the Anthropic-wire env
(`ANTHROPIC_BASE_URL`/`AUTH_TOKEN`/`MODEL`) is already pointed at the cluster
LiteLLM with a workspace virtual key, so `git clone ... && claude` just works.
Moving files and reaching ports:

```bash
kubectl -n agent-workspaces cp ./notes.md "$WS":/workspace/ -c workspace
kubectl -n agent-workspaces cp "$WS":/workspace/out.tar.gz ./out.tar.gz -c workspace
kubectl -n agent-workspaces port-forward "pod/$WS" 8888:8888   # dev server in the workspace
```

### Extend / pause / resume

```bash
# more time: push the deadline out (claim's lifecycle is authoritative;
# verify it propagated to the Sandbox)
kubectl -n agent-workspaces patch sandboxclaim ws-mytask --type=merge \
  -p '{"spec":{"lifecycle":{"shutdownTime":"'"$(date -u -d '+24 hours' +%Y-%m-%dT%H:%M:%SZ)"'"}}}'
kubectl -n agent-workspaces get sandbox "$WS" -o jsonpath='{.spec.shutdownTime}'

# pause: pod goes away, /workspace PVC stays; resume brings it back
kubectl -n agent-workspaces patch sandbox "$WS" --type=merge -p '{"spec":{"operatingMode":"Suspended"}}'
kubectl -n agent-workspaces patch sandbox "$WS" --type=merge -p '{"spec":{"operatingMode":"Running"}}'
```

### Dispose

```bash
kubectl -n agent-workspaces delete sandboxclaim ws-mytask
```

Or do nothing: `shutdownTime` garbage-collects it, and the `workspace-janitor`
CleanupPolicy reaps anything older than 7 days as a backstop.

### From Headlamp

Everything above also works point-and-click at
<https://headlamp.allegedly.works> (already deployed, Authentik OIDC —
`../../headlamp/`): the sandbox CRDs and their instances show up automatically
under Custom Resources, the Create button's YAML editor (with dry-run) takes
the `SandboxClaim` snippet above, the pod details view has the in-browser exec
terminal and logs, and deleting the claim disposes the workspace. No
agent-sandbox Headlamp plugin exists yet (upstream's dashboard is
roadmap-only, kubernetes-sigs/agent-sandbox#697); a small custom plugin adding
a "new workspace" button is a candidate follow-up.

### Troubleshooting

- **Claim stuck unclaimed**: pool exhausted or template broken —
  `describe sandboxwarmpool codex`, then `kubectl -n agent-workspaces get events`.
- **Pod `Pending`**: usually the namespace ResourceQuota
  (`workspaces/namespace/resourcequota.yaml`) or PVC binding —
  `describe pod ws-mytask`.
- **Controller-side questions**: `kubectl -n agent-sandbox-system logs deploy/agent-sandbox-controller`.

Standalone `Sandbox` objects (own `podTemplate`, no warm pool) also work — see
[the lifecycle docs](https://agent-sandbox.sigs.k8s.io/docs/sandbox/lifecycle/).

## Credentials

**LLM traffic goes through the cluster LiteLLM, never direct to a provider.**
Each lane holds its own virtual key minted by
<../../../../tf/gitops/litellm-keys/> and reflector-mirrored into this
namespace — model allowlisting and per-lane usage observability, deliberately
no budget caps; deleting a lane's `litellm_key.*` TF resource is that lane's
LLM kill switch.

- `codex`: the image bakes `~/.codex/config.toml`
  (`workspace-image/codex-config.toml`) with a LiteLLM provider over the
  Responses API; the template supplies `LITELLM_API_KEY` from
  `litellm-key-agent-workspaces-codex` (`chatgpt/oai-responses/*`
  Codex-account models, same allowlist as codex-pod).

Other credential classes follow the zones/codex-pod pattern: mirror a Secret
into `agent-workspaces` (reflector or ESO) and reference it from the template.

## Isolation

Workspaces run as plain runc pods in `agent-workspaces` (trusted, personal-use;
quota-capped by the namespace ResourceQuota/LimitRange, no agent RBAC, no
ServiceAccount tokens). No gVisor/Kata RuntimeClass exists in this cluster
yet; when the gVisor Talos system extension lands, add
`runtimeClassName: gvisor` to the template — the CRDs are designed for exactly
that. Untrusted workloads stay in the haku zones perimeter until then.

## Follow-ups (not in this change)

- **SSH**: run sshd in a dedicated workspace image and expose it via the
  SNI-multiplexed TLSRoute + `ProxyCommand` path sketched in
  <../../../docs/plans/vm_ssh_exposure.md> (per-workspace Envoy listener
  ports don't scale to dynamic sandboxes). Until then `kubectl exec` is the
  door.
- **Web UI**: `coder/agentapi` (Apache-2.0) per sandbox behind an
  Authentik-guarded `HTTPRoute`.
- **Haku dispatch**: the archived `haku/x/dispatch/k8s_jobs.py` remains a design
  reference for a future `SandboxClaim`-based launcher; the zone perimeter
  (namespace + Kyverno mitmproxy injection) would apply to sandbox pods unchanged.
