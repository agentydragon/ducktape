@README.md

Shared types between agents and host-side code go in `props/core/`; host-side code
(orchestration, backend, CLI) never lives here.

## Prompt templates

Mako templates rendered by `render_system_prompt()` into the agent's system prompt, with helpers
`describe_relation`, `include_doc`, `source_inspection` (<runtime.py>).

- If template A `include_doc()`s B, A must not `describe_relation()` a table B already describes
  (the agent would see the schema twice).
- Write for the agent in the container, not for developers: state the observable contract
  ("init output must stay under `util.exec.models.MAX_BYTES_CAP` or the run fails"), not
  implementation internals (which handler raises what).
