# Ember

Containerised LLM agent (`emberd`) that watches Matrix rooms and responds via
OpenAI tool calls. See <plan/SPEC.md> for the full specification and
<docs/agent_ontology.md> for vocabulary.

## Kubernetes

Credentials are projected into `/var/run/ember/secrets/` (Matrix token, Gitea
token, OpenAI key). Rotate by reapplying the Helm charts:

```bash
helm upgrade matrix k8s/helm/matrix-stack -n matrix -f k8s/helm/matrix-stack/values.yaml
helm upgrade gitea k8s/helm/gitea -n gitea --create-namespace
helm upgrade ember k8s/helm/ember -n ember --create-namespace
```

Persistent workspace at `/var/lib/ember/workspace` (`EMBER_WORKSPACE_DIR`).

```bash
# Tail logs
kubectl logs -n ember $(kubectl get pods -n ember -l 'app.kubernetes.io/name=ember,app.kubernetes.io/component=agent' -o jsonpath='{.items[0].metadata.name}') -f

# Roll pod
kubectl -n ember rollout restart deployment/ember
kubectl -n ember rollout status deployment/ember
```

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
