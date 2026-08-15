# `openclaw.json`

Declarative OpenClaw config, planted into the state PVC by an
init container. JSON takes no comments, so the rationale lives here.

Declarative OpenClaw config, planted into the state PVC by an init container.

gateway.bind is "lan" (not loopback as in the lab rig): the Authentik outpost
reaches this pod over the cluster network, so the gateway must listen on the
pod IP. What keeps that safe is not the bind address but networkpolicy.yaml,
which admits only the outpost's pods -- without it any pod could forge
x-authentik-username and be trusted as agentydragon.

The agent uses Codex subscription models through one LiteLLM key. Only the 5.6
group is offered. contextWindow/maxTokens are measured, not
published: openai_utils/probe_context_window.py binary-searches the live
serving path and all three 5.6 models reject identically just above
372,000 total context. Published figures disagree in both directions --
the raw models are ~1.05M, Codex product docs say 272K -- and neither is
what this chain accepts. cluster/validation/test_codex_context_window.py
pins every declaration in the repo to the same measured numbers.

`bind` is an enum -- `loopback`, `lan`, `tailnet`, `auto`, `custom`. `"all"` is
not a member and the gateway exits with
`Invalid --bind. Use "loopback", "lan", "tailnet", "auto", or "custom".`

The Matrix channel leaves `password` out of this file deliberately. OpenClaw
reads the documented `MATRIX_PASSWORD` fallback, which is a stable placeholder;
iron-proxy swaps the placeholder for the real SOPS-backed password only on the
Matrix login body. OpenClaw then caches the resulting access token in its state
directory. The Matrix DM config uses `sessionScope: "per-room"` and threaded
replies so concurrent DM rooms remain independent.

`requestTimeoutMs` on the haku-console server is load-bearing, and its absence is not
a slow-server problem. OpenClaw gives the startup catalog listing a hardcoded
`BUNDLE_MCP_CATALOG_LIST_TIMEOUT_MS = 1500` ms **unless the server entry declares
`requestTimeoutMs` or `timeout`** -- `getCatalogListTimeoutMs` in
`agent-bundle-mcp-runtime`. Declaring either one makes the catalog use the server's
request timeout instead. Haku Console fans out to every upstream MCP server it proxies,
so its `tools/list` is 194 tools and ~733 KB and takes ~5.9 s. Against a 1.5 s budget it
never finishes, and `bundle-mcp` gives up with

    McpError: MCP error -32001: Request timed out

so the server is never registered and none of its tools attach. The failure is silent in
the sense that matters: every HTTP request succeeds (`initialize` 200, `notifications/initialized`
202, `tools/list` 200) -- only the client-side deadline fires. That is why it presents as a
network or auth fault and is neither.

`${HAKU_CONSOLE_TOKEN}` **is** interpolated; do not "fix" this by inlining the literal.
Verified twice: iron-proxy's audit log annotates every one of the agent's requests
`swapped: [HAKU_CONSOLE_TOKEN]`, which only happens when it finds the expanded
placeholder in the header, and a probe with `requestTimeoutMs` added and the `${...}`
left alone returns all 194 tools. `mcp doctor`'s "contains a literal sensitive value"
warning is a static lint on the config text, not evidence about what gets sent -- and
`mcp doctor` is documented as checking _static_ setup problems, so it does not detect
this at all. `openclaw mcp probe` is the command that connects.
