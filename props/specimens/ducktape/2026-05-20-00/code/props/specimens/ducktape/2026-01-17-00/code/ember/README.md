# Ember (v0)

This directory contains the minimal scaffolding for the containerised agent
pilot described in `ember/docs/pilot_plan.md`.

The pilot intentionally keeps the feature set extremely small:

- the agent loop runs inside a dedicated container (`emberd`)
- the only integration is Matrix chat (accessed directly using a scoped token)
- no policy gateway, approvals, or additional MCP tool surfaces
- health, restart, and shutdown are exposed via a tiny HTTP API
- OpenAI Responses API (`gpt-5`) is forced to call tools; encrypted reasoning
  traces are stored alongside tool calls in the on-disk history
- Reasoning replay follows the encrypted reasoning guidance in the OpenAI
  Responses API docs
- Container image ships the repository docs under `/opt/emberd/docs` so Ember can
  read its own reference material at runtime.

The code here is **not** production ready. It gives the agent a sandbox that can
be iterated on while the surrounding control plane remains minimal.

## Kubernetes integration

The k3s deployment provisions Ember-specific credentials inside the `ember`
namespace and projects them into the pod as a shared secrets directory. The
Matrix bootstrap job still writes `matrix-ember-token`; the Gitea bootstrap job
still writes `gitea-ember-token`; a sealed secret `openai-api` stores the OpenAI
key. A projected volume merges them under `/var/run/ember/secrets` so Ember can
reload credentials in-place without a restart.

Reapply the charts whenever you need to rotate the credentials:

```bash
# Matrix credentials
helm upgrade matrix k8s/helm/matrix-stack -n matrix -f k8s/helm/matrix-stack/values.yaml

# Gitea credentials
helm upgrade gitea k8s/helm/gitea -n gitea --create-namespace

# Ember chart
helm upgrade ember k8s/helm/ember -n ember --create-namespace
```

The Helm chart replicates the old namespace definition and PVC setup, so a
plain `--create-namespace` is enough to bootstrap the space.

Ember gets a persistent scratch workspace at `/var/lib/ember/workspace`
(configurable via `EMBER_WORKSPACE_DIR`). The agent writes temporary scripts or
other helper artifacts there between Matrix turns, so expect that directory to
hold ad-hoc tooling and clean it up only when coordinating with the agent.

To inspect the logs of the current pod without looking up its name first:

```bash
kubectl logs -n ember $(kubectl get pods -n ember -l 'app.kubernetes.io/name=ember,app.kubernetes.io/component=agent' -o jsonpath='{.items[0].metadata.name}') -f
```

Drop `-f` to print once or add `--tail` to scope the output as needed.

To roll the pod after updating manifests or secrets:

```bash
kubectl -n ember rollout restart deployment/ember
kubectl -n ember rollout status deployment/ember
```

Consult `docs/agent_ontology.md` for the vocabulary Ember uses to describe the
runner, the LLM core, and the surrounding control loop.

## Running locally

The directory uses direnv + uv to manage an isolated virtual environment. Allow it once:

```bash
cd ember
direnv allow    # creates .venv and installs the package in editable mode
```

```bash
cat <<'EOF' > ember.toml
[matrix]
base_url = "https://matrix.example.com"
admin_user_id = "@agentydragon:matrix.example.com"

[state]
dir = "${PWD}/.pilot-state"
workspace_dir = "${PWD}/.pilot-workspace"

[openai]
model = "gpt-5-codex"
reasoning_effort = "medium"
include_encrypted_reasoning = true
EOF

export MATRIX_ACCESS_TOKEN="s3cret"
export OPENAI_API_KEY="sk-..."
export EMBER_CONFIG_FILE="${PWD}/ember.toml"

# optional overrides
# export EMBER_STATE_DIR="${PWD}/.pilot-state"
# export EMBER_WORKSPACE_DIR="${PWD}/.pilot-workspace"
# export OPENAI_MODEL="gpt-5.1"

emberd

# or use uvicorn directly:
# EMBER_CONFIG_FILE=${PWD}/ember.toml uvicorn ember.app:create_app --factory --reload
```

On k3s, the OpenAI API key is supplied via the projected secret file rather than
an environment variable; the env var export above is only required for local
development.

With that running you can:

- `curl http://127.0.0.1:8000/healthz`
- `curl -X POST http://127.0.0.1:8000/control/restart`
- `curl -X POST http://127.0.0.1:8000/control/shutdown`

The Matrix client polls the configured rooms and records unread messages. The
assistant is expected to use the `run_shell_command` tool to post replies (for
example via a CLI utility). No additional tool surfaces are exposed in this v0
pilot.

The runtime accepts invites from the `matrix.admin_user_id` account. Joined rooms
are discovered directly from the homeserver (`/_matrix/client/v3/joined_rooms`)
when Ember starts, so the agent will resume listening in the same spaces after a
restart without relying on local cache files.
