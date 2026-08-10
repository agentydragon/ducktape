# Does the Matrix agent's traffic go through LiteLLM/Langfuse, and could it?

Investigated 2026-08-10, alongside <auto_mode_classifier.md>.

## The path today — confirmed, and it is neither

```text
Claude binary (haku-claude-sandbox pod)
  CLAUDE_CODE_OAUTH_TOKEN = "sk-ant-oat01-proxy-haku-claude-placeholder"   ← a placeholder
  HTTPS_PROXY = haku-egress-proxy:8180
        │
        ▼
haku-egress-proxy (iron, MITM with its own CA — NODE_EXTRA_CA_CERTS points at it)
  transforms.secrets: match_headers ["Authorization"], host api.anthropic.com
  swaps proxy_value → the real CLAUDE_CODE_OAUTH_TOKEN held only in this pod
        │
        ▼
api.anthropic.com
```

Sources: `ClaudeRuntimeConfig.claude_environment()` in <../config.py>, and
<../../../cluster/k8s/agents/haku-egress-proxy/claude-iron.yaml>.

So: **no LiteLLM, no Langfuse, no trace of these turns anywhere except the console's own
`claude_chat_messages` rows.** The sandbox never holds the real token — that is the point of the
substitution — and the egress proxy is a credential swapper, not an observability hop.

**Confirmed from the other end.** Langfuse holds 35,816 generations; a recent sample is
`gpt-5.6-terra` (Codex via CLIProxyAPI), `gpt-oss:20b` (local) and one embedding — **not one
Anthropic model**. Nothing that reaches Anthropic on the subscription is visible there today,
which is exactly the gap this note is about.

**And the obvious alternative explanation was checked and is wrong.** The suspicion that LiteLLM
simply does not trace the Anthropic Messages endpoint would have made that absence meaningless —
but `llm.request.type` across a 150-observation sample is `anthropic_messages` **129**,
`acompletion` 18, `aembedding` 3. The Messages API is not merely traced, it is the _dominant_
traced path: the `gpt-5.6-terra` rows are Claude Code speaking `/v1/messages` to LiteLLM, complete
with input, output and usage. The absence of Anthropic is therefore about **routing, not
instrumentation** — the subscription traffic never reaches the proxy.

This is also the encouraging datum for the whole idea: whatever fronts the subscription, the
traces would come out looking like the Claude Code traffic Langfuse already records well. One
blemish worth knowing — `llm.provider` is empty on the `anthropic_messages` rows (the
`acompletion` ones say `openai`), so provider attribution is lost on that path even though model,
input, output and usage all survive.

## Could LiteLLM sit in that path?

The obstacle is not LiteLLM's Anthropic support, it is **how subscription OAuth differs from an
API key**:

- LiteLLM's `anthropic/` provider authenticates with `x-api-key: <ANTHROPIC_API_KEY>` — a
  workspace API key. The repo already uses it that way: the plain `claude-*` entries in
  <../../../cluster/k8s/litellm/app/test_litellm_config.py> take `os.environ/ANTHROPIC_API_KEY`,
  an ESO mirror of "the spend-capped haku-cloud workspace key".
- A **subscription** token is `Authorization: Bearer sk-ant-oat01-…` plus the OAuth beta header,
  and Anthropic expects the request to look like Claude Code. That is a different auth scheme on
  a different header, which is exactly why the egress-proxy substitution matches on
  `Authorization` rather than `x-api-key`.

I found **no LiteLLM provider in this repo configured for subscription OAuth**, and no evidence
either way about an upstream one — that part is unverified, and worth ten minutes against
LiteLLM's provider list before anyone builds anything.

### There is already a working precedent for exactly this problem

**CLIProxyAPI** (<../../../cluster/k8s/cli-proxy-api/README.md>) solves the same shape for the
_Codex_ subscription: it holds its own OAuth session, speaks Anthropic `/v1/messages` to its
clients, and the main LiteLLM fronts it as the `codex-*` upstream through the ordinary
`anthropic/` provider pointed at `http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317`.
The README is explicit that it exists because LiteLLM's own `/v1/messages` bridge mistranslates
tool calls (BerriAI/litellm#25429).

**It supports Claude — and _how_ it does is the reason not to use it here.** The deployed build
answers for itself (`./CLIProxyAPI --help`, v7.2.77, commit c880371):

```text
  -claude-login
    Login to Claude using OAuth
```

Reading the source (`router-for-me/CLIProxyAPI` at `main`), the Claude path is four layers, and
only the first is what we assumed:

1. **OAuth** — `internal/auth/claude/anthropic_auth.go`: PKCE against
   `https://claude.ai/oauth/authorize`, token at `https://platform.claude.com/v1/oauth/token`,
   `ClientID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"` (Claude Code's own client id, hardcoded),
   `RedirectURI = "http://localhost:54545/callback"`, scopes
   `user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload`.
2. **Outbound auth** — `Authorization: Bearer <token>` for OAuth (vs `x-api-key` for an API key),
   with `Anthropic-Beta: claude-code-20250219, oauth-2025-04-20`.
3. **System-prompt cloaking** — `internal/runtime/executor/claude_executor_cloaking.go` **rewrites
   the caller's request**: it replaces top-level `system` with a synthetic billing block plus
   `"You are Claude Code, Anthropic's official CLI for Claude."` (marked ephemeral), demotes the
   caller's real system blocks to mid-conversation system messages after the first user turn, and
   injects a version (`2.1.220`), an entrypoint (`cli`) and the current date.
4. **Transport fingerprinting** — `helps/utls_client.go` dials with a Chrome/Claude-Code TLS
   ClientHello (`tls.HelloChrome_Auto`, `claudeCodeTLSClientHelloSpec`) and reproduces Claude
   Code's exact header order and casing (`claudeCodeRequestHeaderOrder`, and a map that restores
   `Anthropic-Beta` / `X-App` casing because Go would canonicalise them).

That is **impersonating the Claude Code client**, not proxying a credential — layers 3 and 4 exist
specifically to make a non-Claude-Code caller indistinguishable from one. Note what it costs even
setting intent aside: it mutates the request (so "same requests in as out" is exactly what it does
not do), the client id and fingerprint are the first things an upstream would pin on, and the
blast radius of being wrong is the subscription account.

**We do not need any of it, and that is the point.** Our Matrix path already runs the **real**
Claude Code binary in the sandbox: the genuine system prompt, headers and TLS are produced by the
genuine client, and the egress proxy only swaps a placeholder token for the real one. Layers 3 and
4 are there for callers that are _not_ Claude Code — Codex CLI, arbitrary API clients — which is
not our case. Adopting CLIProxyAPI for Claude would mean taking on impersonation machinery to
solve a problem we do not have.

So the CLIProxyAPI template does **not** carry over to the Claude side, and the recommendation
below stands more firmly than when it was written as a fallback.

## On the "same HTTP requests in as out" requirement

The user's constraint — the shim should forward what it receives so the traffic looks the same —
is the right one, and it is _more_ achievable via a passthrough shim than via LiteLLM proper.
LiteLLM normalises: it parses the request into its internal representation and re-emits it, which
is precisely where the tool-call mistranslation above comes from. A shim that only rewrites the
`Authorization` header and tees the body to Langfuse changes nothing about the request.

Which raises the honest question about design: **if the goal is only Langfuse visibility, the
egress proxy is already the natural place**. It is already MITM-ing this exact traffic, already
matching on `api.anthropic.com`, and already rewriting the header. A transform that additionally
posts the request/response pair to Langfuse would add observability with **no** change to the
request that reaches Anthropic and no second credential — strictly less machinery than routing
through LiteLLM, and it keeps the "same requests in as out" property by construction rather than
by care.

The cost is that it is a change to `iron`'s transform set rather than a config entry, and iron
is a shared component. Worth weighing against the LiteLLM route rather than assumed.

## Not wired up

Nothing was built, and after reading CLIProxyAPI's Claude implementation the "easily" the request
was conditioned on does not hold for that route. **The recommendation is the egress-proxy tee**:

1. Add a Langfuse tee to the existing `haku-egress-proxy` transform chain, next to the
   substitution that already rewrites `Authorization` for `api.anthropic.com`. It sees the exact
   request and response, changes neither, needs no second credential, and requires no client to
   be impersonated because the client genuinely _is_ Claude Code.
2. Route a **classifier** — small, our own, not the agent's traffic — through the existing main
   LiteLLM against the workspace API key, which already reports to Langfuse
   (<auto_mode_classifier.md> § "does not need the OAuth question answered").
3. Leave the subscription behind LiteLLM alone unless something later needs it for a reason
   beyond observability, since the only known route to it is the impersonation stack above.

On the ToS point, the distinction now has teeth. Teeing a copy of traffic the real Claude Code
sends is observability: nothing about the request changes, and the property is enforced by the
design rather than promised. Rewriting a caller's system prompt to claim it is Claude Code and
matching Claude Code's TLS fingerprint is a different act, whatever the intent behind it — worth
naming plainly rather than filing under the same "logging-only" heading.
