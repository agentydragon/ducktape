# Agentplane acceptance suite

The default acceptance scenarios are **deployed vertical tests**: they create real sandboxes through
the app's HTTP API, open sessions on the real harnesses, use the deployed LLM, and assert on
cross-component behavior and durable system evidence. They are not unit tests with a live backend —
they are the checks that the deployed system does what <../egress/SPEC.md> says it does and that a
session's standing instructions reach the model that serves it.

Fast offline driver tests cover login choreography (`test_operator_login`), startup retries and
terminal-turn handling (`test_agent`). They create no Sandbox and do not prove that a deployed IdP,
Agentplane, or harness is reachable. Deployed login remains setup for the MCP vertical scenario.

`Agent.run` returns only for `TURN_STATUS_COMPLETED`. Any other terminal status fails immediately
with the turn id, status and quoted runner diagnostic, before scenario assertions about tool effects.
It does not dump prompts, tool output or native frames, or automatically resend the admitted input.

The general egress and instruction scenarios run on **both harnesses**. The runner protocol is the
same for Claude and Codex, so one parametrized harness case covers both; `model` asks the deployment
which models it offers for that harness rather than hardcoding one. `test_launch_presets` instead exercises the configured `public-coder` preset's
intentional Codex default, Sandbox binding, bootstrap marker, inherited fields, and local override.
Initial session Open uses a 120-second retry budget for HTTP 503, retaining the same session id;
refusals and other server failures are not startup readiness signals.

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

Verified 2026-10-15 in `agentplane-testing` against `runner-vm` image
`devel-20261015052552-c3949a5`: the fresh workspace was absent before Session open, then owned by
UID/GID 107 with successful `git init` and `git rev-parse` and no `safe.directory` setting. The
one-case run passed in 224 seconds, and fixture teardown removed its Sandbox and PVC. See the
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/aef53a32-d86e-4a86-ba26-40ae9dc97708).

## Suspend and resume

`//agentplane/acceptance:test_suspend_resume` tests two Threads (Claude and Codex) in one Pod Sandbox,
and a separate Codex Thread in a VM Sandbox. It suspends until the old launcher or Pod is gone,
then resumes the original Threads. Each case verifies the state PVC UID and bound PV survive,
the incarnation changes, the sealed archived history remains byte-for-byte unchanged, and one
`JournalReopened` continues its source sequence before a resumed native turn recalls its token.
The two-Thread case also checks token isolation. Intentional delta settlement may leave gaps in
archive cursors; retained entries remain ordered and unique.

Run only after the app, controller and runner changes have reached testing. Check the VM image's
embedded wheel release provenance: its runner must include Session setup reporting before testing
suspend, even when the container runner already has it. Run one VM case at a time within the
testing storage quota. To exercise Codex on the VM without repeating Pod or Claude acceptance:

```bash
bazelisk test //agentplane/acceptance:test_suspend_resume \
  --test_arg=-k --test_arg=test_vm_codex --test_output=streamed
```

Successful cases write PVC/PV identities, old/new incarnation UIDs, Session IDs and seal/reopen/
continuation cursors to the target's `test.outputs/{runner,runner-vm}-resume.json`. Checkpoints are
saved during the run, with `passed: false` until all assertions succeed, so partial evidence survives
a model refusal or later failure. The memory code is explicitly synthetic and non-secret. The scenario creates uniquely named
Sandboxes, and fixture teardown suspends and deletes only the Sandboxes that invocation created.
It does not delete an existing Sandbox. This completed-turn scenario does not cover a crash at a
command checkpoint or interrupted setup; deterministic coverage for those boundaries lives in
`//agentplane/runner:test_restart` and `//agentplane/runner:test_seal`.

## Dispatcher replica acceptance

On the authorized operator host, run the bounded testing-only replica case:

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=replicas --test_output=streamed
```

It queues a setting change in a fresh stopped Sandbox, temporarily scales the testing Dispatcher
from one replica to two, deletes the original owner's Pod with a UID precondition, and observes
Lease takeover through a watch. The retained command is then delivered after explicitly starting
the Sandbox. Finally, streams pinned to both replica Pods must receive the queued command's
terminal deletion outcome. No model turn or app HTTP API is used. Authentication uses a short-lived
token bound to an existing testing app Pod, so an app rollout during the probe revokes that token.

Only the fixture Sandbox is deleted. The original replica count is restored if the Deployment UID
and generation still match the temporary scale, preserving concurrent GitOps/operator changes.
Evidence is written to `test.outputs/dispatcher-deployed.json`; `passed` and `cleanup_complete`
are distinct. The `queue` case retains the original single-replica delivery check.

Testing config allows `agentplane-app` and `agentplane-notifications` to request wakes. The bounded
`opted_in` case passed against testing in #10541: concurrent app `OpenSession` and notification
`SubmitInput` shared one wake and both reached runner admission. Reuse that evidence while only
staging's caller allowlist changes; run this case again if the wake implementation or its caller
authorization changes. For a fresh testing acceptance, wait until the Dispatcher, migration and
Sandbox Controller images are Ready, then run:

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=opted_in --test_output=streamed
```

It creates one fresh stopped Sandbox with `wakeOnSend: true`, then concurrently submits an app
`OpenSession` and a notification `SubmitInput` using short-lived tokens bound to their existing Pods.
Both senders must share one `wakeGeneration`; the Dispatcher must wait for a new Ready incarnation
before admitting the Session and input. The existing `queue` case checks that an app send against
the default `wakeOnSend: false` stays queued with `wake_on_send_disabled` and does not mutate the
Sandbox. The test records the deployed image references and deletes only its own Sandbox. It does
not exercise staging. A staging allowlist does not change any existing Sandbox: send-triggered wake
still requires that Sandbox's `spec.wakeOnSend` to be explicitly true. The app's current create API
and form do not expose that field, so app-created Sandboxes remain opted out until the separate
create-form follow-up lands. The Dispatcher charges at most ten distinct wake generations per
authenticated caller per rolling minute.

## MCP integration

`//agentplane/acceptance:test_mcp` belongs to this deployed suite. Both real harnesses
receive the Action API URL and public workload placeholder, discover the `everything`
group's `echo` Action, submit a structured group/name request, poll until terminal,
and report JSON. The test checks the reported result against the fresh marker's exact
upstream echo output. It uses the existing sandbox setup/teardown and `Agent` fixtures.
Testing GitOps (`cluster/generated/agentplane-testing/`) wires the upstream image, ActionGroup
and discovery egress; nothing there auto-approves. `test_agent_executes_mcp_action` first binds
its Sandbox to an `exact_actions` set naming `everything/echo`, written through the Kubernetes
API as below. Run after the PR's images and manifests have rolled out; remote adapter tests are
not evidence that the real-agent deployed test has run.

`test_policy_binding_auto_approves_the_bound_sandbox` writes, with the caller's own kubeconfig,
a `PolicySet` whose `action_auto_approve` rule approves `everything/echo` with a string `message`
of at most 200 characters and a `PolicyBinding` naming the ServiceAccount of the Sandbox it
launched, then waits for the Action Service's `ActionsReady` condition on the binding. A matching
echo comes back auto-approved and executed, and the BFF receipt's Decision names the binding, the
set and the matching rule; an over-long message waits for the operator; after the binding is
patched to an expiry in the past, the Policy Controller deletes it and the BFF's caller grants stop
listing it, a match waits too. The objects are deleted at teardown; the pending requests are
denied through the BFF so nothing lingers. The role in
`../../cluster/generated/agentplane-testing/agentplane-testing.k8s.yaml`
grants create/get/patch/delete on the two kinds for this.

`test_agent_mcp_bff_decision` adds allow/deny cases on each harness: turn 1 submits
an echo from a Sandbox nothing binds, so it waits for the operator, and returns only
its UUID; Python inspects and decides through the app's `/actions/{id}` BFF; the
same Agent polls in turn 2 and returns strict JSON. Python independently checks
durable request/Decision/Execution snapshots, exact arguments/result, operator
identity, duplicate-decision idempotency and stale-version rejection. No canonical operator API fallback is used.

`test_operator_links_oauth_mcp_server` exercises the operator-managed MCP OAuth linkage flow
end to end against a real, deployed, OAuth-protected MCP server -- the `example` server, a
Dex-backed fixture with one `echo` tool, deployed the same way as `everything`. It starts the
linkage through the BFF, then follows Dex's ordinary local-password form in a separate fresh
browser. Dex 2.45.1 does not establish reusable SSO cookies for this password-connector path, so
the app session is never copied to Dex. The separate browser stops at the exact linkage callback;
the BFF then validates the unchanged state and completes its own callback/token boundary. The test
asserts the server reaches `linked` status and executes the protected tool through a real
Agentplane agent. Unlike the real GitHub/Kubernetes providers linked in staging, nothing here is
mocked or fabricated: this is the same code path an operator uses to link any OAuth MCP server,
run against a server this repo controls end to end.

These cases read only `public-coder-agent/agentplane-testing-acceptance-operator` via
`kubectl get --raw=/api/v1/namespaces/public-coder-agent/secrets/agentplane-testing-acceptance-operator`
using the existing kubeconfig and either the Haku Console Kubernetes proxy
(`https://haku-kubeapi.allegedly.works`) or a Sandbox's in-cluster API server. No operator environment variables or
pre-issued cookie are used. Missing proxy/RBAC/reflection or malformed Secret data
fails **BLOCKED**, without printing kubectl output or decoded values.

The Secret's `login` is the identifier submitted to the identity provider; `username`
is the expected operator name returned by the app. Dex's local password connector
uses the configured email for login, which is distinct from its display username.
Both the testing ESO definition and staging Terraform definition supply these fields.

A fresh `httpx` cookie jar starts at the app's `/auth/login`, follows the configured
OIDC provider's redirects and returns to the app's `/auth/callback`. The app owns OAuth
state, nonce, PKCE, token exchange and server-side session storage. Mutation requests use
the app's exact same-origin `Origin`. Authentik staging uses its pinned FlowExecutor
adapter and fails **BLOCKED** for MFA, consent or browser-only challenges. The testing
instance uses Dex's ordinary local password form; the test does not reproduce an
Authentik-specific web flow and still exercises the app's real authorization-code/session
boundary. The test never fabricates a cookie or inserts a session.

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

Run the other live scenarios by their explicit targets: `:test_launch_presets`,
`:test_instructions`, `:test_mcp`, and `:test_thread_latency`.

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
| `AGENTPLANE_ACCEPTANCE_IDP`             | `dex`                                        |
| `AGENTPLANE_OPERATOR_SECRET_PATH`       | testing acceptance Secret path               |

Staging remains available for ad hoc click-through tests by overriding the URL, namespace,
IDP, and operator Secret path to the Authentik deployment. The shipped default is Dex/testing.
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

`test_instructions` is the one scenario that cannot follow the rule: no part of the system records
that a system prompt arrived, so the model obeying the instruction is the only evidence there is.
Its answer to that is a marker token no model emits on its own plus a control session, opened with
no instructions on the same sandbox and given the same prompt, that must not produce it.
