# Agentplane acceptance suite

The default acceptance scenarios are **deployed vertical tests**: they create real sandboxes through
the app's HTTP API, open sessions on the real harnesses, use the deployed LLM, and assert on
cross-component behavior and durable system evidence. They are not unit tests with a live backend —
they are the checks that the deployed system does what <../egress/SPEC.md> says it does and that a
session's standing instructions reach the model that serves it.

The deployment under test is whatever the environment names: `AGENTPLANE_ACCEPTANCE_{URL,TOKEN,NAMESPACE,SERVICE_ACCOUNT}`
and the caller's kubeconfig, testing by default. A scenario declared with
`acceptance_test(k3s = ...)` ([`defs.bzl`](defs.bzl)) also gets a `<scenario>_k3s` target: one
RBE action that starts its own [k3s stack](../x/k3s_env/README.md) and publishes it through the same
variables ([`acceptance_target.py`](../x/k3s_env/acceptance_target.py)), so it needs no cluster
credentials. That stack runs no VM Sandboxes; it deselects cases marked `vm_sandboxes`.

The model backend is the target's too (`AGENTPLANE_ACCEPTANCE_MODEL_BACKEND`). A deployment serves a
real model. On the k3s stack, LLM ingress forwards to the scenario itself, which plays the model
([`scripted_upstream.py`](scripted_upstream.py)): the `scripted` fixture hands a scenario the
`ScriptedModel` of its own Sandbox's Pod, and the scenario answers each request and asserts what
the harness sent. A case marked `real_model` or `scripted_model` runs only where that backend is;
only cases whose evidence is what a model does are `real_model`. The scripted stack serves Codex no
model catalog, so Codex calls the function tools the scripted model answers, as in the runner tests.
Every request the scripted model receives is kept in the target's `test.outputs/scripted_upstream/`.

```bash
bbr test //agentplane/acceptance:test_dispatcher_deployed_k3s //agentplane/acceptance:test_storage_deployed_k3s //agentplane/acceptance:test_session_instructions_k3s
```

Fast offline driver tests cover login choreography (`test_operator_login`), startup retries and
terminal-turn handling (`test_agent`). They create no Sandbox and do not prove that a deployed IdP,
Agentplane, or harness is reachable. Operator login is setup for the MCP scenario.

`Agent.run` returns only for `TURN_STATUS_COMPLETED`. Any other terminal status fails immediately
with the turn id, status and quoted runner diagnostic, before scenario assertions about tool effects.
It does not dump prompts, tool output or native frames, or automatically resend the admitted input.

The general egress and instruction scenarios run on **both harnesses**. The runner protocol is the
same for Claude and Codex, so one parametrized harness case covers both; `offered_model` asks the deployment
which models it offers for that harness rather than hardcoding one. `test_launch_presets_k3s` instead exercises a Codex launch preset's
defaults, Sandbox binding, bootstrap marker, inherited fields, and local override, with the scripted
model only. The k3s fixture configures that preset (`launch_preset.py`) in place of the deployment's.
Initial session Open uses a 120-second retry budget for HTTP 503, retaining the same session id;
refusals and other server failures are not startup readiness signals.

## Thread stream

`test_thread_stream` is a browser's view of a Thread with the scripted model: every tab follows one
archived log by event id, a reconnect resumes after its `Last-Event-ID`, a command id answers only
the command it first named, and turns nobody watches are archived, a failed one included, with the
next input its own turn. Its scripted cases run as `test_thread_stream_k3s`.

## Thread latency

`test_thread_latency` times a one-word turn at low reasoning effort and holds what agentplane adds
to the harness's own reported turn to half a second, then times the browser's reads to open that
thread, per stage, cold and warm, against regression ceilings; its module docstring defines each
interval. Each run writes its timings to
`bazel-testlogs/agentplane/acceptance/test_thread_latency/test.outputs/`.

## VM workspace ownership

`//agentplane/acceptance:test_vm_workspace_ownership` creates one uniquely named testing VM
Sandbox and opens two Codex Sessions without sending a model turn. The first setup checks that the
second Session's workspace path is absent; the second exercises normal runner setup, checks UID/GID
107 ownership, then runs `git init` and `git rev-parse` with global and system Git config disabled.
Fixture teardown suspends and deletes only the Sandbox created by that invocation. Run it after the
testing `runner-vm` template points at an image built with the pinned ownership-fix wheel:

```bash
bazelisk test //agentplane/acceptance:test_vm_workspace_ownership --test_output=streamed
```

The result is written to
`bazel-testlogs/agentplane/acceptance/test_vm_workspace_ownership/test.outputs/vm-workspace-ownership.json`.

## Workspace storage layout acceptance

`//agentplane/acceptance:test_storage_deployed` is a model-free fixture for the
built-in Pod `DirectoryTree` and VM `RawDiskImage`/ext4 workspace paths. It checks Sandbox-owned
PVC identity and layout records, marker bytes across suspend/resume, refusal of an incompatible
claim before workload start, and retained data after VM replacement. It creates only its own
Sandbox, template and workload, then removes them through UID-preconditioned lifecycle operations.
It refuses a deployment that already has Sandboxes, so on testing it runs only while no one else
uses it; `test_storage_deployed_k3s` runs the Pod case on a stack of its own.

```bash
bazelisk test //agentplane/acceptance:test_storage_deployed --test_output=streamed
```

The target writes `test.outputs/storage-{pod-directory,vm-raw}.json`. The deployed target is
`manual` and `no-remote-exec`; general controlled-host setup is below.

## Suspend and resume

`//agentplane/acceptance:test_suspend_resume` tests two Threads (Claude and Codex) in one Pod Sandbox
with the scripted model, and a separate Codex Thread in a VM Sandbox with a real one. It suspends
until the old launcher or Pod is gone, then resumes the original Threads by sending their next input.
Each case verifies all Sandbox-owned state and workspace PVC UIDs and bound PVs survive, the
incarnation changes, the sealed archived history remains byte-for-byte unchanged, and one
`JournalReopened` continues its source sequence before the resumed native turn. It also checks that
Open reserves a Session without a harness process, that a completed turn has a ready process
assignment, and that the new runner retains dormant Sessions and their native IDs before the next
input. While the Sandbox is suspended, an exact retry of each seed input is answered from the archive. The
resumed Pod Threads must send the model their own seeded token and not the other's; the
VM Thread's model must recall its token. Intentional delta settlement may leave gaps in archive
cursors; retained entries remain ordered and unique.

`test_suspend_resume_k3s` runs the Pod case on a stack of its own. The deployed target runs the VM
case: run it only after the app, controller and runner changes have reached testing. Check the VM
image's embedded wheel release provenance: its runner must include Session setup reporting before
testing suspend, even when the container runner already has it. Run one VM case at a time within the
testing storage quota.

```bash
bazelisk test //agentplane/acceptance:test_suspend_resume --test_output=streamed
```

Successful cases write PVC/PV identities, old/new incarnation UIDs, Session IDs and seal/reopen/
continuation cursors to the target's `test.outputs/{runner,runner-vm}-resume.json`. Checkpoints are
saved during the run, with `passed: false` until all assertions succeed, so partial evidence survives
a model refusal or later failure. The memory code is explicitly synthetic and non-secret. The scenario creates uniquely named
Sandboxes, and fixture teardown suspends and deletes only the Sandboxes that invocation created.
It does not delete an existing Sandbox. This completed-turn scenario does not cover a crash at a
command checkpoint or interrupted setup; deterministic coverage for those boundaries lives in
`//agentplane/runner:test_restart` and `//agentplane/runner:test_seal`.

## Dispatcher queue and replica acceptance

These probes target the Runner Gateway. The Dispatcher
Service remains `session-dispatcher:8080`; replica operations target the `runner-gateway`
Deployment, and direct Pod probes use the shared gRPC listener on `8080`.

The `queue` case checks authenticated submission to a stopped Sandbox, durable queueing, and
runner admission after an explicit start. It uses a fixture Sandbox and the testing app's workload
identity for TokenReview; it does not call app HTTP or run a model turn. Run it on the authorized
operator host or from a `claude-ai` Sandbox (the
[testing operator Role](../../cluster/docs/agent_rbac.md#testing-only-agentplane-operator-access) holds the grants it needs):

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=queue --test_output=streamed
```

To cover replica takeover and deletion broadcast, run the bounded testing-only replica case:

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=replicas --test_output=streamed
```

It queues a setting change in a fresh stopped Sandbox, temporarily scales the testing Runner Gateway
from one replica to two, deletes the original owner's Pod with a UID precondition, and observes
Lease takeover through a watch. The retained command is then delivered after explicitly starting
the Sandbox. Finally, streams pinned to both replica Pods must receive the queued command's
terminal deletion outcome. No model turn or app HTTP API is used. Authentication uses a short-lived
token bound to an existing testing app Pod, so an app rollout during the probe revokes that token.

The fixture Sandbox is deleted by UID; the failover case deletes only the recorded owner Pod. The
original replica count is restored if the Deployment UID and generation still match the temporary
scale, preserving concurrent GitOps/operator changes. Evidence is written to
`test.outputs/dispatcher-deployed.json`; `passed` and `cleanup_complete` are distinct. The `queue`
case retains the original single-replica delivery check.

The `opted_in` case covers send-triggered wake for both app `OpenSession` and notification
`SubmitInput`. Run it after the Goose migration completes and the Runner Gateway, Sandbox Controller, app
and notification images are Ready and the caller allowlist matches the intended identities:

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=opted_in --test_output=streamed
```

It creates one stopped Sandbox with `wakeOnSend: true`, then concurrently submits an app
`OpenSession` and a notification `SubmitInput` using short-lived tokens bound to existing Pods. Both
senders must share one `wakeGeneration`; the Dispatcher waits for a new Ready incarnation before
admitting the Session and input. The `queue` case covers the default `wakeOnSend: false`, which
leaves a send queued without changing Sandbox state. New Sandboxes default to false; the app's
create and edit controls let the operator opt in explicitly, and changing the setting alone does
not wake a Sandbox. These cases exercise testing, not staging. The Dispatcher charges at most ten
distinct wake generations per authenticated caller per rolling minute.

## MCP integration

`test_mcp_k3s` runs on a stack with Dex, `mcp-everything` and the Dex-backed OAuth MCP fixture
(`acceptance_test(mcp_fixtures = True)`), configured under test-only names
([`mcp_servers.py`](mcp_servers.py)). The scripted model runs one `agentplane --json actions …`
command per turn, on each harness, and the scenario reads the printed receipt back from what the
harness sent the model.

- `test_agent_executes_mcp_action` binds its Sandbox to an `exact_actions` set naming the
  `mcp-everything` echo, submits it with `--wait` and checks the exact upstream echo result.
- `test_policy_binding_auto_approves_the_bound_sandbox` writes a `PolicySet` approving that echo
  with a `message` of at most 200 characters and a `PolicyBinding` naming the Sandbox's
  ServiceAccount, then waits for the binding's `ActionsReady`. A match comes back auto-approved, and
  the BFF receipt's Decision names the binding, the set and the rule; an over-long message waits for
  the operator; once the expired binding is deleted, a match waits too. Pending requests are denied
  through the BFF.
- `test_agent_mcp_bff_decision` submits from an unbound Sandbox, decides allow or deny through the
  app's `/actions/{id}` BFF, and has the agent `wait` for the outcome. It checks the
  request/Decision/Execution snapshots, the operator's identity, duplicate-decision idempotency and
  stale-version rejection.
- `test_operator_links_oauth_mcp_server` links the OAuth fixture through the BFF, following Dex's
  local-password form in a separate fresh browser that stops at the linkage callback; the BFF then
  validates the unchanged state and completes its own token exchange. The agent then calls the
  protected tool.

These cases read only the operator Secret the k3s target publishes (`AGENTPLANE_OPERATOR_SECRET_PATH`)
via `kubectl get --raw`, with a kubeconfig naming the Haku Console Kubernetes proxy
(`https://haku-kubeapi.allegedly.works`), a Sandbox's in-cluster API server or the k3s fixture's.
No pre-issued cookie is used. Missing proxy/RBAC/reflection or malformed Secret data fails
**BLOCKED**, without printing kubectl output or decoded values.

The Secret's `login` is the identifier submitted to the identity provider; `username`
is the expected operator name returned by the app. Dex's local password connector
uses the configured email for login, which is distinct from its display username.
The k3s fixture chart writes these fields beside Dex's config.

A fresh `httpx` cookie jar starts at the app's `/auth/login`, follows the configured
OIDC provider's redirects and returns to the app's `/auth/callback`. The app owns OAuth
state, nonce, PKCE, token exchange and server-side session storage. Mutation requests use
the app's exact same-origin `Origin`. Authentik staging uses its pinned FlowExecutor
adapter and fails **BLOCKED** for MFA, consent or browser-only challenges. The testing
instance uses Dex's ordinary local password form; the test does not reproduce an
Authentik-specific web flow and still exercises the app's real authorization-code/session
boundary. The test never fabricates a cookie or inserts a session. On k3s the app (over http) and
Dex (over TLS from a CA the stack makes and the test trusts) serve on their in-cluster Service names,
which the stack resolves for the test process; `operator_login` allows http only to such names.

Credentials stay in process memory. HTTP logging is suppressed for the BFF client's
lifetime; pytest local-variable dumps are refused before Secret access. Do not add
HTTP tracing, response dumps or credential artifacts. The existing suite URL/token
settings below still serve the separate workload client. The agent-pod runner
restriction below still applies: remote choreography tests are not live acceptance.

After login, a fresh absent-Action lookup checks federation without listing existing
requests. Its `404` must carry the BFF's structured upstream error for that exact
operator Action request path, not an arbitrary ingress, signing-key, or login error.
Malformed or unexpected responses fail without exposing their contents.

## Running it

Not in CI, and not on RBE: the target is `manual`, so `//...` never selects it, and it needs a
kubeconfig and a route to the cluster.

Bazel excludes `manual` targets from package patterns, so `:all` only runs the fast
offline driver tests. It does not run any deployed vertical scenario. Name the live
scenario explicitly. Start with the egress vertical slice:

```bash
bazelisk test //agentplane/acceptance:test_egress --test_output=streamed --test_arg=-s
```

That target runs the egress probe, whose evidence is what a real model works out. `test_egress_k3s`
runs the other egress cases with the scripted model: the model call goes through the proxy, and a
PolicySet bound to a running Sandbox takes effect. The latter binds the fixture's own PolicySet
([`runtime_grant.py`](runtime_grant.py)), not one a deployment's Sandboxes use.

Run the other live scenarios by their explicit targets: `:test_session_instructions` and
`:test_thread_latency`.

`//agentplane/acceptance:test_ollama_routes` exercises every configured Ollama chat
route offered by the committed testing app ConfigMap, on each harness that offers it,
with the 128k cases first. Collection reads the generated manifest, not cdk8s Python;
each case still checks that the live app offers the route before opening a session. Each cell creates a Sandbox,
opens a real session, and requires recorded shell-tool output. It has an absolute
300-second turn limit; a backend that never completes still leaves an interrupted
turn rather than a model verdict. To run just one cell, set `OLLAMA_SMOKE_CASE` to
its pytest id, for example
`harness_codex-ollama-oai-chat-gpt-oss-20b-128k`. Each cell writes a small JSON
result under the target's `test.outputs/` directory. A Bazel rerun of this target
replaces local test outputs, so save any evidence needed across runs first.
Results include context windows reported by the native harness (Codex token-usage
notifications and Claude result model usage). These can differ from server context:
for example, Codex may report its usable budget after reserving headroom. An empty
list means the harness did not report a window in the observed turn.
For transient sidecar diagnosis, `--test_env=OLLAMA_SMOKE_HOLD_SECONDS=30` retains
the isolated Sandbox for at most 30 seconds after the turn result is written, before
normal fixture cleanup.

By default it tests `https://agentplane-testing.allegedly.works` and mints its own bearer token with
`kubectl -n agentplane-testing create token agentplane-agent --audience=agentplane`. That call needs
RBAC on `serviceaccounts/token`, and the app only admits subjects its `AGENTPLANE_TOKEN_SUBJECTS`
names, so a token for any other ServiceAccount is refused with `403`.

Override any of it through the environment:

| Variable                                | Default                                      |
| --------------------------------------- | -------------------------------------------- |
| `AGENTPLANE_ACCEPTANCE_URL`             | `https://agentplane-testing.allegedly.works` |
| `AGENTPLANE_ACCEPTANCE_TOKEN`           | minted with `kubectl`                        |
| `AGENTPLANE_ACCEPTANCE_NAMESPACE`       | `agentplane-testing`                         |
| `AGENTPLANE_ACCEPTANCE_SERVICE_ACCOUNT` | `agentplane-agent`                           |

Staging remains available for ad hoc click-through tests by overriding the URL and namespace.
The testing environment is Flux-managed and exposes the app and Dex HTTPS routes needed for the
browser authorization-code flow plus the Action Service's `/mcp` and OAuth surface at
`agentplane-actions-testing.allegedly.works`; the `mcp-everything` fixture itself is
cluster-internal. Do not copy credentials into the checkout or pass them as command-line arguments.

### Controlled-host preflight

Run the suite from a controlled NixOS host, devbox/VM, or FHS-compatible agent
container. Do not use `bbr`/`bb remote` for this suite: the completed test
process must stay on the caller, where it can use the caller's Kubernetes
credentials and network path. BuildBuddy-hosted runners do not inherit the
caller Sandbox workload token or the current egress substitution path.

Before starting a long run, check the client-side seams separately:

```bash
command -v bazelisk kubectl
bazelisk version
kubectl config current-context
kubectl -n agentplane-testing auth can-i create serviceaccounts/token/agentplane-agent
kubectl -n agentplane-testing create token agentplane-agent \
  --audience=agentplane --duration=600s >/dev/null
```

The preflight must not print or save the returned token. If the token command
fails, fix caller RBAC or kubeconfig before launching the suite. If Bazel
fails while loading the module graph, fix the caller's Bazel/repository-rule
runtime before investigating staging.

### Failure classification

Use the first point at which the run fails to choose the next investigation:

| Observation                                                   | Likely seam                                                                  |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| No `accept-*` Sandbox is created                              | Bazel client, module/repository rules, kubeconfig, or acceptance-token setup |
| Sandbox is created but never becomes ready                    | Scheduling, image pull, runner bootstrap, or testing capacity                |
| App rejects the initial API request                           | Acceptance token audience, subject allowlist, or app ingress                 |
| Model turn hangs and the decision history is empty            | Sandbox proxy environment, proxy route, or model ingress path                |
| Ring records a deny for an expected destination               | Egress policy/binding or destination URL mismatch                            |
| Rules discovery succeeds but destination authentication fails | Placeholder substitution or independent destination authentication           |
| Test assertions pass but teardown reports a failure           | Runtime cleanup/reconciliation; inspect the named Sandbox before rerunning   |
| Process is killed and `accept-*` Sandboxes remain             | Expected teardown limitation; clean them up deliberately before the next run |

Keep the complete test output and the proxy/app decision evidence together.
The model transcript explains what the agent attempted, but the decision history
is the authority for what the proxy actually served.

### Where an agent can run it

Agent pods must use `bbr`/CI, never local Bazel or pytest. This deployed suite is
`manual` / `no-remote-exec` and lacks an approved CI runner with staging identity and
connectivity. Therefore it is **blocked from agent pods**; do not disable remote
execution/caching or mint substitute credentials to work around that boundary.
The controlled-host instructions above are operator-only, not an agent-pod fallback.
See [repository instructions](../../AGENTS.md).

Teardown subscribes to `/live/sandboxes` until a fresh watch snapshot reports effective `Suspended`
mode for the created Sandbox's name and UID, within the existing readiness bound. The spec write
alone does not satisfy deletion admission. Authorization refusals fail immediately; teardown
does not poll the inventory, reconnect a dropped stream, or delete a replacement Sandbox.
Afterwards, check that nothing leaked: `kubectl -n agentplane-testing get sandboxes.agentplane.allegedly.works`
should show no `accept-*`.

## What it costs

Each scenario provisions a Pod and runs turns on the cheap-experiments LiteLLM key with Haiku, so a
full run is minutes and a few cents. Sandboxes are suspended and deleted in fixture teardown,
including after a failure; a teardown that cannot delete one fails loudly, because a leaked sandbox
holds a PVC and a node slot in testing.

## TODO: sweep sandboxes a killed run leaks

Fixture teardown suspends and deletes every sandbox a scenario created, including after a failure.
It cannot run if the process is killed outright — a Bazel timeout, a `^C`, a dropped connection —
and each leaked sandbox holds a PVC and a node slot in testing until someone notices.

What that wants is a sweep at session start: list the sandboxes whose names carry this suite's
`accept-` stem, and delete any older than a run could plausibly be. Deliberately not built yet,
because the stem is the only marker and a real sandbox someone named `accept-something` would be
destroyed by it — a label the app sets on suite-created sandboxes, or a dedicated namespace, is the
thing to add first.

## Why it asserts on the ring, not on the agent

The agent's own account of a tool call is prose. "I fetched the repository" is equally consistent
with a request the proxy admitted, a request that never reached the proxy, and a model that did not
run the command at all. The proxy's decision history is the system's record of what it admitted,
so that is what a scenario checks; the turn's output is carried into the failure message, where it
explains a failure rather than deciding one.

Where a scenario has to look _inside_ the sandbox, it never asks the model to print a secret. A
model that declines to echo a credential, or redacts it, produces output with no credential in it —
which would satisfy an "is it absent" assertion for entirely the wrong reason. The command prints a
verdict token instead, and the scenario fails unless one of the two tokens actually comes back, so a
refusal reads as a failure rather than as an absence.

This suite exists because the last gap of that shape — a runner that dropped the sandbox's proxy
variables, so every call bypassed the proxy and hung with an empty history — sat behind a fully green
unit suite until someone drove the deployed app by hand.

The real-model case of `test_session_instructions` is the one scenario that cannot follow the rule:
whether a model obeys the instruction is what it tests, so the model's reply is its evidence. Its
answer to that is a marker token no model emits on its own plus a control session, opened with no
instructions on the same sandbox and given the same prompt, that must not produce it. Its scripted
case asserts what each session sends the model instead.
