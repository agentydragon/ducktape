# TODO — Agentplane operator frontend

Gaps to close if a workflow needs them; none is committed.

## Per-Action widget schemas generated from the servers' models

The SSH `exec` widgets' zod schemas (`actions/rendering/ssh.tsx`) and the stored result in
`visual/harness.tsx` mirror `x/ssh_mcp_server/server.py`'s `exec` signature and `ExecResult` by
hand. The schemas are strict, so a field the server adds sends real results to the generic view
while every test still passes. Consider generating them from the pydantic models, for example from
the JSON Schema FastMCP publishes as the tool's `inputSchema` and `outputSchema`, converted to zod at
build time.
