# Agentplane acceptance suite

Scenarios run against a **deployed** Agentplane: the suite creates real sandboxes through the app's
HTTP API, opens sessions on the real harnesses, and asserts on what the egress proxy recorded. It is
not a unit test with a live backend — it is the check that the deployed system does what
<../egress/SPEC.md> says it does, and that a session's standing instructions reach the model that
serves it.

Every scenario runs on **both harnesses**. The runner protocol is the same for Claude and Codex, so
one test body covers both: the `provider` fixture is parametrised over `Provider`, and `model` asks
the deployment which models it offers for that harness rather than hardcoding one.

## Running it

Not in CI, and not on RBE: the target is `manual`, so `//...` never selects it, and it needs a
kubeconfig and a route to the cluster.

```bash
bazelisk test //x/agentplane/acceptance:all --test_output=streamed --test_arg=-s
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

### Where an agent can run it

An agent in a Claude Code session usually cannot: this suite needs a kubeconfig, a route to the
cluster, and a Bazel that can fetch the module graph, and a web session typically has none of the
three. A Haku sandbox has all of them, and it mints its own token, so no credential has to be
handed to it.

```bash
# in a Haku sandbox (sandbox__provision_sandbox, then sandbox__exec_sandbox)
git clone --depth 1 --branch <branch> https://github.com/agentydragon/ducktape.git
cd ducktape && bazel test //x/agentplane/acceptance:all \
  --remote_executor= --remote_cache= --bes_backend= --bes_results_url= \
  --spawn_strategy=local --genrule_strategy=local --config=nolint \
  --test_output=streamed --nocache_test_results
```

**Deviation:** those flags turn off remote execution and caching, which
[AGENTS.md](../../../AGENTS.md) otherwise forbids. A Haku sandbox can reach neither
`remote.buildbuddy.io` nor an API key for it, and this target is `no-remote-exec` regardless; the
repo-wide rule is about machines that have BuildBuddy, and that one does not. Run it with `nohup`
into a log and poll — a full run is two to four minutes, longer than one `exec_sandbox` call.

Afterwards, check that nothing leaked: `kubectl -n agentplane-staging get sandboxes.agents.x-k8s.io`
should show no `accept-*`.

## What it costs

Each scenario provisions a Pod and runs turns on the cheap-experiments LiteLLM key with Haiku, so a
full run is minutes and a few cents. Sandboxes are suspended and deleted in fixture teardown,
including after a failure; a teardown that cannot delete one fails loudly, because a leaked sandbox
holds a PVC and a node slot on staging.

## TODO: sweep sandboxes a killed run leaks

Fixture teardown suspends and deletes every sandbox a scenario created, including after a failure.
It cannot run if the process is killed outright — a Bazel timeout, a `^C`, a dropped connection —
and each leaked sandbox holds a PVC and a node slot on staging until someone notices.

What that wants is a sweep at session start: list the sandboxes whose names carry this suite's
`accept-` stem, and delete any older than a run could plausibly be. Deliberately not built yet,
because the stem is the only marker and a real sandbox someone named `accept-something` would be
destroyed by it — a label the app sets on suite-created sandboxes, or a dedicated namespace, is the
thing to add first.

## Why it asserts on the ring, not on the agent

The agent's own account of a tool call is prose. "I fetched the repository" is equally consistent
with a request the proxy admitted, a request that never reached the proxy, and a model that did not
run the command at all. The proxy's decision ring is the system's record of what it actually served,
so that is what a scenario checks; the turn's output is carried into the failure message, where it
explains a failure rather than deciding one.

Where a scenario has to look _inside_ the sandbox, it never asks the model to print a secret. A
model that declines to echo a credential, or redacts it, produces output with no credential in it —
which would satisfy an "is it absent" assertion for entirely the wrong reason. The command prints a
verdict token instead, and the scenario fails unless one of the two tokens actually comes back, so a
refusal reads as a failure rather than as an absence.

This suite exists because the last gap of that shape — a runner that dropped the sandbox's proxy
variables, so every call bypassed the proxy and hung with an empty ring — sat behind a fully green
unit suite until someone drove the deployed app by hand.

`test_instructions` is the one scenario that cannot follow the rule: no part of the system records
that a system prompt arrived, so the model obeying the instruction is the only evidence there is.
Its answer to that is a marker token no model emits on its own plus a control session, opened with
no instructions on the same sandbox and given the same prompt, that must not produce it.
