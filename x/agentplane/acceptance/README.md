# Agentplane acceptance suite

The default acceptance scenarios are **deployed vertical tests**: they create real sandboxes through
the app's HTTP API, open sessions on the real harnesses, use the deployed LLM, and assert on
cross-component behavior and durable system evidence. They are not unit tests with a live backend —
they are the checks that the deployed system does what <../egress/SPEC.md> says it does and that a
session's standing instructions reach the model that serves it.

There is one deliberate exception: `test_operator_login` is a fast, offline provider-adapter test.
It uses mocked Authentik and Dex responses, creates no sandbox, and does not prove that deployed Dex
or Agentplane is reachable. It stays in this package because login choreography is a high-churn
prerequisite for the vertical scenarios, and keeping its regression loop beside them makes it useful
when changing the real login path. The deployed login path remains covered as setup for the MCP
vertical scenario.

The general egress and instruction scenarios run on **both harnesses**. The runner protocol is the
same for Claude and Codex, so one test body covers both: the `provider` fixture is parametrised over
`Provider`, and `model` asks the deployment which models it offers for that harness rather than
hardcoding one. `test_launch_presets` instead exercises the configured `public-coder` preset's
intentional Codex default, Sandbox binding, bootstrap marker, inherited fields, and local override.

## MCP integration

`//x/agentplane/acceptance:test_mcp` belongs to this deployed suite. Both real harnesses
receive the Action API URL and public workload placeholder, discover the `everything`
group's `echo` Action, submit a structured group/name request, poll until terminal,
and report JSON. The test checks the reported result against the fresh marker's exact
upstream echo output. It uses the existing sandbox setup/teardown and `Agent` fixtures.
Testing GitOps (`cluster/k8s/agentplane-testing/actions/`) wires the upstream image, ActionGroup,
narrow echo provider, and discovery egress. Run after the PR's images and manifests have rolled
out; remote adapter tests are not evidence that the real-agent deployed test has run.

`test_agent_mcp_bff_decision` adds allow/deny cases on each harness: turn 1 submits
an echo longer than the fixture's 200-character auto-allow bound and returns only
its UUID; Python inspects and decides through the app's `/actions/{id}` BFF; the
same Agent polls in turn 2 and returns strict JSON. Python independently checks
durable request/Decision/Execution snapshots, exact arguments/result, operator
identity, duplicate-decision idempotency and stale-version rejection. Python also
reads the full event history through the BFF's `GET /actions/{id}/events` and asserts
the contiguous sequence and state progression. No canonical operator API fallback is used.

These cases read only `public-coder-agent/agentplane-testing-acceptance-operator` via
`kubectl get --raw=/api/v1/namespaces/public-coder-agent/secrets/agentplane-testing-acceptance-operator`
using the existing kubeconfig and Haku Console Kubernetes proxy
(`https://haku-kubeapi.allegedly.works`). No operator environment variables or
pre-issued cookie are used. Missing proxy/RBAC/reflection or malformed Secret data
fails **BLOCKED**, without printing kubectl output or decoded values.

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

## Running it

Not in CI, and not on RBE: the target is `manual`, so `//...` never selects it, and it needs a
kubeconfig and a route to the cluster.

Bazel excludes `manual` targets from package patterns, so `:all` only runs the fast
`test_operator_login` exception. It does not run any deployed vertical scenario. Name the live
scenario explicitly. Start with the egress vertical slice:

```bash
bazelisk test //x/agentplane/acceptance:test_egress --test_output=streamed --test_arg=-s
```

Run the other live scenarios by their explicit targets: `:test_launch_presets`,
`:test_instructions`, and `:test_mcp`.

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
See [repository instructions](../../../AGENTS.md).

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
