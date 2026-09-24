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

## MCP integration

`//agentplane/acceptance:test_mcp` belongs to this deployed suite. Both real harnesses
receive the Action API URL and public workload placeholder, discover the `everything`
group's `echo` Action, submit a structured group/name request, poll until terminal,
and report JSON. The test checks the reported result against the fresh marker's exact
upstream echo output. It uses the existing sandbox setup/teardown and `Agent` fixtures.
Testing GitOps (`cluster/k8s/agentplane-testing/`) wires the upstream image, ActionGroup
and discovery egress; nothing there auto-approves. `test_agent_executes_mcp_action` first binds
its Sandbox to an `exact_actions` set naming `everything/echo`, written through the Kubernetes
API as below. Run after the PR's images and manifests have rolled out; remote adapter tests are
not evidence that the real-agent deployed test has run.

`test_policy_binding_auto_approves_the_bound_sandbox` writes, with the caller's own kubeconfig,
an `ActionPolicySet` that auto-approves `everything/echo` with a string `message` of at most 200
characters and an `ActionPolicyBinding` naming the Sandbox it launched by name and UID, then
waits for the Action Service's `Ready` condition on both. A matching echo comes back auto-approved
and executed, and the BFF receipt's Decision names the binding, the set and the matching policy;
an over-long message waits for the operator; after the binding is patched to an expiry in the past
and re-acknowledged, so does a match. The objects are deleted at teardown; the pending requests are
denied through the BFF so nothing lingers. The role in
`../../cluster/k8s/agentplane-testing/agentplane.k8s.yaml`
grants create/get/patch/delete on the two kinds for this.

`test_agent_mcp_bff_decision` adds allow/deny cases on each harness: turn 1 submits
an echo from a Sandbox nothing binds, so it waits for the operator, and returns only
its UUID; Python inspects and decides through the app's `/actions/{id}` BFF; the
same Agent polls in turn 2 and returns strict JSON. Python independently checks
durable request/Decision/Execution snapshots, exact arguments/result, operator
identity, duplicate-decision idempotency and stale-version rejection. Python also
reads the full event history through the BFF's `GET /actions/{id}/events` and asserts
the contiguous sequence and state progression. No canonical operator API fallback is used.

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
using the existing kubeconfig and Haku Console Kubernetes proxy
(`https://haku-kubeapi.allegedly.works`). No operator environment variables or
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

`//agentplane/acceptance:test_ollama_routes` exercises the twelve configured Ollama chat
routes on both harnesses, with the 128k cases first. Each cell creates a Sandbox,
opens a real session, and requires recorded shell-tool output. It has an absolute
300-second turn limit; a backend that never completes still leaves an interrupted
turn rather than a model verdict. To run just one cell, set `OLLAMA_SMOKE_CASE` to
its pytest id, for example
`harness_codex-ollama-oai-chat-gpt-oss-20b-128k`. Each cell writes a small JSON
result under the target's `test.outputs/` directory. A Bazel rerun of this target
replaces local test outputs, so save any evidence needed across runs first.

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

Afterwards, check that nothing leaked: `kubectl -n agentplane-testing get sandboxes.agents.x-k8s.io`
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
