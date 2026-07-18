# Managed Agents — self-hosted sandbox (Runtime B)

Runtime B from <../../../plans/runtime_options.md>: Anthropic runs the agent loop;
tool execution runs in a worker **you** run in `haku-sandbox` (self-hosted
sandbox, `config.type: self_hosted`). The Anthropic-hosted-sandbox alternative is
the sibling <../anthropic_hosted/README.md>. Full design + tradeoffs:
<../../../plans/managed_agents.md> (+ <../../../plans/managed_agents_artifacts.md>).

The bring-up RCA — the fix chain, diagnostics, and open issues — is in
<debug/self_hosted_worker_bringup.md>.

## Why this is provisioned imperatively, not Terraform

Unlike the sibling **anthropic-hosted** agent — which is managed declaratively by
the `claude-managed-agents` OpenTofu provider in
<../../../../tf/gitops/haku-cloud-agent/> — this self-hosted agent is provisioned
**imperatively** with `ant` (`provision.sh`, `haku.{environment,agent,deployment}.yaml`).

The reason is a provider gap: `modus-agendi/anthropic-claude-managed-agents`
(v1.1.0, the latest release — and unreleased `main` as of 2026-07-03) has **no
`self_hosted` support**. Its `environment` resource makes `networking` a
**required** attribute and always sends `config.networking` to the API. That is
correct for `type = "cloud"` (where Anthropic runs the sandbox and enforces
egress), but the API's `config` is discriminated on `type`, and for
`type = "self_hosted"` it **rejects** `networking`:

```text
anthropic api: status=400 type=invalid_request_error
message=config.networking: Extra inputs are not permitted
```

For self-hosted, egress is _ours_ (the haku-managed-agent pod's `haku-egress-proxy` + CCNP),
so `{type: self_hosted}` is the entire config — no `networking`. The `ant` CLI
sends exactly that, so the imperative path works (it created the live env,
`env_015uqL9WAMSDytQEWWmLG9zF`); the provider cannot express it. We briefly
migrated this to TF (`tf/gitops/haku-self-hosted-agent`, PR #2673) and it never
applied for this reason, so it was reverted. Revisit only if the provider gains
`self_hosted` (make `networking` optional / discriminated by `type`); an upstream
issue is the path there. The **cloud** agent stays on TF — the provider handles
`type = "cloud"` fine.

Only the **environment / agent / deployment** are imperative. The **vault + all MCP
credentials are declarative and shared**: the cloud agent module
(`tf/gitops/haku-cloud-agent`) owns one vault used by both agents, publishing its ID
to the `haku-cloud-agent-ids` Secret; `provision.sh` reads that ID instead of creating
its own vault, so haku-console/kubectl-machine/grocy-sf tokens get TF
drift-detection + rotation for the self-hosted agent too. The agent's toolset is
identical to the cloud agent's — kept in step by
`//haku/base:test_agent_config_ssot`.

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

| File                    | Role                                                                                 | Runs on              |
| ----------------------- | ------------------------------------------------------------------------------------ | -------------------- |
| `haku.environment.yaml` | self-hosted environment (`ant beta:environments create`)                             | control plane        |
| `haku.agent.yaml`       | agent: thin `system` pointer, fixed toolset + 4 MCP `mcp_toolset`s (= cloud agent)   | control plane        |
| `haku.deployment.yaml`  | scheduled-deployment wake trigger                                                    | control plane        |
| `provision.sh`          | one-shot: create environment/agent/deployment via `ant` (vault is the shared TF one) | operator / CI        |
| `entrypoint.sh`         | clone ducktape + haku-state, then exec `haku-managed-agent`                          | `haku-managed-agent` |
| `worker.py`             | the poll loop (anthropic Python SDK `EnvironmentWorker`)                             | `haku-managed-agent` |
| `nixos.nix`             | full-NixOS worker image (`nix build .#haku-managed-agent-image`)                     | CI / build           |

## Trust split — keep the org key off the worker

- **Worker pod** (`haku-sandbox`): only `ANTHROPIC_ENVIRONMENT_KEY`
  (`sk-ant-oat01-…`, one environment's work queue). The worker authenticates
  every call with a Bearer sub-client derived from it; a prompt-injected tool
  call can't reach the control plane.
- **Provisioning** (`provision.sh`, deployment management, `…:work stats`): the
  org-scoped `ANTHROPIC_API_KEY`, run from CI / the operator laptop — **never**
  on the worker host.

## Worker image (`nix build .#haku-managed-agent-image`)

A full-NixOS rootfs (`nixos.nix`, declaratively consistent with the fleet)
carrying `bash`, `git`, `kubectl`, `postgresql` (`psql`), `curl`, `jq`, `cacert`,
`fastmcp`, `tea`, and `haku-managed-agent` (the pinned `python3` + `anthropic` 0.111 running
`worker.py`). We do **not** boot it: booting systemd PID 1 in an unprivileged
container can't mount the API filesystems, so the pod runs the closure
**directly** — k8s execs `/sw/bin/haku-managed-agent-run` (a wrapper that puts the tool
closure on PATH and execs `entrypoint.sh`) as the non-root `haku` uid with all
caps dropped. Build the uncompressed rootfs tarball with `nix build
.#haku-managed-agent-image`; CI imports it (`podman import`) and pushes to GHCR, pinned
by Flux — see <../../../../cluster/docs/container-images.md>.

`tea` is available in the image and logged in via the `haku-forgejo-tea` Secret
mounted at `/home/haku/.config/tea/config.yml`. The token is minted by
`forgejo-token-rotation` with the full privileges of the `haku` Forgejo account;
check it in a worker session with `tea whoami`.

The fixed toolset is `agent_toolset_20260401` (`bash/read/write/edit/glob/grep`);
Haku reaches Plaid (`psql`), Google (`curl`), and the cluster (`kubectl`,
in-cluster `haku` SA) through `bash`. On top of that it has three native
`mcp_toolset`s (Anthropic-side, shared-vault auth), identical to the cloud agent:
`haku-console` (the console's aggregated MCP catalog — Tana reads to start), `grocy-sf`
(read-only grocy), and `kubectl-machine` (a machine-JWT cluster path — redundant here
with in-pod `kubectl`, kept for parity).

## k8s wiring

The `haku-managed-agent` Deployment, its `haku-managed-agent` ServiceAccount (bound to
`haku-sandbox-admin`), the `ANTHROPIC_ENVIRONMENT_KEY` secret stub, and the
clone/git env live in <../../../../cluster/k8s/haku/managed-agent/README.md> (that
dir's README is the bring-up runbook). The worker reuses Haku's `haku-sandbox`
perimeter (`haku-sandbox-admin` RBAC, `haku-egress-proxy` egress + CA injection,
ResourceQuota); none of it relies on agent restraint.

## Bring-up

```sh
./provision.sh                                   # org ANTHROPIC_API_KEY, outside the worker
# generate the environment key in the Console -> ANTHROPIC_ENVIRONMENT_KEY secret
# deploy haku-managed-agent (env key + the HAKU_* clone/git env) in haku-sandbox
ant beta:deployments run --deployment-id "$DEPL_ID"   # test one run, watch in Console
```

## Updating the agent / deployment (control plane)

These are **control-plane** objects at Anthropic — `haku.{agent,environment,deployment}.yaml`
are version-controlled here but applied with `ant` (org `ANTHROPIC_API_KEY`),
**not** Flux. Editing the YAML alone changes nothing live. The two image-side
files (`worker.py`, `nixos.nix`, `entrypoint.sh`) are the only ones that flow
through CI + Flux. Live IDs: agent `agent_01CV5VupX8ALuVD1dsoEzHY6`, deployment
`depl_011DSrUoXuhoDWJoPyDuePqR` (haku-scan), environment
`env_015uqL9WAMSDytQEWWmLG9zF`.

The MCP **credentials** live in the shared TF vault (`tf/gitops/haku-cloud-agent`),
so enabling a new MCP on the live agent no longer means creating a credential here —
Flux applies it on the shared vault. You only push the new agent version (below) so
the `mcp_toolset` takes effect, and re-point the deployment at the shared vault once
(`ant beta:deployments update --deployment-id <id> --vault-id <shared-vault-id>`,
from the `haku-cloud-agent-ids` Secret).

After editing `haku.agent.yaml`, apply it and re-pin in **both** steps:

```sh
# 1. Push the new agent version. The YAML is the request body (stdin); --version
#    is the CURRENT version (optimistic-concurrency guard) — get it from retrieve.
cur=$(ant beta:agents retrieve --agent-id "$AGENT_ID" --transform version -r)
ant beta:agents update --agent-id "$AGENT_ID" --version "$cur" < haku.agent.yaml
#    -> returns the new version (e.g. 3)

# 2. RE-PIN the deployment. It pins a SPECIFIC agent version, so a fresh agent
#    version is ignored until you re-pin. A bare agent ID re-pins to latest:
ant beta:deployments update --deployment-id "$DEPL_ID" --agent "$AGENT_ID"
ant beta:deployments retrieve --deployment-id "$DEPL_ID" --transform '{agent}'  # confirm version bumped
```

`haku.deployment.yaml` (schedule, initial events) is likewise applied with
`ant beta:deployments update` (flags like `--schedule`, `--initial-event`).

Test + observe:

```sh
ant beta:deployments run --deployment-id "$DEPL_ID" --transform '{id}'  # the run's session_id is in the response
ant beta:sessions:events list "$SID"                                    # transcript (tool_use vs tool_result)
ant beta:sessions delete --session-id "$SID"  # end a stuck/parked session; the worker force-stops the
                                              # work item and returns to polling (no pod restart needed)
```

**Gotcha:** a deployment run uses the agent version the deployment pins _at run
time_, and a parked session keeps the version it was created with — so re-pin
**before** triggering a wake, and start a fresh session to pick up the change
(re-pinning doesn't migrate an in-flight session).

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
its toolset's `permission_policy` is `always_ask` (set it to `always_allow` in
`haku.agent.yaml` for unattended runs; that is the trust-boundary posture here).
Distinguish from the old empty-output **deadlock** (now fixed): that showed a
`tool_use` whose result never posted because the worker 400'd on empty text —
here results post fine (`200 OK` in the pod logs); the agent is just **waiting**.

The worker-side view is the pod logs (`kubectl logs deploy/haku-managed-agent
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
deployment schema) follow the `ant` docs as of 2026-06 — verify with
`ant <cmd> --help`.
