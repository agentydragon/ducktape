# Haku runtime options: A / B / C

Status: **Runtime A is the primary live runtime for now.** Haku currently runs
as manually configured Claude Code web routines. Runtime A stays primary despite
real drawbacks — full Anthropic lock-in and the options it forecloses (see
below). Runtimes B and C remain experiments at different levels of completeness,
not production replacements. This is the "which runtime" comparison; the
detailed design of Runtime B is in [managed_agents.md](managed_agents.md) (+ its
[artifact drafts](managed_agents_artifacts.md)).

## The two layers of lock-in

Lock-in has two independent layers: **who runs the agent loop**, and **which
model provider you pay**. They're separable, and the second is already solved —
the in-cluster **LiteLLM** proxy decouples the model layer (one endpoint →
Anthropic / OpenAI / Z.AI-GLM, with budgets, kill-switch, and Langfuse traces).
So the runtimes differ mostly in the **loop layer**, and "no provider lock-in"
means: self-host the loop **and** route models through LiteLLM.

## Runtime A — Claude Code web routine

Anthropic-managed Claude Code in the cloud; **routines** fire on a schedule, a
GitHub event, or an API call. This is essentially v0-plus-triggers (today Haku is
a manual web session).

- **Pros:** ~zero ops, the mature Claude Code harness, the Console session view;
  **flat subscription billing** — runs on the operator's Claude subscription
  rather than metered API rates.
- **Cons:** Anthropic-managed container **only** (no BYOC); full lock-in (loop +
  infra + model all Anthropic); in-cluster access only via the public
  `kubeapi.allegedly.works` proxy (as today).

### Runtime A variant — self-hosted Claude Code (Agent SDK)

The **Claude Agent SDK** (`claude-agent-sdk`, formerly `claude-code-sdk`) is a
client-side driver that subprocess-spawns the `claude` CLI binary: the same
Claude Code harness as A, but **the loop runs in your process**, so it could run
in `haku-sandbox` (BYOC) rather than Anthropic's cloud. It is **not** Managed
Agents (B) — the Agent SDK runs the loop locally around the CLI, whereas B runs
the loop server-side at Anthropic. The CLI honors `ANTHROPIC_BASE_URL` and
`ANTHROPIC_AUTH_TOKEN`, so its model leg can point at LiteLLM's Anthropic-format
passthrough — but the harness stays Anthropic-request-shaped (prompt-caching
fidelity, beta param shapes, OAuth/subscription auth), so routing to a
non-Anthropic provider through LiteLLM is lossy and the subscription benefit
doesn't survive. Net: it collapses to "A's harness in front of LiteLLM →
Anthropic" — self-hosted infra, same model lock-in — which is why it's a variant
of A, not a peer of C.

**Amendment (2026-07-31).** The above conflates two separable choices —
self-hosting the loop, and routing the model leg through LiteLLM. Only the second
kills the subscription:

- **Agent SDK → LiteLLM → Anthropic.** As described: subscription benefit lost,
  because LiteLLM terminates and re-emits the request, so what reaches Anthropic is
  not a Claude Code request. It is also **blocked** — consumer OAuth tokens are
  rejected outside Claude Code with `This credential is only authorized for use with
Claude Code`, and Anthropic's [legal-and-compliance
  doc](https://code.claude.com/docs/en/legal-and-compliance) scopes OAuth to
  "ordinary use of Claude Code and other native Anthropic applications".
- **Agent SDK → Anthropic directly.** Keeps flat subscription billing: the same doc
  puts advertised Pro/Max limits on "ordinary, individual usage of Claude Code **and
  the Agent SDK**". This buys BYOC — the loop inside the perimeter, in-cluster access
  without Runtime A's public `kubeapi.allegedly.works` hop — while staying on the
  subscription. It is the option this section didn't separate out.

Two things to weigh before adopting it:

- **It inverts a deliberate credential boundary.** Today the launch credential lives
  in `haku-console`, a namespace Haku has no RBAC into — see <../console/README.md>
  ("the confidentiality boundary that lets the console hold secrets Haku may not
  read"). Running the loop in `haku-sandbox` puts the subscription OAuth token where
  Haku has full CRUD. Blast radius is worse than a LiteLLM virtual key: misuse is
  enforceable against the **personal Anthropic account**, without prior notice, and
  there is no per-lane Terraform kill switch. Mitigations exist (loop in a third
  namespace; Agent SDK with built-in tools disabled, reaching `haku-sandbox` only
  through an MCP server) but they are design work, not defaults.
- **The observability motivation is gone.** Wanting traces/metrics out of Haku was a
  main driver for moving off Runtime A. That gap was a monitoring-stack bug, not a
  runtime limitation — see
  <../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>.
  Runtime A emits all three signals once the env var is set on the web environment.

## Runtime B — Anthropic Managed Agents (self-hosted sandbox)

Loop at Anthropic; tools execute in **your** worker in `haku-sandbox`; **vaults**
handle MCP auth; one long-lived session woken by events; Console session trace.
Full detail: [managed_agents.md](managed_agents.md).

- **Pros:** hosted loop (nothing to maintain); vaults remove the
  `client_credentials` MCP-auth spike; BYOC keeps tools/data/egress in-cluster;
  Console UI.
- **Cons:** Anthropic **API-rate** billing (the cost gripe); model is
  effectively Anthropic-only (the harness assumes Anthropic platform surfaces —
  see below); gives up in-cluster LiteLLM/Langfuse attribution; you run a worker
  fleet.

## Runtime C — provider-agnostic self-hosted loop

**You own the loop** (an off-the-shelf neutral framework), route models through
in-cluster LiteLLM → {Anthropic, OpenAI, Z.AI/GLM}, do MCP auth yourself, and
send traces to your existing Langfuse. This is essentially the PLAN's original
self-hosted vision (LiteLLM + Langfuse were always in it), with a multi-provider
framework as the loop instead of Claude Code.

- **Pros:** no provider lock-in (per-call provider choice; cheap GLM coding plans
  answer the API-rate gripe); restores LiteLLM/Langfuse attribution; fully
  self-hosted including the loop.
- **Cons:** you own the loop (tools, context management, retries, MCP plumbing —
  though the frameworks give most of this); no vaults (MCP auth is yours); no
  Console (lean on Langfuse).

Framework shortlist, all on the LiteLLM keystone:

- **Pydantic AI** — cleanest fit for this repo (typed Python, FastAPI-native,
  MCP client, native OTel→Langfuse, DBOS/Temporal durable-exec options).
- **Dapr Agents** — `DurableAgent` on Dapr Workflows: checkpointed, **crash- and
  restart-survivable** wakeups; k8s-native. Best if durable wakeups dominate.
  (Provider wiring for Anthropic/GLM goes via `DaprChatClient`/components —
  verify.)
- **LangGraph or Microsoft Agent Framework** — durable, resumable sessions
  (checkpointer/threads), heavier abstraction.

Concrete drafts (Pydantic AI agent + supervisor + k8s wiring):
[runtime_c_artifacts.md](runtime_c_artifacts.md).

## What Runtime C gives up — the Anthropic surfaces

The Claude harness isn't a thin `/v1/messages` client; it assumes Anthropic
platform surfaces a neutral framework (and LiteLLM) don't reimplement:
server-side tools (web search/fetch/code-execution), the Files API, Batches,
`count_tokens`, prompt-caching fidelity/portability, subscription/OAuth auth, and
the beta param shapes (effort, compaction, task budgets, fallbacks). **For Haku
this barely matters** — its tools are all client-side already (bash + the MCP
servers), so it never needed Anthropic's server tools, Files, or batches. Prompt
caching still works on the Anthropic _leg_ (framework → LiteLLM → Anthropic
honors `cache_control`); it just isn't portable across OpenAI/GLM.

## The subscription paradox

The Max/Pro **subscription** benefit and **provider-agnosticism** are mutually
exclusive within one runtime: the subscription is Anthropic OAuth + Anthropic
inference, i.e. itself a form of lock-in. So it's either "cheap via the Max plan,
Anthropic-locked" (A/B) **or** "provider-agnostic, pay each provider directly,
incl. cheap GLM" (C). You can run both as separate Haku runtimes; you can't
collapse them into one.

**Beyond billing mode, there is a coverage nuance** (policy/ToS, not technical).
Running Haku as a **Claude Code web routine (A) is fine — routines are a first-party
feature of the product**, i.e. a supported use of the subscription; scheduling one is
not "programmatic use" in the ToS sense. The constraint is on the A-variant: driving the
subscription-authed `claude` CLI from a **self-hosted Agent-SDK loop**. The Agent SDK's own
documentation says Anthropic "does not allow third party developers to offer claude.ai login
or rate limits for their products, including agents built on the Claude Agent SDK", and
directs callers to API-key auth; `CLAUDE_CODE_OAUTH_TOKEN` appears nowhere in the SDK's
documented auth surface. That note addresses _offering_ subscription login to others rather
than single-operator personal use, so it is not decisively on point — but it is a good deal
more pointed than a grey area, and the instruction that follows is unqualified. Settle it
before building on the A-variant; see
[agent_sdk_sandbox_runtime.md](agent_sdk_sandbox_runtime.md). Managed Agents (B) bill **API rates
regardless**, so they never draw on the subscription in the first place. Net:
A-as-routine is defensible; the caution is specifically about self-hosted
subscription-CLI automation, and it is one more reason C (pay each provider directly,
no subscription entanglement) is clean if Haku's automation grows beyond routines.

## Comparison

| Axis                         | A: Claude Code web routine  | B: Managed Agents (self-hosted) | C: provider-agnostic loop                |
| ---------------------------- | --------------------------- | ------------------------------- | ---------------------------------------- |
| Runs the loop                | Anthropic                   | Anthropic                       | you (framework)                          |
| Tools execute                | Anthropic container         | your worker (`haku-sandbox`)    | your process (`haku-sandbox`)            |
| Model providers              | Anthropic only              | Anthropic only                  | any (LiteLLM: Anthropic/OpenAI/GLM)      |
| Provider lock-in             | full                        | model-locked                    | none                                     |
| Billing                      | subscription / seats        | API rates + session runtime     | per-provider (cheap GLM option)          |
| In-cluster access            | via `kubeapi` proxy         | direct                          | direct                                   |
| MCP auth                     | native                      | vaults (managed)                | you wire it                              |
| Triggers                     | routines (sched/GitHub/API) | you wire (webhook → session)    | you wire (webhook → loop)                |
| Wakeup model                 | new session per routine     | long-lived session + events     | framework persistence / re-init from git |
| Observability                | Console                     | Console                         | Langfuse (existing)                      |
| LiteLLM/Langfuse attribution | no                          | no                              | yes                                      |
| Ops burden                   | ~none                       | run a worker fleet              | run loop + worker                        |

## Recommendation

- **A** remains the live primary runtime until another runtime proves enough
  operational value to replace it.
- **C** is the preferred replacement direction if provider-agnosticism and
  reusing the existing LiteLLM/Langfuse are worth the loop-maintenance cost:
  framework = **Pydantic AI**, or **Dapr Agents** if crash-survivable k8s
  wakeups dominate.
- **B** remains an option if hosted loop infrastructure, vaults, and the Console
  are worth Anthropic model-lock + API rates.

Haku's `haku-state` git memory means the warm "wake session" is an
**optimization** in every runtime — losing it just re-orients from `haku-state` —
so C's "re-instantiate per wake" is both cheap and robust, and the choice between
B and C is really hosted-convenience-with-lock-in vs. self-hosted-and-portable.
