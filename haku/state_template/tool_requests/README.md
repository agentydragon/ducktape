# tool_requests/ — authored console-approved tool calls

Haku writes one YAML file here when it wants the operator to run a privileged tool through
haku-console from the UI. The file is the authored request; haku-console is the source of truth for
authorization, execution, audit, and results. Do not mirror results back into this repo.

The UI renders a request with:

```html
<tool-call request="request-id" label="Run the action"></tool-call>
```

That button reads `tool_requests/request-id.yaml` in the frontend, sends the exact request body to
the haku-ui backend, and the backend forwards it to haku-console. haku-console mints the canonical
`tool_call_id`, dedupes retries by the backend-supplied `client_request_id`, asks the trusted console
frontend for approval when required, and stores the result in its own audit log.

Schema:

```yaml
state_request_id: request-id
server_id: grocy-sf
tool_name: stock_add
title: Human-readable approval title
rationale: Why this call is useful right now
arguments:
  example: value
```

Rules:

- `state_request_id` must match the filename stem and is limited to letters, numbers, `.`, `_`, and
  `-`.
- `server_id` names a connected MCP server from haku-console's registry.
- `tool_name` names any tool reflected from that server's MCP `tools/list`.
- `arguments` must match that tool's MCP input schema; do not duplicate schemas here.
- Put grounding in `rationale`. Request arguments may contain secrets when the MCP operation really
  needs them; haku-state and haku-console are private stores, and public/reflection APIs must still
  avoid exposing configured bearer-token names or values.
- Results stay in haku-console. Haku can query or sweep the console audit log during its run when it
  wants to act on executed calls.
