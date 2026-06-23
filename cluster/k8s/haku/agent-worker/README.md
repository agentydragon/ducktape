# haku-worker — Managed Agents self-hosted worker (Runtime B)

In-cluster worker for Haku on Anthropic Managed Agents. It long-polls
Anthropic's work queue (`ant beta:worker poll`) and executes tool calls inside
`haku-sandbox`. Design, trust split, and the control-plane provisioning live with
the component: <../../../../haku/runtime/managed_agent/README.md>.

The image is the full-NixOS `.#haku-worker-image` (systemd PID 1), built and
pushed to `ghcr.io/agentydragon/haku-worker` by
`.github/workflows/haku-worker-image.yml`; Flux tracks the tag via the
`haku-worker` ImagePolicy.

## Shipped suspended

`flux-kustomization.yaml` has `suspend: true`. It is the first systemd-PID1 pod
in the cluster (runs as root PID 1, unprivileged; the worker process drops to
non-root `haku` via the systemd unit), and it can't authenticate until the
environment key exists. Nothing deploys until the operator activates it.

## Activation runbook

1. **Generate the environment key** in the Console (Environments →
   `haku-selfhosted` → _Generate environment key_). It is an `sk-ant-oat01-…`
   value, created only via the Console — never the API.
2. **Set the secret**: `sops cluster/k8s/haku/agent-worker/environment-key.sops.yaml`
   and replace the placeholder `environment_key` with the generated value.
3. **Confirm the env wiring** in `deployment.yaml`: `ANTHROPIC_ENVIRONMENT_ID`,
   `HAKU_GIT_HOST`, and `HAKU_DUCKTAPE_REPO_URL` (the last is a literal — adjust
   if ducktape isn't public / not on `HAKU_GIT_HOST`).
4. **Activate**: set `suspend: false` in `flux-kustomization.yaml`, commit, push.
   Watch the Deployment — being the first systemd container here, verify it boots
   unprivileged (cgroup-v2 delegation, writable `/run`); tune the pod
   `securityContext` if systemd needs an adjustment.
5. **Smoke test**: `ant beta:deployments run --deployment-id depl_011DSrUoXuhoDWJoPyDuePqR`
   (org `ANTHROPIC_API_KEY`, off the worker) and watch the session in the Console.
