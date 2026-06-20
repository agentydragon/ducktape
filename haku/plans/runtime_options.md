# Haku runtime options: A / B / C

Status: **design / exploring**. Three candidate runtimes for Haku beyond the v0
Claude Code web home. This is the "which runtime" comparison; the detailed design
of Runtime B is in [managed_agents.md](managed_agents.md) (+ its
[artifact drafts](managed_agents_artifacts.md)). Delete or tombstone once the
runtime is chosen.

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

- **Pros:** ~zero ops, the mature Claude Code harness, the Console session view.
- **Cons:** Anthropic-managed container **only** (no BYOC); full lock-in (loop +
  infra + model all Anthropic); in-cluster access only via the public
  `kubeapi.allegedly.works` proxy (as today); subscription/seat billing.

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

- **C** if provider-agnosticism and reusing the existing LiteLLM/Langfuse is the
  priority (it is, per the lock-in concern) — framework = **Pydantic AI**, or
  **Dapr Agents** if crash-survivable k8s wakeups dominate.
- **B** if you'd rather not maintain a loop and will accept Anthropic
  model-lock + API rates in exchange for hosted infra, vaults, and the Console.
- **A** is the zero-ops / manual stopgap (≈ today plus triggers).

Haku's `haku-state` git memory means the warm "wake session" is an
**optimization** in every runtime — losing it just re-orients from `haku-state` —
so C's "re-instantiate per wake" is both cheap and robust, and the choice between
B and C is really hosted-convenience-with-lock-in vs. self-hosted-and-portable.
