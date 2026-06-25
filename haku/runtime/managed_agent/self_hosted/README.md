# Managed Agents — self-hosted sandbox (Runtime B)

Runtime B from <../../../plans/runtime_options.md>: Anthropic runs the agent loop;
tool execution runs in a worker **you** run in `haku-sandbox` (self-hosted
sandbox, `config.type: self_hosted`). The Anthropic-hosted-sandbox alternative is
the sibling <../anthropic_hosted/README.md>. Full design + tradeoffs:
<../../../plans/managed_agents.md> (+ <../../../plans/managed_agents_artifacts.md>).

The bring-up RCA — the fix chain, diagnostics, and open issues — is in
<debug/self_hosted_worker_bringup.md>.

## The worker is `worker.py` on the anthropic Python SDK (not `ant`)

The poll loop is `worker.py`, built on the official `anthropic` Python SDK's
`EnvironmentWorker`. It replaced `ant beta:worker poll` (the Go CLI) because the
Go SDK's session tool runner posts an **empty text block** for empty tool output,
which the API 400s — deadlocking the session
([anthropic-sdk-go#377](https://github.com/anthropics/anthropic-sdk-go/issues/377)).
The Python session runner guards that exact case (`"(no output)"`), so the
Python worker sidesteps the deadlock. The Go-vs-Python source evidence is in the
RCA.

**Why the SDK is pinned in `nixos.nix`, not the shared Bazel lockfile:** the
worker lib (`anthropic.lib.environments`) needs `anthropic>=0.111`, but
`agent-framework-anthropic` (used by `haku/runtime/agent`) hard-pins
`anthropic<0.80.1` on every release to date — an irreconcilable lockfile
conflict. So the worker closure gets `anthropic` 0.111 via a `python3` override
in `nixos.nix`, and `worker.py` is a baked script excluded from Bazel (see the
local `BUILD.bazel`) — independent of the repo lockfile, Runtime C untouched.

The **control plane** still uses `ant` (`provision.sh`,
`ant beta:deployments run`, `…:work stats`) — only the in-pod poll loop moved to
Python. The `anthropic-cli` package stays in the devshell.

The **warm-session supervisor is deferred**: the wake trigger is a scheduled
deployment that fires a fresh session each tick; `haku-state` (git) is the
durable memory, so a cold session just re-orients.

## Pieces

| File                    | Role                                                             | Runs on       |
| ----------------------- | ---------------------------------------------------------------- | ------------- |
| `haku.environment.yaml` | self-hosted environment (`ant beta:environments create`)         | control plane |
| `haku.agent.yaml`       | agent: thin `system` pointer, fixed toolset + tana `mcp_toolset` | control plane |
| `haku.deployment.yaml`  | scheduled-deployment wake trigger                                | control plane |
| `provision.sh`          | one-shot: create environment/agent/vault/deployment via `ant`    | operator / CI |
| `entrypoint.sh`         | clone ducktape + haku-state, then exec `haku-worker`             | `haku-worker` |
| `worker.py`             | the poll loop (anthropic Python SDK `EnvironmentWorker`)         | `haku-worker` |
| `nixos.nix`             | full-NixOS worker image (`nix build .#haku-worker-image`)        | CI / build    |

## Trust split — keep the org key off the worker

- **Worker pod** (`haku-sandbox`): only `ANTHROPIC_ENVIRONMENT_KEY`
  (`sk-ant-oat01-…`, one environment's work queue). The worker authenticates
  every call with a Bearer sub-client derived from it; a prompt-injected tool
  call can't reach the control plane.
- **Provisioning** (`provision.sh`, deployment management, `…:work stats`): the
  org-scoped `ANTHROPIC_API_KEY`, run from CI / the operator laptop — **never**
  on the worker host.

## Worker image (`nix build .#haku-worker-image`)

A full-NixOS rootfs (`nixos.nix`, declaratively consistent with the fleet)
carrying `bash`, `git`, `kubectl`, `postgresql` (`psql`), `curl`, `jq`, `cacert`,
`fastmcp`, and `haku-worker` (the pinned `python3` + `anthropic` 0.111 running
`worker.py`). We do **not** boot it: booting systemd PID 1 in an unprivileged
container can't mount the API filesystems, so the pod runs the closure
**directly** — k8s execs `/sw/bin/haku-worker-run` (a wrapper that puts the tool
closure on PATH and execs `entrypoint.sh`) as the non-root `haku` uid with all
caps dropped. Build the uncompressed rootfs tarball with `nix build
.#haku-worker-image`; CI imports it (`podman import`) and pushes to GHCR, pinned
by Flux — see <../../../../cluster/docs/container-images.md>.

The fixed toolset is `agent_toolset_20260401` (`bash/read/write/edit/glob/grep`);
Haku reaches Plaid (`psql`), Google (`curl`), and the cluster (`kubectl`,
in-cluster `haku` SA) through `bash`. Tana is a native `mcp_toolset`
(Anthropic-side, vault auth).

## k8s wiring

The `haku-worker` Deployment, its `haku-worker` ServiceAccount (bound to
`haku-sandbox-admin`), the `ANTHROPIC_ENVIRONMENT_KEY` secret stub, and the
clone/git env live in <../../../../cluster/k8s/haku/agent-worker/README.md> (that
dir's README is the activation runbook). The worker reuses Haku's `haku-sandbox`
perimeter (`haku-sandbox-admin` RBAC, `haku-mitmproxy` egress + CA injection,
ResourceQuota); none of it relies on agent restraint.

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

The pod runs the closure directly (no systemd), so the Deployment's `env` lands
straight in the entry process — plus the `HTTP(S)_PROXY`/`SSL_CERT_FILE` the
`haku-sandbox` Kyverno policy injects for mitmproxy egress (httpx honors both via
`trust_env`). Set the pod `fsGroup` to the `haku` gid so the `/workspace`
emptyDir is writable.

Beta-surface field/flag names (`agent_toolset_20260401`, `vault_ids`, the
deployment schema) follow the `ant` docs as of 2026-06 — verify with
`ant <cmd> --help`.
