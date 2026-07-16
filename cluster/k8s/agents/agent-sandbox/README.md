# agent-sandbox — disposable agent workspaces

[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
controller plus a `workspace` template in `agent-workspaces`: click-a-command
disposable dev workspaces for agents — the agent-box workflow (a machine you go
to, creds already wired, Claude CLI installed) minus the persistence.

## Layout

- `controller/` — the upstream controller (v0.5.1) installed from the in-repo
  Helm chart via a `GitRepository` + `HelmRelease`, with extensions enabled
  (`SandboxTemplate`/`SandboxClaim`/`SandboxWarmPool` CRDs).

  **Deviation** from the upstream `kubectl apply -f manifest.yaml` install: at
  v0.5.1 the release assets `manifest.yaml` and `extensions.yaml` both declare
  the `agent-sandbox-controller` Deployment (the second `kubectl apply` is
  expected to overwrite the first), which under Flux is either a duplicate
  resource ID or two SSA owners fighting over `args`. The Helm chart renders a
  single Deployment with `--extensions`.

  <!-- CLEANUP(added 2026-07-16): switch controller/ to the single collision-free
    release manifest (upstream main's k8s/ kustomization says the next release
    after v0.5.1 ships a sandbox-with-extensions.yaml asset for GitOps engines);
    drop the GitRepository + HelmRelease then. -->

- `workspaces/{namespace,app}/` — the dedicated `agent-workspaces` namespace
  (own ResourceQuota/LimitRange) and the `workspace` `SandboxTemplate` + warm
  pool + janitor `CleanupPolicy`. Deliberately **not** `claude-sandbox` — that
  namespace is Claude's own disposable in-cluster scratch space, and hosting
  workspaces there would mix tenants and quotas.

## Operating a workspace

One object drives everything: a `SandboxClaim` named after your task. The claim
adopts a pre-warmed `Sandbox` from the pool; claim name == sandbox name == pod
name, so every `kubectl` command below uses the same handle. All commands
assume `-n agent-workspaces`. The namespace is operator-only by design: no
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
    name: workspace
  lifecycle:
    shutdownPolicy: Delete
    shutdownTime: "$(date -u -d '+8 hours' +%Y-%m-%dT%H:%M:%SZ)"
EOF
```

Always set `shutdownTime` — the default policy is `Retain`, and an unclaimed
deadline means the workspace lives until the 7-day janitor gets it.

### Inspect

```bash
kubectl -n agent-workspaces get sandboxclaims,sandboxes,pods   # what exists
kubectl -n agent-workspaces get sandbox ws-mytask -o yaml      # conditions (Ready/Suspended/Finished), nodeName, podIPs
kubectl -n agent-workspaces describe sandboxwarmpool workspace # pool readiness (readyReplicas)
```

### Work in it

```bash
kubectl -n agent-workspaces exec -it ws-mytask -c workspace -- bash
```

Inside: `/workspace` is the PVC (survives pod restarts and suspend/resume),
`claude` and `codex` are on `PATH`, and the Anthropic-wire env
(`ANTHROPIC_BASE_URL`/`AUTH_TOKEN`/`MODEL`) is already pointed at the cluster
LiteLLM with a workspace virtual key, so `git clone ... && claude` just works.
Moving files and reaching ports:

```bash
kubectl -n agent-workspaces cp ./notes.md ws-mytask:/workspace/ -c workspace
kubectl -n agent-workspaces cp ws-mytask:/workspace/out.tar.gz ./out.tar.gz -c workspace
kubectl -n agent-workspaces port-forward pod/ws-mytask 8888:8888   # dev server in the workspace
```

### Extend / pause / resume

```bash
# more time: push the deadline out (claim's lifecycle is authoritative;
# verify it propagated to the Sandbox)
kubectl -n agent-workspaces patch sandboxclaim ws-mytask --type=merge \
  -p '{"spec":{"lifecycle":{"shutdownTime":"'"$(date -u -d '+24 hours' +%Y-%m-%dT%H:%M:%SZ)"'"}}}'
kubectl -n agent-workspaces get sandbox ws-mytask -o jsonpath='{.spec.shutdownTime}'

# pause: pod goes away, /workspace PVC stays; resume brings it back
kubectl -n agent-workspaces patch sandbox ws-mytask --type=merge -p '{"spec":{"operatingMode":"Suspended"}}'
kubectl -n agent-workspaces patch sandbox ws-mytask --type=merge -p '{"spec":{"operatingMode":"Running"}}'
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
  `describe sandboxwarmpool workspace`, then `kubectl -n agent-workspaces get events`.
- **Pod `Pending`**: usually the namespace ResourceQuota
  (`workspaces/namespace/resourcequota.yaml`) or PVC binding —
  `describe pod ws-mytask`.
- **Controller-side questions**: `kubectl -n agent-sandbox-system logs deploy/agent-sandbox-controller`.

Standalone `Sandbox` objects (own `podTemplate`, no warm pool) also work — see
[the lifecycle docs](https://agent-sandbox.sigs.k8s.io/docs/sandbox/lifecycle/).

## Credentials

**LLM traffic goes through the cluster LiteLLM, never direct to a provider.**
The template points `ANTHROPIC_BASE_URL` at
`litellm.litellm.svc.cluster.local:4000` and reads `ANTHROPIC_AUTH_TOKEN` from
`litellm-key-agent-workspaces` — a virtual key minted by
<../../../../tf/gitops/litellm-keys/> (alias `agent-workspaces`, GLM-model
allowlist shared with the haku zai lane, $25/30d budget) and
reflector-mirrored into this namespace. That buys budget capping, model
allowlisting, and usage observability per workspace lane; deleting the
`litellm_key.agent_workspaces` TF resource is the LLM kill switch.
`ANTHROPIC_MODEL`/`ANTHROPIC_SMALL_FAST_MODEL` default to allowlisted GLM
models so Claude Code doesn't request `claude-*` names the key rejects.

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
- **Haku dispatch**: `haku/dispatch/k8s_jobs.py` could stamp `SandboxClaim`s
  instead of `Job`s to gain warm starts and pause/resume; the zone perimeter
  (namespace + Kyverno mitmproxy injection) applies to sandbox pods unchanged.
