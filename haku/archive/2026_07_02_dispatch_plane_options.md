# Dispatch plane — options survey (2026-07-02, settled)

Archived decision history for the Haku multi-agent dispatch plane. These evaluations
picked the design that is now built (dispatcher + k8s Jobs + two-layer LiteLLM + Claude
Code CLI harness); they are kept for the reasoning, not as current plans. The built
system is documented in <../x/dispatch/README.md> and
<../../cluster/k8s/x/haku/dispatch/README.md>; still-forward options (ChatGPT access for the
oai zone, sensors) live in <../plans/multi_agent.md>.

## Dispatch plane

Research verdict (five independent surveys, 2026-07-02): **no existing system ships
"job → automatic classification gate → execute" as a first-class feature** — the one
purpose-built pre-action approval library (HumanLayer) was deprecated in a pivot;
everything else gates by human assignment or post-hoc PR review. But the gate composes
with any plane as a thin dispatcher in front of the submission API, so the plane was
chosen on ops merits alone:

| Option                                                     | Verdict                                                                                                                                                                                                                                                                                                                                          |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **plain k8s Jobs behind the dispatcher** ✅                | Chosen. Zero new components; Job objects are the run record; quotas/TTL/retries built in.                                                                                                                                                                                                                                                        |
| Argo Workflows                                             | The upgrade path if we want DAGs/per-step retries/runs UI: the same dispatcher submits `Workflow` CRs instead (gate stays _in front of_ submission, not inside a template, so it can't be edited out). Argo Events and Kueue: skip (4+ standing pods / admission-quota machinery for what one HTTP call does).                                   |
| Forgejo Actions                                            | Demoted from dispatch plane to CI: `workflow_dispatch` is solid (typed inputs; Forgejo returns run id + jobs on fire), but the runner has no pod-per-job executor (privileged dind only; native k8s backend is prototype-stage 05/2026) — running agent workloads inside CI would mean privileged dind. CI keeps image builds + required checks. |
| Temporal / Windmill / n8n / Prefect / Kestra               | None clears the bar at single-operator scale; Temporal is the most ops for the least incremental value here; Windmill's audit/git features are EE; n8n is JSON-blob workflows (license fair-code). Prefect is the closest call if we ever outgrow CI+Jobs.                                                                                       |
| Centaur (Paradigm)                                         | Closest existing system; evaluated in depth from source — see below. Verdict: don't adopt (no zone axis; it owns the same perimeter layer our Cilium/Kyverno stack owns); steal its durable-execution API design for the dispatcher.                                                                                                             |
| Anthropic Managed Agents (self-hosted)                     | 1:1 architecture match (Anthropic queues sessions; our worker spawns a pod per session) **but the loop runs Anthropic models only** — cannot drive GLM/OpenAI zones. Stays relevant only as a Haku runtime option (Runtime B).                                                                                                                   |
| Vendor clouds (Codex cloud, Jules, Cursor, Devin, Copilot) | All fail self-host and/or our-forge requirements. Reference: Jules is the only vendor with an API-level plan-approval gate (`requirePlanApproval`); Cursor self-hosts only workers while the loop stays in their cloud.                                                                                                                          |

Useful design references: Beads' ready-queue + atomic-claim semantics for a future job
table; `coder/agentapi` (MIT, one Go binary) to drive/observe a CLI harness in a pod
over HTTP+SSE; `kubernetes-sigs/agent-sandbox` CRDs + gVisor (Talos system extension)
as a later isolation upgrade.

## Centaur — deep evaluation (from source)

[Centaur](https://github.com/paradigmxyz/centaur) (Paradigm + Tempo; Apache-2.0 OR MIT;
open-sourced 2026-05 after internal production since 2026-01; release-per-merge cadence,
~800★) is the closest existing system to this plan's shape, so it was read from source
before committing to the bespoke dispatcher.

**What it is** (verified in-repo): a Helm chart deploying a Rust/Axum control plane
(`api-rs`), Postgres (paradedb), a Slack bot (on by default; Teams/Discord/Linear
optional), and the `kubernetes-sigs/agent-sandbox` controller (v0.4.6) — sandboxes are
`Sandbox` CRs with a warm pool (3 pre-booted by default). Each sandbox gets a dedicated
**iron-proxy** MITM pod injected via `HTTPS_PROXY`: the sandbox holds only placeholder
strings; the proxy substitutes real credentials at the wire, bound to specific hosts
(Anthropic/OpenAI/GitHub headers, Bedrock SigV4 re-signing, OAuth brokering — including
Claude-subscription refresh tokens via iron-token-broker — even proxied Postgres DSNs).
Runs on k3s-class clusters; docs include a Mac-Mini/VPS setup.

**The headless path is exactly our contract**: `POST /api/session/{thread_key}` (harness
choice per session: claude-code / codex / amp / pi-mono) → `POST …/execute` with an
`idempotency_key` → SSE `…/events` replayable via `after_event_id` → terminal
`session.execution_completed {result_text}`; all events durable in Postgres. Claude Code
runs as stream-json pass-through with a `CLAUDE_SETTINGS_OVERLAY` deep-merged into
`~/.claude/settings.json` at pod start.

**What fights us** (all verified in source):

1. **The zone axis doesn't exist.** The sandbox namespace is hardwired to the Helm
   release namespace (`SESSION_SANDBOX_K8S_NAMESPACE: {{ .Release.Namespace }}`); the
   sandbox image and env are deployment-global; the session API exposes only
   `harness_type`/`persona_id`. Our core requirement — job → pinned
   namespace/image/secrets/perimeter — means one full Centaur install per zone, or
   forking `api-rs`.
2. **It insists on owning the perimeter.** `api-rs` dynamically creates per-sandbox
   NetworkPolicies and per-sandbox MITM proxies and injects `HTTPS_PROXY` + a proxy CA —
   a competing owner of exactly the layer our CiliumNetworkPolicy + Kyverno-injected
   mitmproxy already occupy. Chaining two MITM proxies/CAs is not a supported path, and
   Kyverno mutations on controller-created pods would race its warm-pool logic.
3. **Credential scoping is chat-principal- or deployment-shaped, not zone-shaped.**
   `ANTHROPIC_BASE_URL` (→ LiteLLM) can only be set deployment-wide; per-zone virtual
   keys don't map. iron-proxy solves the same problem as our LiteLLM virtual keys at a
   different layer (agent holds nothing vs. agent holds a scoped key) — for an
   LLM-only credential surface it duplicates what we already have; its distinctive value
   is wire-level injection of _tool_ credentials (GitHub, Postgres, OAuth'd SaaS), which
   our bounded-write-MCP pattern covers differently.
4. **Chat gravity and churn**: Slack secrets are boot requirements even if unused;
   1Password is the default secret source; 0.1.x API changing weekly; the recent Rust
   rewrite dropped capabilities (issues #621, #683) and docs drift.

**Verdict: don't adopt.** Centaur's unique value concentrates in layers this plan
already solves differently (perimeter → Cilium/Kyverno; credentials → LiteLLM virtual
keys) or deliberately keeps thin (the gate). **Steal for the dispatcher instead**: the
four-endpoint durable-execution API shape, `idempotency_key` on execute,
`after_event_id` event replay, terminal-`result_text` extraction from stream-json, the
settings-overlay config merge, and warm pools if cold-start latency ever matters. That
Centaur builds on `agent-sandbox` also validates those CRDs as our optional substrate.

**Re-checks in the same pass**: `agenttier` is structurally _closer_ to the zone model
(multi-namespace sandboxes; templates-as-profiles with per-template ServiceAccount;
`POST /invoke` SSE) but is bus-factor-1 with no visible adoption (55★) — watch, don't
build on. New single-operator reference found:
[netclode](https://github.com/angristan/netclode) (k3s + Kata microVMs, warm pool,
replayable event stream) — conversation-first, good microVM notes.

## Worker harness

| Option                                             | Verdict                                                                                                                                                                                                       |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Claude Code CLI → LiteLLM (Anthropic shape)** ✅ | v1 for both zones: proven by `z-claude`; one image; `--bare`/headless mode is first-class; per-zone model via virtual key.                                                                                    |
| Runtime C loop (`haku/runtime/agent/`, MAF)        | Feature-complete but undeployed; speaks OpenAI chat shape to LiteLLM, and the GLM entries are registered Anthropic-shape — translation path untested. Long-term provider-agnostic option, not the v1 blocker. |
| Codex CLI                                          | Natural if a zone ever talks to OpenAI directly (native subscription auth, MCP support); the oai zone uses it to speak the Responses shape natively.                                                          |
| `coder/agentapi` wrapper                           | Optional layer for driving/observing whichever CLI runs in the pod; adopt if the dispatcher wants streaming introspection.                                                                                    |
