# Ember — Specification

Ember is a containerised LLM agent (`emberd`) that watches Matrix rooms and
responds via OpenAI tool calls. It runs on k3s with minimal infrastructure.

## Architecture

```
Matrix room ←→ emberd (poll + post) ←→ OpenAI Responses API
                 │
                 ├── /healthz, /control/{restart,shutdown}
                 ├── JSONL history (on-disk)
                 └── projected secrets (/var/run/ember/secrets/)
```

**LLM contract:** The model is forced to call tools — never raw text.
Two tools form the v0 surface:

| Tool                       | Purpose                               |
| -------------------------- | ------------------------------------- |
| `run_shell_command`        | Execute commands inside the container |
| `sleep_until_user_message` | Suspend until the Matrix poller wakes |

## Components

| Component      | Location                | Role                                       |
| -------------- | ----------------------- | ------------------------------------------ |
| Runtime        | `runtime.py`            | Agent loop, Matrix polling, sleep handling |
| FastAPI app    | `app.py`                | `/healthz`, `/control/*` endpoints         |
| Config         | `config.py`             | TOML config + env overrides                |
| Matrix client  | `matrix_client.py`      | Room polling, debounce, invite acceptance  |
| MCP tools      | `mcp_tools.py`          | Compositor: exec + sleep tool surfaces     |
| Handlers       | `handlers.py`           | Sleep, persistence, text-redirect handlers |
| Python session | `python_session.py`     | Persistent IPython kernel                  |
| System prompt  | `system_prompt.md.mako` | Mako template with embedded examples       |
| Secrets        | `secrets.py`            | Projected-secret file reader               |

## Requirements

### Secrets and configuration

- `MATRIX_BASE_URL`, `MATRIX_ADMIN_USER_ID` via config map or TOML.
- Matrix token and OpenAI API key projected into `/var/run/ember/secrets/`.
- Model defaults to `gpt-5`; overridable via `OPENAI_MODEL` or TOML.

### Runtime loop

- Poll Matrix, debounce room updates, batch into agent context.
- Persist tool calls, outputs, and encrypted reasoning traces to JSONL history.
- `sleep_until_user_message` pauses the loop until new Matrix traffic.
- Never enqueue events that originated from the agent itself.

### Control API

| Endpoint            | Method | Behaviour                        |
| ------------------- | ------ | -------------------------------- |
| `/healthz`          | GET    | Runtime readiness                |
| `/control/restart`  | POST   | Rebuild history, restart clients |
| `/control/shutdown` | POST   | Graceful stop                    |

### Deployment

- OCI image built via Bazel (`oci_image` / `oci_load`).
- Helm chart provisions namespace, PVC, projected secrets.
- Persistent workspace at `/var/lib/ember/workspace`.

## Ontology

See <docs/agent_ontology.md>.

## Not yet implemented

- MCP tool surfaces beyond exec (resources, approvals, policy gateway).
- Loop hooks (`loop://hooks/{id}`) and handler hot-swaps.
- External timeline / UI state projections.
- Database-backed history (currently JSONL).
