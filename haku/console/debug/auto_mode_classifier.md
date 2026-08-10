# How Claude Code's auto-mode classifier works, and whether haku-console can reuse the shape

Investigated 2026-08-10, for the idea of haku-console gating its own tool calls ("Haku reached
for `hostexec` when its own k8s token would do") with a cheap model call rather than a rule.

**Source note.** Langfuse was the intended source and was **not** used: this session had no
cluster credentials (the session-start SOPS group decrypt failed for every age recipient, so
no kubeconfig and no Langfuse API key). It turned out not to be needed — the classifier ships
in the Claude Code binary that `claude-agent-sdk` vendors at
`claude_agent_sdk/_bundled/claude` (275 MB, not stripped), so the prompt, the request shape and
the parser are all readable directly. That is **stronger** evidence than a trace: it is the code
that builds the request, not one sample of it. Everything below is quoted from
`claude-agent-sdk==0.2.128`.

## Shape

It is a **two-stage** classifier, both stages against the Messages API with
`querySource: "auto_mode"`.

### Stage 1 — the cheap screen

```js
{model: o, max_tokens: (l === "fast" ? 256 : 64) + L, system: t,
 skipSystemPromptPrefix: true, temperature: POd(), thinking: H,
 messages: [...e, {role: "user", content: te}],
 stop_sequences: [u ? "</severity>" : "</block>"],   // omitted in pure-fast mode
 querySource: "auto_mode", extraBodyParams: …, extraBetas: …}
```

**64 output tokens** in the default two-stage mode — the guess that auto mode uses a
few-token output mode is right, and it is even tighter than expected. The output is one XML
tag, parsed by regex:

- `<block>yes|no</block>` — `RBs()`: `/<block>(yes|no)\b(<\/block>)?/gi`, first match wins.
- `<severity>N</severity>` — `Pi_()`: a 0–100 number, and **exactly one match or it is a parse
  failure**. Used when the model has a severity config.

`stop_sequences` closes the tag, so the model is cut off the moment it has answered.

### Stage 2 — only when stage 1 says "maybe"

Two thresholds per model (`t1`, `t2`, from the `tengu_auto_mode_config` gate): below `t1` →
allow and stop; above `t2` → block outright ("Blocked by fast classifier"); in between →
escalate. Stage 2 is the expensive one:

```js
{model: o, max_tokens: 8192 + L, system: t, skipSystemPromptPrefix: true,
 temperature: POd(), thinking: H, messages: [...], querySource: "auto_mode", …}
```

with `thinking` on, parsed for `<thinking>`, `<block>`, `<reason>`, and
`<category>` (`/<category>([a-z0-9 _-]{1,48})<\/category>/i`, validated against a known set).
`<thinking>` blocks are stripped before the other tags are matched, so reasoning that mentions
`<block>` cannot be misread as the answer.

Fail-closed throughout: a parse failure, an abort, or an unavailable model all return
`shouldBlock: true` ("Auto mode classifier unavailable, denying with retry guidance
(fail closed)").

### The system prompt

`nDo()` returns it, opening:

> You are a security monitor for autonomous AI coding agents.

It splits rules into **HARD BLOCK** ("no user context authorizes these; they cross a security
boundary") and **SOFT BLOCK** ("destructive or irreversible mistakes where user intent, visible
in the transcript, can clear the block"), and spends most of its length on how to read consent
out of a transcript — questions are not consent, scope escalation is autonomous behaviour, the
bar binds at the step that ships. User rules are spliced into `<permissions_template>` slots
(`allow` / `soft_deny` / `hard_deny` / `environment`), so a deployment customises it by adding
rules, not by rewriting the prompt.

## Prefix caching — the part that matters for us

This is the interesting find, and it answers the question directly: **the classifier is built
to be prefix-cacheable across successive actions in one conversation**, by construction.

Two cache breakpoints:

1. **The system prompt** — `vi_()` returns
   `[{type: "text", text: f, cache_control: GEe({ttl: MBs()})}]`. Stable for the session.
2. **The end of the transcript** — the action being judged is pushed as the final block with
   `E.push({type: "text", text: a, cache_control: D})`, `D = GEe({ttl: MBs()})`.

Because the breakpoint sits on the _last_ block, everything before it is written to cache; the
next action in the same conversation appends and reads that prefix back. The transcript is also
deliberately **chunked on tool-call boundaries** rather than concatenated blindly — the
`A += oe` / `b()` flush pair splits a new block at each `tool_use` — which is what keeps byte
boundaries stable as the conversation grows.

`skipSystemPromptPrefix: true` is worth noting too: the classifier does **not** carry the Claude
Code system-prompt preamble. Its system prompt is only the monitor prompt.

## Can haku-console fire these through the Agent SDK?

**Not as a bare Messages call — the SDK has no such surface.** The public API is exactly
`query()` and `ClaudeSDKClient` (`__init__.py` `__all__`), and both spawn the bundled CLI as a
subprocess. There is no client that just posts to `/v1/messages`. `ClaudeAgentOptions` exposes
`model`, `system_prompt`, `max_turns`, `max_thinking_tokens`, `allowed_tools`,
`setting_sources`, `fork_session` and an `extra_args` escape hatch — but **no `max_tokens`**, so
the 64-token screen cannot be reproduced through it. The nearest thing is a one-turn agent with
`max_turns=1`, an empty tool surface, and a prompt that asks for one tag.

So the three options, honestly:

|                                                                                   | Cost per decision                                                        | Prefix caching                                                             | Notes                                                                                                                                                                                                                          |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Long-lived `ClaudeSDKClient`** — one classifier session, `query()` per decision | Whole conversation re-read each turn, but cached; output unbounded by us | Yes — same session, so the CLI's own caching applies to the growing prompt | Closest to what the user described as "a separate gating agent". Keeps context across Haku's tool calls, which is exactly what the `hostexec`-vs-own-token judgment wants. One subprocess, not one per decision.               |
| **`query()` per decision**                                                        | New subprocess **and** a cold prompt every time                          | No                                                                         | The expensive option, and the one to avoid.                                                                                                                                                                                    |
| **Direct HTTP `/v1/messages`**                                                    | Whatever we set — a 64-token screen is reproducible                      | Yes, we place `cache_control` ourselves                                    | Full control, and the only way to copy the two-stage shape faithfully. But it needs a credential of its own; see <litellm_oauth_passthrough.md> for why the subscription OAuth token is not straightforwardly usable this way. |

**Recommendation.** The long-lived `ClaudeSDKClient` is the right first cut: it is the sanctioned
path, it needs no new credential, and it is the option whose weakness (unbounded output) is a
prompt problem rather than an architectural one. Ask for exactly `<block>yes|no</block>` and a
short `<reason>`; the model will not spend 8k tokens answering that. Keep one session per
conversation being gated, so the transcript prefix is warm.

### The classifier does not need the OAuth question answered at all

Worth separating, because the two problems have been running together: **the subscription-OAuth
work in <litellm*oauth_passthrough.md> is about the \_main agent* traffic, and the classifier does
not need it.**

The classifier is a prompt plus one XML tag. That makes it provider-portable — but it does not
have to leave Anthropic to get Langfuse, because **the main LiteLLM already has real Anthropic
and already logs to Langfuse**: the plain `claude-*` entries take `os.environ/ANTHROPIC_API_KEY`
(an ESO mirror of the spend-capped haku-cloud workspace key), and the deployment wires
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` from `langfuse-secrets`. So haku-console can point a
classifier at `litellm.litellm.svc.cluster.local:4000` **today** and get Anthropic models,
Langfuse traces, spend caps, and per-key model allowlists, with no new component and no OAuth.

The economics make this the easy call rather than a compromise. The reason the subscription
matters for Haku's main turns is that they are large and frequent; a stage-1 screen is **64
output tokens** over a cached prefix. Paying API-key rates for that is close to free, and it buys
the thing OAuth cannot: the call shows up in Langfuse like everything else, because it goes
through the proxy that already reports there.

Two caveats before building on it:

- **Placing `cache_control` through LiteLLM is unverified here.** The whole prefix-cache design
  above depends on the breakpoints surviving the proxy. LiteLLM passes Anthropic-native params
  for the `anthropic/` provider, so it very likely works, but measure `cache_read_input_tokens`
  on the second classification rather than assume it.
- **Model allowlists are per-key**, so the console would need its own LiteLLM virtual key scoped
  to whichever model the classifier uses.

If the classifier later wants the subscription anyway — because volume grew, or because it should
share a cache with the main agent — that is the same problem as the main traffic, and it waits on
the same answer.

What to copy from the real thing regardless of transport:

- **Two tiers, not one.** Most calls are obviously fine; only escalate the ambiguous ones. Even
  without `max_tokens`, a cheap screen that emits one tag is far less than a reasoned answer.
- **Fail closed**, with the reason distinguishing "denied" from "classifier unavailable".
- **Strip `<thinking>` before parsing**, and treat "no tag" and "more than one tag" as failures
  rather than guessing.
- **Rules as data spliced into slots**, so adding a rule ("prefer the k8s token over hostexec")
  is a config change, not a prompt rewrite.

## Not established here

- The actual model IDs behind `DBs()` / the Sonnet 5 probe: they resolve through gate config
  (`tengu_auto_mode_config`), so the binary does not name a fixed default.
- Real token/latency numbers. The binary gives the request shape, not what it costs in practice;
  that still wants a Langfuse look once cluster credentials are available in a session.
