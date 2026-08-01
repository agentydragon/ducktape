# `openclaw.json`

Declarative OpenClaw config, planted into the state PVC by an
init container. JSON takes no comments, so the rationale lives here.

Declarative OpenClaw config, planted into the state PVC by an init container.

gateway.bind is "lan" (not loopback as in the lab rig): the Authentik outpost
reaches this pod over the cluster network, so the gateway must listen on the
pod IP. What keeps that safe is not the bind address but networkpolicy.yaml,
which admits only the outpost's pods -- without it any pod could forge
x-authentik-username and be trusted as agentydragon.

Two lanes on one LiteLLM key: the Codex subscription models and the z.ai GLM
models. Only the 5.6 group is offered from the Codex lane. contextWindow/maxTokens are measured, not
published: openai_utils/probe_context_window.py binary-searches the live
serving path and all three 5.6 models reject identically just above
372,000 total context. Published figures disagree in both directions --
the raw models are ~1.05M, Codex product docs say 272K -- and neither is
what this chain accepts. cluster/validation/test_codex_context_window.py
pins every declaration in the repo to the same measured numbers.

`bind` is an enum -- `loopback`, `lan`, `tailnet`, `auto`, `custom`. `"all"` is
not a member and the gateway exits with
`Invalid --bind. Use "loopback", "lan", "tailnet", "auto", or "custom".`

The haku-console MCP `Authorization` header carries the literal
`proxy-haku-console-placeholder`, not `${HAKU_CONSOLE_TOKEN}`. Not every code path
that reads this file interpolates `${...}`: the gateway's own MCP client does, but
`mcp doctor` and `bundle-mcp` send the string unexpanded -- `mcp doctor` even says so
("headers.Authorization contains a literal sensitive value"). The console answers an
unrecognized bearer with `401` plus
`WWW-Authenticate: Bearer ... resource_metadata=".../.well-known/oauth-protected-resource/mcp"`,
which sends the MCP client into OAuth discovery and dynamic client registration. Nothing
headless can finish that dance, so the request hangs until the client gives up with
`McpError -32001: Request timed out` -- a timeout whose cause is an auth failure, which is
why it reads like a network problem and is not one.

Inlining the literal is safe because this value is not a credential: it is the
placeholder that ../proxy/iron.yaml swaps for the real static-Agent bearer, and only in
the `Authorization` header for `haku.allegedly.works`. The `HAKU_CONSOLE_TOKEN` env var
in deployment.yaml stays -- it is what the agent presents on its own `curl`s to the
console -- it is simply no longer on the path that has to survive interpolation.
