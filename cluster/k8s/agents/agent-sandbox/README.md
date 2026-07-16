# agent-sandbox — disposable agent workspaces

[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
controller plus a `workspace` template in `claude-sandbox`: click-a-command
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

- `workspaces/` — the `workspace` `SandboxTemplate` + warm pool in
  `claude-sandbox`, plus a janitor `CleanupPolicy` for leaked sandboxes.

## Usage

Create a workspace (adopts a pre-warmed sandbox, so it's ready in seconds):

```bash
kubectl apply -f - <<EOF
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: ws-mytask
  namespace: claude-sandbox
spec:
  warmPoolRef:
    name: workspace
  lifecycle:
    shutdownPolicy: Delete
    shutdownTime: "$(date -u -d '+8 hours' +%Y-%m-%dT%H:%M:%SZ)"
EOF
```

Go to it (the claim name is the sandbox/pod name):

```bash
kubectl -n claude-sandbox exec -it ws-mytask -c workspace -- bash
# then inside: claude  (ANTHROPIC_BASE_URL/AUTH_TOKEN already wired to z.ai GLM)
```

Dispose early with `kubectl -n claude-sandbox delete sandboxclaim ws-mytask`;
otherwise `shutdownTime` garbage-collects it. The `workspace-janitor`
CleanupPolicy reaps anything older than 7 days as a backstop (same contract as
the rest of `claude-sandbox`).

Standalone `Sandbox` objects (own `podTemplate`, no warm pool) also work — see
[the lifecycle docs](https://agent-sandbox.sigs.k8s.io/docs/sandbox/lifecycle/).

## Credentials

Same delivery as the haku zones and codex-pod: Kubernetes Secret → env var in
the pod spec. The template wires `ANTHROPIC_AUTH_TOKEN` from the existing
`zai-api-key` Secret (mirrored into `claude-sandbox` by
`../claude-zai-key/`) and points `ANTHROPIC_BASE_URL` at z.ai's Anthropic
endpoint, so Claude Code CLI works out of the box. Additional credential
classes follow the same pattern: mirror the Secret into `claude-sandbox` (ESO
or sops) and reference it from the template.

## Isolation

Workspaces run as plain runc pods in `claude-sandbox` (trusted, personal-use —
same trust level as the rest of the namespace, quota-capped by the existing
ResourceQuota/LimitRange). No gVisor/Kata RuntimeClass exists in this cluster
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
