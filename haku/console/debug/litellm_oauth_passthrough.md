# Does the Matrix agent's traffic go through LiteLLM/Langfuse, and could it?

Investigated 2026-08-10, alongside <auto_mode_classifier.md>.

## The path today — neither

```text
Claude binary (haku-claude-sandbox pod)
  CLAUDE_CODE_OAUTH_TOKEN = "sk-ant-oat01-proxy-haku-claude-placeholder"   ← a placeholder
  HTTPS_PROXY = haku-egress-proxy:8180
        │
        ▼
haku-egress-proxy (iron-proxy, tls.mode: mitm, its own CA — NODE_EXTRA_CA_CERTS points at it)
  transforms.secrets: match_headers ["Authorization"], host api.anthropic.com
  swaps proxy_value → the real CLAUDE_CODE_OAUTH_TOKEN held only in this pod
        │
        ▼
api.anthropic.com
```

Sources: `ClaudeRuntimeConfig.claude_environment()` in <../config.py>, and
<../../../cluster/k8s/agents/haku-egress-proxy/claude-iron.yaml>.

No LiteLLM, no Langfuse: these turns leave no trace anywhere but the console's own
`claude_chat_messages` rows. The sandbox never holds the real token — that is the point of the
substitution — and the egress proxy is a credential swapper, not an observability hop.

**What the proxy does and does not preserve.** `mitm` mode means iron terminates the sandbox's
TLS and **re-originates its own connection** upstream. So the request _body_ — system prompt,
messages, betas — is exactly what the real Claude Code binary composed, while the ClientHello
Anthropic sees is iron's TLS stack, and header order and casing survive only as far as iron's
serialisation preserves them. We send the real client's request over our own transport.

## What Langfuse shows, and why

Langfuse holds 35,816 generations. A recent sample is `gpt-5.6-terra` (Codex via CLIProxyAPI),
`gpt-oss:20b` (local) and one embedding — **not one Anthropic model**.

That absence is about routing, not instrumentation. `llm.request.type` across a 150-observation
sample is `anthropic_messages` **129**, `acompletion` 18, `aembedding` 3: LiteLLM traces the
Anthropic Messages endpoint perfectly well, and it is in fact the dominant traced path — the
`gpt-5.6-terra` rows are Claude Code speaking `/v1/messages` to LiteLLM, with input, output and
usage intact. Subscription traffic is missing because it never reaches the proxy.

Encouraging corollary: whatever ends up fronting the subscription, its traces will come out
looking like the Claude Code traffic Langfuse already records well. One blemish — `llm.provider`
is empty on `anthropic_messages` rows (the `acompletion` ones say `openai`), so provider
attribution is lost on that path even though model, input, output and usage survive.

## Could LiteLLM sit in that path?

The obstacle is how subscription OAuth differs from an API key:

- LiteLLM's `anthropic/` provider authenticates with `x-api-key: <ANTHROPIC_API_KEY>`, a workspace
  key. The repo already uses it that way — the plain `claude-*` entries in
  <../../../cluster/k8s/litellm/app/test_litellm_config.py> take `os.environ/ANTHROPIC_API_KEY`,
  an ESO mirror of the spend-capped haku-cloud workspace key.
- A subscription token is `Authorization: Bearer sk-ant-oat01-…` plus OAuth beta headers. A
  different scheme on a different header, which is why the egress-proxy substitution matches on
  `Authorization` rather than `x-api-key`.

No LiteLLM provider in this repo is configured for subscription OAuth, and whether one exists
upstream is unverified — worth ten minutes against LiteLLM's provider list before building
anything on the assumption either way.

### CLIProxyAPI: the precedent, and why it does not transfer

**CLIProxyAPI** (<../../../cluster/k8s/cli-proxy-api/README.md>) solves this shape for the _Codex_
subscription: it holds its own OAuth session, speaks Anthropic `/v1/messages` to its clients, and
the main LiteLLM fronts it as the `codex-*` upstream through the ordinary `anthropic/` provider at
`http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317`. It exists because LiteLLM's own
`/v1/messages` bridge mistranslates tool calls (BerriAI/litellm#25429).

It supports Claude too — the deployed build says so (`./CLIProxyAPI --help`, v7.2.77, commit
`c880371`):

```text
  -claude-login
    Login to Claude using OAuth
```

Reading `router-for-me/CLIProxyAPI` at `main`, that path is four layers:

1. **OAuth** (`internal/auth/claude/anthropic_auth.go`) — PKCE against
   `https://claude.ai/oauth/authorize`, token at `https://platform.claude.com/v1/oauth/token`,
   `ClientID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"` (Claude Code's own, hardcoded),
   `RedirectURI = "http://localhost:54545/callback"`, scopes
   `user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload`.
2. **Outbound auth** — `Authorization: Bearer <token>` for OAuth (vs `x-api-key` for an API key),
   with `Anthropic-Beta: claude-code-20250219, oauth-2025-04-20`.
3. **System-prompt cloaking** (`internal/runtime/executor/claude_executor_cloaking.go`) — rewrites
   the caller's request: replaces top-level `system` with a synthetic billing block plus
   `"You are Claude Code, Anthropic's official CLI for Claude."` (ephemeral), demotes the caller's
   real system blocks to mid-conversation system messages after the first user turn, and injects a
   version (`2.1.220`), an entrypoint (`cli`) and the current date.
4. **Transport fingerprinting** (`helps/utls_client.go`) — dials with a Chrome/Claude-Code TLS
   ClientHello (`tls.HelloChrome_Auto`, `claudeCodeTLSClientHelloSpec`) and reproduces Claude
   Code's header order and casing (`claudeCodeRequestHeaderOrder`, plus a map restoring
   `Anthropic-Beta` / `X-App` casing that Go would otherwise canonicalise).

Layers 3 and 4 are impersonation: they exist to make a caller that is not Claude Code
indistinguishable from one. Setting intent aside, layer 3 also mutates the request, so "the same
requests out as in" is precisely what it does not give.

**Neither layer is worth buying here.** Layer 3 we already have honestly, because the sandbox runs
the real binary. Layer 4 we already lack — iron re-originates the connection — and this path runs
without trouble, which is evidence that ClientHello fingerprinting is not enforced on it, or at
least not acted on. CLIProxyAPI's uTLS work may be defensive, aimed at another upstream, or simply
thorough; it is not evidence that we are being measured on something we are already failing.

So adopting CLIProxyAPI on the Claude side would mean forging the part we currently get honestly,
to fix a part nobody appears to be judging.

### Is there a private inference API the binary has and the SDK does not expose?

No. Every inference call CLIProxyAPI makes goes to `https://api.anthropic.com/v1/messages`, the
same public Messages API an API key uses; its only other host is
`https://platform.claude.com/v1/oauth/token` for the token lifecycle. Everything special is in how
the request is dressed, not where it is sent.

The binary does carry a wider private surface than the SDK exposes, but it is account machinery.
Endpoint strings in `claude_agent_sdk/_bundled/claude`:

| Family                | Examples                                                                                                                                                                                |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Public inference      | `/v1/messages`, `/v1/messages/count_tokens`, `/v1/messages/batches`, `/v1/models`, `/v1/files`, `/v1/complete`                                                                          |
| Private account/OAuth | `/api/oauth/organizations/…` (37 refs), `/api/oauth/validate`, `/api/oauth/usage`, `/api/oauth/account/settings`, `/api/oauth/claude_cli/roles`, `/api/oauth/claude_cli/create_api_key` |
| Other private         | `/api/hello`, `/api/claude_cli_feedback`, `/v1/sessions/…`, `/v1/deployments`, `/api/web/domain_info`                                                                                   |

CLIProxyAPI touches only profile and roles, to identify the account. Nothing private is needed to
make an inference call.

`/api/oauth/claude_cli/create_api_key` is worth naming because it looks like a shortcut and is
not: neighbouring strings are `platform.claude.com/oauth/code/success?app=claude-code` and
`buy_credits`, so it is the supported "create an API key from your Claude account" flow, minting
an ordinary console key billed as API usage. It is not a way to bill inference to the
subscription.

The framing matters more than the inventory: **the SDK ships the same binary**. "Not exposed by
the SDK" describes the Python surface, not a capability gap — anything the binary can do is
reachable by running it, which is what `ClaudeSDKClient` does. What is missing is a programmatic
way to make one raw request, not an endpoint.

## Recommendation

1. **Tee to Langfuse from the existing egress proxy.** It already MITMs this exact traffic and
   already rewrites the header; a tee changes nothing about the request, needs no second
   credential, and keeps "same requests out as in" by construction rather than by care. The cost
   is a change to iron's transform set, and iron is shared — weigh that rather than assume it.
2. **Route a classifier through the existing main LiteLLM** on the workspace API key, which
   already reports to Langfuse (<auto_mode_classifier.md> § "does not need the OAuth question
   answered"). Small, ours, and not the agent's traffic.
3. **Leave the subscription-behind-LiteLLM idea alone** unless something later needs it for a
   reason beyond observability, since the only known route to it is the impersonation stack above.

On the ToS question: teeing a copy of traffic the real Claude Code sends is observability, and
nothing about the request changes. Rewriting a caller's system prompt to claim it is Claude Code
is a different act whatever the intent, and belongs under a different heading.

Context worth having: OpenAI takes the opposite position on the equivalent question. ChatGPT
Plus/Pro/Team subscription OAuth is a documented, supported way for third-party clients to charge
model calls to a subscription — Cline ships it as a feature, OpenClaw documents it as a provider,
and there are maintained OAuth plugins for opencode. That asymmetry is real rather than a
misreading, and it is why the Codex side of this cluster has the easier story. It does not change
what Anthropic's terms say; it does mean the shape we want is not an unreasonable thing to want.
