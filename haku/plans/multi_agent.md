# Haku multi-agent architecture — remaining work

**This doc is forward-looking.** The dispatch plane is built and live: a Haku
orchestrator (Anthropic, full perimeter) dispatching well-scoped jobs to cheaper,
lower-trust worker **zones**. What is built is documented where the code lives — this
plan holds only what is **not yet built**.

Built, documented elsewhere:

- Dispatcher service + worker image: <../dispatch/README.md>
- Cluster wiring (workers-LiteLLM, CNPG, the three-hop key chain): <../../cluster/k8s/haku/dispatch/README.md>
- Zone perimeters and the trust model: <../../cluster/k8s/haku/zones/README.md>
- Security contract (enforcement inventory): <../docs/security.md>
- Settled options survey (dispatch plane, Centaur deep-eval, harness): <../archive/2026_07_02_dispatch_plane_options.md>

## Where it stands

Live: the **zai zone** (z.ai GLM, public-by-construction prompts only, Claude Code CLI
harness). First production jobs run end to end — `POST /jobs` from `haku-sandbox` →
credential lint → Anthropic classifier → per-job key mint → k8s Job in `haku-sandbox-zai`
→ result read back through the `haku_reader` role. Built across build-order steps 1–4 (PRs
[#2739](https://github.com/agentydragon/ducktape/pull/2739),
[#2748](https://github.com/agentydragon/ducktape/pull/2748),
[#2754](https://github.com/agentydragon/ducktape/pull/2754), plus rollout fixes).

## Goal (unchanged)

- **Haku, the orchestrator** keeps all judgment: synthesis, item authoring, operator-model
  updates, anything reading Gmail/Drive/Plaid/Tana content. In-session subagents share its
  trust — parallelism, not a separate trust level.
- **Cheap worker zones** take scoped jobs Haku dispatches, holding a strict subset of its
  privileges and capped budgets, so Haku may dispatch autonomously.
- **Sensor agents**: prompt-less, one-way watchers that only send findings _to_ Haku.
- Net effect: shrink the Anthropic metered overage (~$2–3k/mo) by routing grunt work to
  ~$0.4/M-token GLM and flat-rate OpenAI subscription inference.

## Remaining build order

5. **`haku-sandbox-oai`** — one more perimeter namespace + a CNP entry on the
   workers-LiteLLM + chatgpt models in its config; same worker image, Codex CLI harness.
   Details below.
6. **Sensors + affordances** — changedetection.io + webhook→intake; Forgejo ducktape
   mirror automation + `haku` PR rights; base doctrine amendments. Details below.

Deferred: grocery-order bounded-write MCP; PII check as a required CI status on PRs.

New local-inference follow-up: <local_dispatch_zone.md>. It adapts the existing zone
perimeter to Ollama-hosted models and adds an active-model scheduler so local workers do
not thrash model residency by running agents across multiple models at once.

New application follow-up: <kitchen_stocking_subagent.md>. Fleshes out the grocery-order
bounded-write MCP line above into a first design pass for a kitchen/household-stocking
subagent (operator, 2026-07-11) — a candidate first real workload for the oai or local zone,
using the existing `grocy_mcp/eval` harness (already model-agnostic) to pick the tier.

## The oai zone (step 5)

The middle trust level the two-level design lacked: OpenAI, subscription-billed, trusted
between z.ai and Anthropic. It takes **moderate personal context in curated prompts**
(project/calendar-shaped facts, coarse finances) but **no** credentials, figures,
identifiers, documents, or health data. The exact prompt line is a values call for the
operator — seed conservatively and tune from feedback (see _Open questions_).

Typical work: harder coding GLM botches (instead of escalating to Haku), research needing
mild personal framing, and a judge/verifier zone reviewing zai-zone output before it
reaches Haku (cross-provider diversity is a feature).

### OpenAI subscription access

Premise verified 2026-07-02: OpenAI de-facto tolerates subscription credentials outside
the official Codex harness (public endorsement by OpenAI's Head of DevEx; opencode ships
native ChatGPT sign-in; no bans for personal proxy use). Community line: one subscription,
your own workers, no pooling/reselling. Mechanism shared by all options: Codex CLI's OAuth
(`auth.json` with auto-refresh; ~8-day client refresh cadence; quota in
`x-codex-primary-*`/`x-codex-secondary-*` headers).

| Option                                   | Notes                                                                                                                                                                                                                                                  |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **LiteLLM native `chatgpt` provider** ✅ | First choice: zero new components; credential stays in LiteLLM; virtual keys give attribution/budget/kill-switch. The provider is already wired in `cluster/k8s/litellm/` (auth-seed + PVC; three Codex-backend models, **streaming-only**).           |
| CLIProxyAPI                              | Fallback upstream behind LiteLLM if the `chatgpt` client trips Cloudflare (TLS-fingerprint class, environment-dependent — test from cluster egress): most maintained (~39k★), Go/Docker/headless, multi-account. Large surface — pin, restrict egress. |
| icebear/codex-proxy                      | If we specifically want an Anthropic-compatible `/v1/messages` surface for codex models + per-account quota reporting.                                                                                                                                 |
| ChatMock                                 | Simplest single-account fallback; slower-moving.                                                                                                                                                                                                       |

The oai zone's harness is **Codex CLI** precisely because it speaks the Responses shape
natively — the `chatgpt` provider's open bug lives in the chat-completions↔responses
bridge, avoided by construction. Codex points at the workers-LiteLLM via a `config.toml`
`[model_providers]` entry.

Operational: persist the auth dir on a PVC and alert on refresh failure; the 5h + weekly
windows are the real capacity constraint (Plus is thin for fleet use — Pro or a higher
tier if this becomes a real zone). Dispatch should be **quota-aware** — extend `aiquota`
with an OpenAI provider next to the z.ai one, check remaining quota before creating a Job,
and queue or re-route (zai→oai→defer) rather than fail.

## Sensors (step 6)

Prompt-less, one-way watchers: they take no input from Haku and only emit findings, so
they run at the lowest trust level and their output is untrusted input to Haku (which
already treats all source data as adversarial).

| Option                                   | Verdict                                                                                                                                                                                                                                                                 |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **changedetection.io ≥0.55.1** ✅        | Mature, Apache-2.0, evaluates plain-English conditions per watch via LiteLLM ("notify only when the price drops below $50"), fires Apprise webhooks with diff payloads. One Deployment + `sockpuppetbrowser` sidecar for JS pages. LLM features are new — expect churn. |
| firecrawl-observer                       | Cleanest small reference (diff → LLM meaningfulness score → threshold → webhook) but Convex/Firecrawl-SaaS-tied.                                                                                                                                                        |
| Bespoke git-scrape + CI cron + LLM judge | Well-precedented; reserve for watches where diff history itself is valuable. Forgejo Actions `schedule:` works (DST quirk; no GitHub-style 60-day auto-disable).                                                                                                        |
| Skyvern / browser-use                    | Heavyweight browser agents; only if a watch needs real interaction.                                                                                                                                                                                                     |

The judging model routes through our LiteLLM, so even sensor judgment stays under our
control. Findings land in `haku-state` `intake/` via webhook. Bespoke sensor agents only
where judgment is richer than a page-diff condition.

## Remaining monitoring affordances

Key containment (per-job keys, CNP reachability) and k8s object status (via
`cluster-diagnostics-reader`) are **built** — see the cluster README and security doc.
Still to wire:

- **Langfuse `haku-workers` project + viewer key**: route the workers-LiteLLM's traffic to
  a dedicated Langfuse project (key alias = job id → per-job attribution) and reflect a
  viewer-scoped key into `haku-sandbox`. Worker traffic is non-sensitive by construction
  (gated prompts), so Haku reading its own prompts/completions/costs is fine; a shared
  all-of-LiteLLM project would expose other consumers. The trace is the highest-value
  "stuck/looping/burning budget" signal.
- **Raw worker logs — pre-approved in principle, skipped at v1** (operator, 2026-07-02).
  Traces + status/events + the result blob cover v1; the case they answer poorly is
  "harness stuck vs. waiting on a long job." If that need materializes, grant Haku
  `pods/log` in the zone namespaces — one `RoleBinding` per zone on the
  `logs-configmaps-reader` pattern, no code. Safe under the zone-LiteLLM design: the only
  credential that can appear in a worker log is a per-job key, unusable from Haku's
  namespace by CNP.

## Doctrine amendments still to make

**Base amendments** (edit `haku/base/instructions.md` when building — these are Haku's own
rules, not enforced code):

1. **Autonomous dispatch rule**: Haku may dispatch work to principals whose privileges are
   a strict subset of its own and whose spend is capped by construction; operator-gated
   launch remains required at or above Haku's own privilege.
2. **Per-zone prompt floor**: enumerated categories that never enter a worker-zone prompt
   (credentials, account numbers, figures, identifiers, documents, health), with the live
   rubric in state.
3. **PR authorship**: record Forgejo-ducktape PRs as a sanctioned bounded write.
4. **Found-credential rule**: Haku never uses a credential found in worker output; finding
   one means reporting it as compromised so it gets rotated.

**Prerequisites for Haku's ducktape PRs**: the cluster-Forgejo ducktape mirror must become
an automated mirror (today bumped manually — see
`haku/runtime/managed_agent/self_hosted/README.md`), and the `haku` Forgejo user needs
fork/branch/PR rights. A PII/classifier check can run as a required status check on
worker- and Haku-authored PRs, as belt to the dispatch gate's suspenders.

**Ownership split** (the principle the amendments encode): enforcement lives in ducktape
(dispatcher, classifier prompt, worker images, Job templates, perimeters, LiteLLM
config/keys — Haku proposes via PR, cannot self-modify, so it can't bypass its own gate);
judgment lives in `haku-state` (routing policy, per-model capability calibration, the
delegation register as a dispatch queue, outcome/cost feedback from Langfuse).

## Routing for not-yet-built zones

- **oai zone**: harder coding GLM botches; research needing mild personal framing; a
  judge/verifier reviewing zai-zone output before it reaches Haku.
- **Never in a worker zone**: anything reading raw Gmail/Drive/Plaid/Tana content;
  haku-state work (the repo is the personal-data motherlode — consider splitting `ui/`
  into its own repo before offloading frontend chores); final synthesis and
  operator-facing voice.
- **Grocery orders** (future): a bounded-write pattern — a small
  reviewed MCP server holding the vendor credential server-side, exposing only bounded ops
  (`add_to_cart`, `submit_order` under a $-ceiling against a merchant allowlist). Once that
  exists, even an untrusted worker can operate it safely. Generalization: **bounded-write
  MCP servers are how low-trust agents get safe write capabilities.**

(The live zai zone's routing is operational and lives in Haku's state runbook, not here.)

## Open questions (operator)

- Which ChatGPT plan carries the oai-zone load (Plus windows are thin for fleet use)?
- Exact oai-zone prompt line — is "employer + rough equity situation" acceptable in an
  OpenAI research prompt, or does anything financial stay with Haku?
- When (if ever) to split `ui/` out of haku-state to make UI chores offloadable.

## References

Forward-relevant external sources (full session research, 2026-07-02, is in the archived
options survey): LiteLLM `chatgpt` provider
([docs](https://docs.litellm.ai/docs/providers/chatgpt),
[PR #19030](https://github.com/BerriAI/litellm/pull/19030); open bugs
[#27175](https://github.com/BerriAI/litellm/issues/27175),
[#25429](https://github.com/BerriAI/litellm/issues/25429)); Codex auth/token lifecycle
([openai/codex](https://github.com/openai/codex));
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI);
[icebear/codex-proxy](https://github.com/icebear0828/codex-proxy);
[ChatMock](https://github.com/RayBytes/ChatMock);
[changedetection.io](https://github.com/dgtlmoon/changedetection.io) (LLM rulesets since
0.55.1); [firecrawl-observer](https://github.com/firecrawl/firecrawl-observer).
