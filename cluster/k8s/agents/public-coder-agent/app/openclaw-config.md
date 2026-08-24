# `openclaw.json`

Declarative OpenClaw config, planted into the state PVC by an
init container. JSON takes no comments, so the rationale lives here.

Declarative OpenClaw config, planted into the state PVC by an init container.

OpenClaw defaults every conversation to a new backing session at 04:00 local
time, even when the visible dashboard thread is unchanged. It has no disabled
reset mode: the schema permits only `daily` and `idle`. The configured 100-year
idle timeout therefore deliberately makes automatic rollover unreachable in
practice while preserving explicit `/new` and `/reset` commands.

gateway.bind is "lan" (not loopback as in the lab rig): the Authentik outpost
reaches this pod over the cluster network, so the gateway must listen on the
pod IP. What keeps that safe is not the bind address but networkpolicy.yaml,
which admits only the outpost's pods -- without it any pod could forge
x-authentik-username and be trusted as agentydragon.

Trusted-proxy authentication protects browser traffic arriving through the
Authentik outpost. OpenClaw's own backend clients (including subagent completion
handoffs) connect directly to the loopback Gateway and therefore have none of
the proxy's identity headers. `gateway-password-eso.yaml` generates the separate
local fallback password and the Deployment exposes it as
`OPENCLAW_GATEWAY_PASSWORD`; OpenClaw deliberately accepts that password only
for direct local requests after proxy authentication is inapplicable. Do not
replace this with loopback proxy-header trust or commit the generated value to
`openclaw.json`.

The agent uses Codex subscription models through one LiteLLM key. Only the 5.6
group is offered. contextWindow/maxTokens are measured, not
published: openai_utils/probe_context_window.py binary-searches the live
serving path and all three 5.6 models reject identically just above
372,000 total context. Published figures disagree in both directions --
the raw models are ~1.05M, Codex product docs say 272K -- and neither is
what this chain accepts. cluster/k8s/litellm/app/test_openclaw_models.py
pins every declaration in the repo to the same measured numbers.

The same LiteLLM key and `litellm-subscription` provider also offer Google's
Gemini chat lineup (`gemini-*`, mirroring GEMINI_MODELS in
cluster/k8s/litellm/app/model_rosters.py), routed the same way Codex is --
LiteLLM's `/v1/messages` surface accepts an Anthropic-shaped request for any
backend model and translates it, so no separate provider entry is needed.
Unlike Codex, there is no live serving-path probe for a hosted third-party
API: contextWindow (1,048,576) and maxTokens (65,536) are Google's published
limits for the current Gemini generation, shared by every entry except that
gemini-3.5-flash-lite is the deliberately cheap/fast tier and is marked
`"reasoning": false`. cluster/k8s/litellm/app/test_openclaw_models.py pins
the catalog to the roster and these figures. The agent's default model is
unchanged (still a Codex 5.6 model) -- Gemini is added as a selectable
option, not a new default.

`bind` is an enum -- `loopback`, `lan`, `tailnet`, `auto`, `custom`. `"all"` is
not a member and the gateway exits with
`Invalid --bind. Use "loopback", "lan", "tailnet", "auto", or "custom".`

The Matrix channel leaves `password` out of this file deliberately. OpenClaw
reads the documented `MATRIX_PASSWORD` fallback, which is a stable placeholder;
iron-proxy swaps the placeholder for the real SOPS-backed password only on the
Matrix login body. OpenClaw then caches the resulting access token in its state
directory. The Matrix DM config uses `sessionScope: "per-room"` and threaded
replies so concurrent DM rooms remain independent. `autoJoin` is `"always"`
because Matrix presents a new DM as a room invite and cannot classify it until
after joining. This does not widen who can drive the agent: DMs remain
allowlisted to `@agentydragon:allegedly.works` and group rooms remain disabled.

`channels.matrix.proxy` is also load-bearing. The Matrix plugin does not make
ordinary global `fetch` calls: its guarded fetch layer constructs a dispatcher
from this channel field. `HTTP_PROXY`, `HTTPS_PROXY`, and
`NODE_USE_ENV_PROXY=1` therefore do not route Matrix traffic by themselves;
without the explicit field, the SDK attempts the homeserver's public IPs
straight from the egress-confined app pod and times out. Keep this URL equal to
the Deployment's `HTTPS_PROXY` value. The dedicated proxy also preserves the
login-body password substitution described above.

Matrix is a separate nix-openclaw runtime-plugin derivation, but the image
physically adds it to the gateway's `dist/extensions` and
`dist-runtime/extensions` bundled-plugin trees. That layout is
security-sensitive: Matrix persists sync and encryption state
through `openKeyedStore`, which OpenClaw intentionally exposes only to bundled
or otherwise trusted official installs. Loading the same derivation through an
arbitrary `plugins.load.paths` entry marks it as `origin: "config"`, and the
provider exits before sync with `openKeyedStore is only available for trusted
plugins in this release.` Keep the config limited to enabling the bundled
`plugins.entries.matrix`; do not restore a path-load workaround. The Brave
runtime plugin is likewise bundled into the Nix-built gateway image so its
pinned artifact, rather than a mutable install in the state PVC, supplies
`web_search`.

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
