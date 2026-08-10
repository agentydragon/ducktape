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

**Confirmed from the other end.** Langfuse holds 35,816 generations; the 120 most recent are
`gpt-5.6-terra` (102, Codex via CLIProxyAPI), `gpt-oss:20b` (17, local) and one embedding —
**not one Anthropic model**. Nothing that reaches Anthropic on the subscription is visible there
today, which is exactly the gap this note is about.

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

That is the template. If CLIProxyAPI (or a small shim of our own) can hold a _Claude_
subscription session the same way it holds the Codex one, LiteLLM needs no new adapter at all —
it just gets another `anthropic/`-provider entry pointing at a cluster-local base URL. Whether
CLIProxyAPI supports Claude subscription login is **the one thing to check first**; its own
docs, not ours, are the source for that.

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

Nothing was built. The user asked for a LiteLLM backend "if there is something we can use
easily", and on the evidence there is not yet: it depends on whether CLIProxyAPI can carry a
Claude subscription, which is unverified. Sequence before writing any manifest:

1. Check whether CLIProxyAPI supports Claude/Anthropic subscription login (it already does Codex,
   Gemini and others; Claude is plausible but unconfirmed here).
2. If yes → a `cluster/k8s/x/` deployment mirroring `cli-proxy-api`, plus one `anthropic/` entry
   in the main LiteLLM pointing at it, and the sandbox's `ANTHROPIC_BASE_URL` moved to LiteLLM.
   The egress-proxy substitution then becomes unnecessary for this path, which is a real
   simplification — one fewer place the real token lives.
3. If no → compare a small OAuth-forwarding shim against a Langfuse transform in the egress proxy
   before choosing.

On the ToS point: the user's read is that logging-only interception is within the spirit, and
nothing here changes what is sent to Anthropic. Worth keeping that property explicit in whatever
lands — a shim that rewrites bodies would be a different question from one that copies them.
