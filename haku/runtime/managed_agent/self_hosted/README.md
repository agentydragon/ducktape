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

The **control plane is provisioned declaratively** by the `claude-managed-agents`
tofu provider — the environment, agent, vault + MCP credentials, and the scheduled
wake deployment all live in <../../../../tf/gitops/haku-self-hosted-agent/> and are
applied by Flux (Terraform CR in
<../../../../cluster/k8s/haku/self-hosted-agent-tf/>). Only the in-pod poll loop
(`worker.py`) and observability (`ant beta:deployments run`, `…:sessions:events`)
use `ant`; the `anthropic-cli` package stays in the devshell for those.

The **warm-session supervisor is deferred**: the wake trigger is a scheduled
deployment that fires a fresh session each tick; `haku-state` (git) is the
durable memory, so a cold session just re-orients.

## Pieces

| File            | Role                                                      | Runs on       |
| --------------- | --------------------------------------------------------- | ------------- |
| `entrypoint.sh` | clone ducktape + haku-state, then exec `haku-worker`      | `haku-worker` |
| `worker.py`     | the poll loop (anthropic Python SDK `EnvironmentWorker`)  | `haku-worker` |
| `nixos.nix`     | full-NixOS worker image (`nix build .#haku-worker-image`) | CI / build    |

The control-plane definition (environment / agent / vault / deployment) is **not**
here anymore — it's the tofu module <../../../../tf/gitops/haku-self-hosted-agent/>
(was the imperative `provision.sh` + `haku.{environment,agent,deployment}.yaml`,
retired when provisioning moved to TF; see git history if you need the old form).

## Trust split — keep the org key off the worker

- **Worker pod** (`haku-sandbox`): only `ANTHROPIC_ENVIRONMENT_KEY`
  (`sk-ant-oat01-…`, one environment's work queue). The worker authenticates
  every call with a Bearer sub-client derived from it; a prompt-injected tool
  call can't reach the control plane.
- **Provisioning** (the tofu runner): the org-scoped `ANTHROPIC_API_KEY`, injected
  into the tofu-controller runner pod (the shared `haku-cloud-anthropic-api-key`
  Secret) — **never** on the worker host. Deployment runs / session inspection use
  the same org key from CI / the operator laptop.

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
in-cluster `haku` SA) through `bash`. Tana and gmail-labeling are native
`mcp_toolset`s (Anthropic-side, vault auth) declared in the tofu agent.

## k8s wiring

The `haku-worker` Deployment, its `haku-worker` ServiceAccount (bound to
`haku-sandbox-admin`), the `ANTHROPIC_ENVIRONMENT_KEY` secret stub, and the
clone/git env live in <../../../../cluster/k8s/haku/agent-worker/README.md> (that
dir's README is the activation runbook). The worker reuses Haku's `haku-sandbox`
perimeter (`haku-sandbox-admin` RBAC, `haku-mitmproxy` egress + CA injection,
ResourceQuota); none of it relies on agent restraint.

## Provisioning + updating the control plane (now TF)

The environment / agent / vault / deployment are tofu resources
(<../../../../tf/gitops/haku-self-hosted-agent/main.tf>), applied by the Flux
Terraform CR — **not** `ant`. To change the agent (model, system pointer, tools,
MCP servers) or the wake schedule, edit `main.tf` and let Flux apply; the
provisioned IDs are published to the `haku-self-hosted-agent-ids` Secret
(flux-system). First-time stand-up + the recreate-fresh cutover from the old
imperative agent (regenerate the environment key, repoint the worker, delete the
orphaned imperative resources) is the runbook in
<../../../../cluster/k8s/haku/self-hosted-agent-tf/README.md>.

Test + observe a deployment (org `ANTHROPIC_API_KEY`, from CI / the operator
laptop — the `deployment_id` is in the IDs Secret):

```sh
ant beta:deployments run --deployment-id "$DEPL_ID" --transform '{id}'  # the run's session_id is in the response
ant beta:sessions:events list "$SID"                                    # transcript (tool_use vs tool_result)
ant beta:sessions delete --session-id "$SID"  # end a stuck/parked session; the worker force-stops the
                                              # work item and returns to polling (no pod restart needed)
```

### Reading a session transcript

`ant beta:sessions:events list <SID>` is the control-plane (org-key) transcript.
Use `--format jsonl` for one event per line so `jq` can parse it (the default
`auto` pretty-prints; it is **not** a JSON array):

```sh
ant beta:sessions:events list "$SID" --format jsonl > events.jsonl

# Conversation flow — the wake, the model's text, and every tool call:
jq -r 'select(.type=="user.message" or .type=="agent.message")
       | "[\(.type)] \([.content[]?|.text//empty]|join(" "))"' events.jsonl
jq -r 'select(.type|test("tool_use$")) | "TOOL \(.name)  \(.input|tojson[:160])"' events.jsonl

# Tool results + errors (is_error=true is a tool-level failure, not a worker bug):
jq -r 'select(.type|test("tool_result$")) | "\(.tool_use_id) is_error=\(.is_error)"' events.jsonl
```

Event types worth knowing: `user.message` (the wake), `agent.thinking`,
`agent.message` (model text), `agent.tool_use` / `agent.mcp_tool_use` (calls),
`user.tool_result` (results, keyed by `tool_use_id`), and
`session.status_idle` / `session.thread_status_idle` carrying a `stop_reason`.

**Diagnosing a stuck session:** a `*_tool_use` whose `id` has no matching
`user.tool_result.tool_use_id` is the pending one. If the session is idle with
`stop_reason.type == "requires_action"`, that tool is **awaiting approval** —
its toolset's `permission_policy` is `always_ask` (every toolset in the tofu
agent uses `always_allow` for unattended runs; that is the trust-boundary posture
here). Distinguish from the old empty-output **deadlock** (now fixed): that showed
a `tool_use` whose result never posted because the worker 400'd on empty text —
here results post fine (`200 OK` in the pod logs); the agent is just **waiting**.

The worker-side view is the pod logs (`kubectl logs deploy/haku-worker
-n haku-sandbox`): `executing tool tool=… tool_use_id=…` then `POST …/events
200` per result, `work/…/heartbeat` keeping the lease, and `session terminated`
→ `work/…/stop` when a session ends.

## Worker env

`ANTHROPIC_ENVIRONMENT_ID`, `ANTHROPIC_ENVIRONMENT_KEY`, `HAKU_DUCKTAPE_REPO_URL`,
`HAKU_STATE_REPO_URL`, `HAKU_GIT_HOST`, `HAKU_GIT_USERNAME`, `HAKU_GIT_PASSWORD`.

The pod runs the closure directly (no systemd), so the Deployment's `env` lands
straight in the entry process — plus the `HTTP(S)_PROXY`/`SSL_CERT_FILE` the
`haku-sandbox` Kyverno policy injects for mitmproxy egress (httpx honors both via
`trust_env`). Set the pod `fsGroup` to the `haku` gid so the `/workspace`
emptyDir is writable.

Beta-surface field/flag names (`agent_toolset_20260401`, `vault_ids`, the
deployment/environment schema) follow the `ant` docs and the `claude-managed-agents`
provider as of 2026-06 — verify with `ant <cmd> --help` and the first `tofu plan`.
