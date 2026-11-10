# Managed Agents — self-hosted sandbox (Runtime B)

> **Unwired.** No Flux registration, no CI image build/publish; the flake package remains for
> deliberate manual builds. Retained manifests and reactivation conditions:
> [`deploy/README.md`](deploy/README.md).

Runtime B: Anthropic runs the agent loop; tools execute
in our worker in `haku-sandbox` (`config.type: self_hosted`). Sibling:
<../anthropic_hosted/README.md>. Bring-up RCA:
<debug/self_hosted_worker_bringup.md>. Control-plane runbook, IDs, and transcript debugging:
<operations.md>.

## Provisioned with `ant`, not Terraform

`provision.sh` + `haku.{environment,agent,deployment}.yaml` via `ant`. The
`modus-agendi/anthropic-claude-managed-agents` provider (v1.1.0 / `main` as of 2026-07-03) always
sends `config.networking`, which the API rejects for `type = "self_hosted"`
(`config.networking: Extra inputs are not permitted`); egress here is ours (`haku-egress-proxy` +
CCNP). A TF migration (`tf/gitops/haku-self-hosted-agent`, PR #2673) never applied and was
reverted. Revisit only once the provider makes `networking` optional for `self_hosted`.

The former shared vault and MCP credentials were deleted 2026-09-30; the `haku-cloud-agent-ids`
Secret's vault ID is invalid. Reactivation needs a fresh vault passed as
`HAKU_MANAGED_AGENT_VAULT_ID` to `provision.sh`. Toolset parity with the historical cloud agent is
enforced by `//haku/runtime/x/managed_agent:test_agent_config_ssot`.

## Worker

`worker.py` uses the `anthropic` Python SDK's environment worker
(`client.beta.environments.work.worker`), not `ant beta:worker poll`: the Go SDK posts an empty
text block for empty tool output, which the API 400s, deadlocking the session
([anthropic-sdk-go#377](https://github.com/anthropics/anthropic-sdk-go/issues/377)); the Python
runner substitutes `"(no output)"`. The control plane still uses `ant` (`anthropic-cli` stays in
the devshell). The SDK (`anthropic` 0.111) comes from a `python3` override in `nixos.nix`;
`worker.py` is gazelle-excluded, not a Bazel target (see `BUILD.bazel`).

No warm-session supervisor: the scheduled deployment fires a fresh session each tick; `haku-state`
is the durable memory.

| File                    | Role                                                                    | Runs on              |
| ----------------------- | ----------------------------------------------------------------------- | -------------------- |
| `haku.environment.yaml` | self-hosted environment (`ant beta:environments create`)                | control plane        |
| `haku.agent.yaml`       | thin `system` pointer, fixed toolset + 2 `mcp_toolset`s (= cloud agent) | control plane        |
| `haku.deployment.yaml`  | scheduled-deployment wake trigger                                       | control plane        |
| `provision.sh`          | create environment/agent/deployment via `ant` (needs fresh vault ID)    | operator / CI        |
| `entrypoint.sh`         | clone ducktape + haku-state, exec `haku-managed-agent`                  | `haku-managed-agent` |
| `worker.py`             | poll loop                                                               | `haku-managed-agent` |
| `nixos.nix`             | full-NixOS worker system                                                | manual image build   |
| `image.nix`             | uncompressed rootfs tarball for `.#haku-managed-agent-image`            | manual image build   |

Image-side files (`worker.py`, `nixos.nix`, `image.nix`, `entrypoint.sh`) do not flow through
CI + Flux while parked; reactivation must choose an image publication/pinning path.

## Trust split

- **Worker pod**: only `ANTHROPIC_ENVIRONMENT_KEY` (`sk-ant-oat01-…`, one environment's work
  queue), used via a Bearer sub-client, so a prompt-injected tool call cannot reach the control
  plane.
- **Provisioning** (`provision.sh`, deployment management, `…:work stats`): org-scoped
  `ANTHROPIC_API_KEY` from CI / operator laptop — **never** on the worker host.

## Worker image (`nix build .#haku-managed-agent-image`)

Full-NixOS rootfs carrying `bash`, `git`, `kubectl`, `psql`, `curl`, `jq`, `cacert`, `fastmcp`,
`tea`, and `haku-managed-agent`. Not booted (systemd PID 1 cannot mount API filesystems
unprivileged): k8s execs `/sw/bin/haku-managed-agent-run` (puts the tool closure on PATH, execs
`entrypoint.sh`) as non-root `haku` with all caps dropped. Pod `fsGroup` must be the `haku` gid so
the `/workspace` emptyDir is writable.

`tea` is logged in via the `haku-forgejo-tea` Secret at `/home/haku/.config/tea/config.yml`
(token from `forgejo-token-rotation`, full `haku` Forgejo account privileges; check with
`tea whoami`).

Tools: fixed `agent_toolset_20260401` (`bash/read/write/edit/glob/grep`) — Plaid via `psql`,
Google via `curl`, cluster via `kubectl` (in-cluster `haku` SA). Plus two native `mcp_toolset`s
with Anthropic-side vault auth: `haku-console` (`haku.allegedly.works/mcp`, an endpoint the
console has since retired) and `kubectl-machine` (machine-JWT; redundant with in-pod `kubectl`,
kept for parity).

## k8s wiring and env

Deployment, `haku-managed-agent` ServiceAccount (bound to `haku-sandbox-admin`), env-key secret,
and clone/git env: <deploy/README.md>. Reuses the `haku-sandbox` perimeter (RBAC,
`haku-egress-proxy` egress + CA injection, ResourceQuota); nothing relies on agent restraint.

Env: `ANTHROPIC_ENVIRONMENT_ID`, `ANTHROPIC_ENVIRONMENT_KEY`, `HAKU_DUCKTAPE_REPO_URL`,
`HAKU_STATE_REPO_URL`, `HAKU_GIT_HOST`, `HAKU_GIT_USERNAME`, `HAKU_GIT_PASSWORD`;
`ANTHROPIC_WORKDIR` unset (defaults to `/workspace`). Without systemd, Deployment `env` reaches the
entry process directly, alongside the Kyverno-injected `HTTP(S)_PROXY` / `SSL_CERT_FILE` (httpx
honors both via `trust_env`).
