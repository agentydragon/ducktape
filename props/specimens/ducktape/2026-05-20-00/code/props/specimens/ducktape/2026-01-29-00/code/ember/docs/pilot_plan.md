# Ember – Pilot Plan (v0)

This pilot delivers the smallest possible end-to-end loop: run Ember inside
its own container (`emberd`), let it watch a Matrix room, and talk to OpenAI
(`gpt-5`) strictly via tool calls. There is deliberately no policy gateway, no
approvals, and no extra MCP surfaces yet.

## Scope

- Containerised runtime exposes only `/healthz`, `/control/restart`, and
  `/control/shutdown`.
- Matrix is the only integration. The container polls the room with a scoped
  access token and wakes the agent whenever unread messages land.
- OpenAI Responses (model `gpt-5`) powers the assistant. The runtime forces the
  model to call one of two tools:
  - `run_shell_command` to talk back to Matrix (the agent is expected to invoke
    a CLI from inside the container).
  - `sleep_until_user_message` to suspend until the Matrix poller finds new traffic.
- Encrypted reasoning traces (`reasoning.encrypted_content`) and tool outputs
  are persisted to the local history for the next turn.
- No timeline projections, MCP chat servers, approvals, or policy enforcement.
  Matrix history is the sole source of truth for conversation logs.

## Steps

1. **Container image**
   - Build `emberd` image with Python, mautrix, OpenAI SDK, and the runtime.
   - Provide the direnv-managed editable install locally for quick iteration.
2. **Matrix/OpenAI wiring**
   - Inject `MATRIX_BASE_URL` and `MATRIX_ADMIN_USER_ID` via the config map.
   - Project the Matrix token (`matrix_access_token`) and OpenAI API key
     (`openai_api_key`) into `/var/run/ember/secrets` (optional `OPENAI_MODEL`,
     defaults to `gpt-5`).
   - Never enqueue events that originated from the agent itself.
3. **Runtime loop**
   - Poll Matrix, debounce room updates, wake the agent with a compact batch
     summary.
   - Persist tool calls and outputs to the on-disk history file.
   - Persist the raw OpenAI response, including reasoning summaries and encrypted
     reasoning payloads, so the next turn can replay context without loss.
   - Respect `sleep_until_user_message` by pausing until the Matrix poller reports new
     traffic.
4. **Control API + health**
   - `/healthz`: check runtime readiness.
   - `/control/restart`: rebuild history, restart Matrix and OpenAI clients.
   - `/control/shutdown`: graceful stop.
5. **Documentation & tooling**
   - Document environment variables, direnv usage, and the Docker image.
   - Track outstanding items in `docs/followups.md`.

## Out of scope (v0)

- MCP tool surfaces (runtime exec, resources, approvals, policy gateway).
- Loop hooks (`loop://hooks/{id}`) and handler hot-swaps.
- External timeline or UI state projections—the UI should read Matrix directly.
- Database writes outside the container; history remains a local JSONL file.

## Success criteria

- Container exposes a healthy `/healthz` and can be restarted via control API.
- Agent communicates exclusively through `run_shell_command` and
  `sleep_until_user_message`.
- Matrix room activity reflects the full conversation; no self-echo.
- Documentation and Dockerfile allow others to run the pilot with minimal setup.
