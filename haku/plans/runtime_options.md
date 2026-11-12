# Haku runtime options: A / B / C

Status: **Runtime A is the primary live runtime for now.** Haku currently runs
as manually configured Claude Code web routines. Runtime A stays primary despite
real drawbacks — full Anthropic lock-in and the options it forecloses (see
below). Runtimes B and C remain experiments at different levels of completeness,
not production replacements. This is the "which runtime" comparison; the
remaining Runtime B design is in [managed_agents.md](managed_agents.md).

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
  access through the operator's Claude subscription, subject to the applicable
  plan and terms.
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
passthrough. The harness remains Anthropic-request-shaped (prompt-caching
fidelity and beta parameter shapes), so routing to another provider can be lossy.
Using this harness keeps an Anthropic loop dependency even if the infrastructure
is self-hosted; credential support and billing require separate validation.

The 2026-07-31 Kubernetes compatibility spike passed: a `claude setup-token`
credential authenticated Agent SDK 0.1.48/Claude CLI 2.1.71 headlessly through
Haku's forced TLS-intercepting proxy, with streaming, multi-turn state,
same-pod disk resume, transcripts, and Python hooks. This resolves technical
feasibility, not the credential-boundary or model-lock-in tradeoffs above. The
runtime was then built, run briefly, and retired; its lessons are in
<../docs/agent_sdk_runtime.md>.

**Authentication and observability are open decisions.** The 2026-07-31 spike
showed that headless subscription authentication worked at that time; it did not
establish that the same credential arrangement is supported for every deployment.
Anthropic's [authentication policy](https://code.claude.com/docs/en/legal-and-compliance)
distinguishes individual use from third-party products and advises developers using
the Agent SDK to use API keys. Before adopting either the direct SDK path or a
proxy, check the current policy and confirm the intended use with Anthropic where
needed. Do not treat the old experiment as authorization or a billing guarantee.

Running the loop where Haku has CRUD access also puts any credential held there in
reach of the agent. Design credential isolation and revocation explicitly; see
<../docs/agent_sdk_runtime.md> for the retired experiment's lessons.

Claude Code's native telemetry can emit metrics and traces when configured; the
July 2026 delta-temporality issue was fixed for that setup (see
<../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>).
This does not establish complete tool-call or worker traces for a new runtime.
Verify trace contents, attribution, access and retention for the chosen deployment.

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

The experimental implementation uses Microsoft Agent Framework in
<../runtime/x/agent_framework/README.md>. Alternative Pydantic AI drafts are in
<runtime_c_artifacts.md>.

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

## Billing and credential decision

Compare the actual credential, provider, pricing and terms for each candidate at
adoption time. The first-party web routine, a self-hosted Agent SDK loop and
Managed Agents have different authentication and billing paths; neither a past
subscription-authentication experiment nor provider-agnostic model routing proves
that a given combination is authorized or covered by a subscription. Runtime C
can route to several providers but needs its own credentials and per-provider cost
controls. Keep credential isolation and trace-data handling in the comparison.

## Comparison

| Axis                         | A: Claude Code web routine  | B: Managed Agents (self-hosted) | C: provider-agnostic loop                |
| ---------------------------- | --------------------------- | ------------------------------- | ---------------------------------------- |
| Runs the loop                | Anthropic                   | Anthropic                       | you (framework)                          |
| Tools execute                | Anthropic container         | your worker (`haku-sandbox`)    | your process (`haku-sandbox`)            |
| Model providers              | Anthropic only              | Anthropic only                  | any (LiteLLM: Anthropic/OpenAI/GLM)      |
| Provider lock-in             | full                        | model-locked                    | none                                     |
| Billing                      | verify plan and terms       | verify API and runtime pricing  | per-provider; verify pricing             |
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
