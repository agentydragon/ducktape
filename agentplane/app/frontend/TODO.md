# TODO — Agentplane operator frontend

Gaps to close if a workflow needs them; none is committed.

## Per-Action widget schemas generated from the servers' models

The SSH `exec` widgets' zod schemas (`actions/rendering/ssh.tsx`) and the stored result in
`visual/harness.tsx` mirror `x/ssh_mcp_server/server.py`'s `exec` signature and `ExecResult` by
hand. The schemas are strict, so a field the server adds sends real results to the generic view
while every test still passes. Consider generating them from the pydantic models, for example from
the JSON Schema FastMCP publishes as the tool's `inputSchema` and `outputSchema`, converted to zod at
build time.

## Hidden characters in what the operator approves

Bidi controls, zero-width and other default-ignorable characters, and control characters render
invisibly, so a command or argument can read differently from what runs. highlight.js escapes only
markup characters and `JSON.stringify` only C0 controls, so neither the highlighted views
(`syntax_highlight.tsx`) nor plain text (titles, descriptions, stdout, drawn arguments) show them.
One way: in `highlight()`, between highlight.js and DOMPurify, wrap each
`[\p{Bidi_Control}\p{Default_Ignorable_Code_Point}]` match and each control character other than tab
and newline in a span showing its code point; do the same in plain text through a small React helper;
and warn on the card, as GitHub does for bidi text. Bidi controls and tag characters warrant a loud
marker; emoji joiners and variation selectors a quiet one.
