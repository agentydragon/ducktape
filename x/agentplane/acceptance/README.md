# Agentplane acceptance suite

Scenarios run against a **deployed** Agentplane: the suite creates real sandboxes through the app's
HTTP API, opens sessions on the real harnesses, and asserts on what the egress proxy recorded. It is
not a unit test with a live backend — it is the check that the deployed system does what
`x/agentplane/plans/egress_proxy.md` says it does.

## Running it

Not in CI, and not on RBE: the target is `manual`, so `//...` never selects it, and it needs a
kubeconfig and a route to the cluster.

```bash
bazelisk test //x/agentplane/acceptance:test_egress --test_output=streamed --test_arg=-s
```

By default it tests `https://agentplane-staging.allegedly.works` and mints its own bearer token with
`kubectl -n agentplane-staging create token agentplane-agent --audience=agentplane`. That call needs
RBAC on `serviceaccounts/token`, and the app only admits subjects its `AGENTPLANE_TOKEN_SUBJECTS`
names, so a token for any other ServiceAccount is refused with `403`.

Override any of it through the environment:

| Variable                                | Default                                      |
| --------------------------------------- | -------------------------------------------- |
| `AGENTPLANE_ACCEPTANCE_URL`             | `https://agentplane-staging.allegedly.works` |
| `AGENTPLANE_ACCEPTANCE_TOKEN`           | minted with `kubectl`                        |
| `AGENTPLANE_ACCEPTANCE_NAMESPACE`       | `agentplane-staging`                         |
| `AGENTPLANE_ACCEPTANCE_SERVICE_ACCOUNT` | `agentplane-agent`                           |

## What it costs

Each scenario provisions a Pod and runs turns on the cheap-experiments LiteLLM key with Haiku, so a
full run is minutes and a few cents. Sandboxes are suspended and deleted in fixture teardown,
including after a failure; a teardown that cannot delete one fails loudly, because a leaked sandbox
holds a PVC and a node slot on staging.

## Why it asserts on the ring, not on the agent

The agent's own account of a tool call is prose. "I fetched the repository" is equally consistent
with a request the proxy admitted, a request that never reached the proxy, and a model that did not
run the command at all. The proxy's decision ring is the system's record of what it actually served,
so that is what a scenario checks; the turn's output is carried into the failure message, where it
explains a failure rather than deciding one.

This suite exists because the last gap of that shape — a runner that dropped the sandbox's proxy
variables, so every call bypassed the proxy and hung with an empty ring — sat behind a fully green
unit suite until someone drove the deployed app by hand.
