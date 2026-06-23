# haku/runtime/managed_agent — Haku on Anthropic Managed Agents (self-hosted)

Runtime B from <../../plans/runtime_options.md>: Anthropic runs the agent loop;
tool execution runs in a worker **you** run in `haku-sandbox` (self-hosted
sandbox, `config.type: self_hosted`). Full design + tradeoffs:
<../../plans/managed_agents.md> (+ <../../plans/managed_agents_artifacts.md>).

## "ant-all-the-way" — no `anthropic` Python SDK

This component uses **only the `ant` CLI**, no `anthropic` Python SDK. Why: the
SDK's self-hosted worker (`EnvironmentWorker`) and session APIs need
`anthropic>=0.103`, but `agent-framework-anthropic` (used by Runtime C and the
skill/grocy evals) hard-pins `anthropic==0.80.0` in the shared lockfile. The
`ant` binary carries its own deps, so leaning on it sidesteps the conflict
entirely — no lockfile bump, no second pip version, Runtime C untouched.

The cost: the **warm-session supervisor is deferred**. The wake trigger is a
scheduled deployment that fires a fresh session each tick; `haku-state` (git) is
the durable memory, so a cold session just re-orients. Adding a warm supervisor
later is the one thing that would reintroduce the SDK-version question.

## Pieces

| File                    | Role                                                             | Runs on       |
| ----------------------- | ---------------------------------------------------------------- | ------------- |
| `haku.environment.yaml` | self-hosted environment (`ant beta:environments create`)         | control plane |
| `haku.agent.yaml`       | agent: thin `system` pointer, fixed toolset + tana `mcp_toolset` | control plane |
| `haku.deployment.yaml`  | scheduled-deployment wake trigger                                | control plane |
| `provision.sh`          | one-shot: create environment/agent/vault/deployment via `ant`    | operator / CI |
| `entrypoint.sh`         | clone ducktape + haku-state, then `ant beta:worker poll`         | `haku-worker` |
| `nixos.nix`             | full-NixOS worker image (`nix build .#haku-worker-image`)        | CI / build    |

## Trust split — keep the org key off the worker

- **Worker pod** (`haku-sandbox`): only `ANTHROPIC_ENVIRONMENT_KEY`
  (`sk-ant-oat01-…`, one environment's work queue). A prompt-injected tool call
  can't reach the control plane.
- **Provisioning** (`provision.sh`, deployment management, `…:work stats`): the
  org-scoped `ANTHROPIC_API_KEY`, run from CI / the operator laptop — **never**
  on the worker host.

## Worker image (`nix build .#haku-worker-image`)

A full-NixOS container image (`nixos.nix`, systemd PID 1 — declaratively
consistent with the rest of the fleet) carrying `bash`, `git`, `kubectl`,
`postgresql` (`psql`), `curl`, `jq`, `cacert`, `fastmcp`, and the **`ant` CLI**
(the `anthropic-cli` nix package). The `haku-worker` systemd unit runs
`entrypoint.sh` as the non-root `haku` user. Build the rootfs tarball with
`nix build .#haku-worker-image`; CI imports it (`podman import … --change 'CMD
["/init"]'`) and pushes to GHCR, pinned by Flux — see
<../../../cluster/docs/container-images.md>.

Runs **unprivileged** on the cluster's cgroup-v2 (Talos) nodes — k8s boots it
with `command: ["/init"]`; no `--privileged`, no extra caps. The fixed `ant`
toolset is `bash/read/write/edit/glob/grep`; Haku reaches Plaid (`psql`),
Google (`curl`), and the cluster (`kubectl`, in-cluster `haku` SA) through
`bash`. Tana is a native `mcp_toolset` (Anthropic-side, vault auth).

## k8s wiring

The `haku-worker` Deployment, its `haku-worker` ServiceAccount (bound to
`haku-sandbox-admin`), the `ANTHROPIC_ENVIRONMENT_KEY` secret stub, and the
clone/git env live in <../../../cluster/k8s/haku/agent-worker/README.md> —
shipped **suspended** (it's the first systemd-PID1 pod in the cluster and needs
an operator-generated environment key; that dir's README is the activation
runbook). The worker reuses Haku's `haku-sandbox` perimeter (`haku-sandbox-admin`
RBAC, `haku-mitmproxy` egress + CA injection, ResourceQuota); none of it relies
on agent restraint.

## Bring-up

```sh
./provision.sh                                   # org ANTHROPIC_API_KEY, outside the worker
# generate the environment key in the Console -> ANTHROPIC_ENVIRONMENT_KEY secret
# deploy haku-worker (env key + the HAKU_* clone/git env) in haku-sandbox
ant beta:deployments run --deployment-id "$DEPL_ID"   # test one run, watch in Console
```

## Worker env

`ANTHROPIC_ENVIRONMENT_ID`, `ANTHROPIC_ENVIRONMENT_KEY`, `HAKU_DUCKTAPE_REPO_URL`,
`HAKU_STATE_REPO_URL`, `HAKU_GIT_HOST`, `HAKU_GIT_USERNAME`, `HAKU_GIT_PASSWORD`.

Set these on the pod (`envFrom` a Secret/ConfigMap); systemd PID 1 lifts them
into the `haku-worker` unit via `ImportEnvironment=` (fallback: mount a Secret as
an env file at `/etc/haku-worker/env`). Set the pod `fsGroup` to the `haku` gid
so the `/workspace` emptyDir is writable, and layer the `haku-mitmproxy` CA into
`/etc/ssl/certs/ca-certificates.crt` so HTTPS through the proxy validates.

Beta-surface field/flag names (`agent_toolset_20260401`, `vault_ids`, the
deployment schema) follow the `ant` docs as of 2026-06 — verify with
`ant <cmd> --help`.
