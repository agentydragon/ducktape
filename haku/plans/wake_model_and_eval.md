# Haku wake model, provider silos, logging, and eval

Status: **design note** (2026-07-05, from an operator thinking-out-loud session).
Companion to [runtime_options.md](runtime_options.md), which compares **who runs the
loop**; this note covers **when the loop runs** (wake model), **which provider sees
which data** (sensitivity silos), and **how we'd know any of it is better** (logging +
eval). Delete or fold into the runtime decision once one is committed to.

## Wake model: long-lived thread vs fresh session per run

The intuition "a long-lived agent should be one long-lived context that events arrive
into" is mostly dissolved by cache economics plus the existing git-state design:

- **Prompt-cache TTLs are minutes, not hours** (verified against Anthropic docs
  2026-07-05): Anthropic ephemeral cache is 5 min TTL at 1.25× write / 0.1× read, or 1 h
  TTL at 2× write; strict prefix match, model-scoped. OpenAI's automatic caching has
  similar minutes-scale retention. **Any wake cadence measured in hours gets zero cache
  reuse under either wake model.**
- So a long-lived thread is not cheaper — it is a **growing prefix re-billed at full
  input price on every wake**, degraded by lossy auto-compaction as it grows. A fresh
  run that re-orients from `haku-state` keeps the prefix short and _curated_:
  `memory/` is effectively a durable, provider-portable, hand-gardened cache. (This is
  runtime_options.md's "the warm wake session is an optimization" point, now with
  numbers.)
- What a long thread _actually_ buys: (a) **event latency** — events processed on
  arrival rather than at the next cron tick; (b) **no re-orientation cost** per wake;
  (c) in-context nuance not yet written to memory. (a) is orthogonal — event-driven
  triggering works equally well firing fresh sessions. (b) is an empirical number we
  can't currently measure (see Logging). (c) is a write-it-down discipline question.
- **No harness hacking is needed** to trial the long-lived shape on subscription auth:
  Claude Code web triggers can fire **into an existing session**, resuming the same
  conversation (self-bind or `persistent_session_id`), or spawn fresh sessions —
  observed live via the claude-code-remote MCP (`create_trigger`, `send_later`) in a
  2026-07-05 session. Caveats: the container is still ephemeral (bootstrap re-runs each
  wake; only the conversation persists), auto-compaction of long context is lossy, and
  cron triggers are hourly-minimum (on-demand `fire` covers event wiring).

**Recommendation:** keep fresh-per-run as the baseline. Invest in **event→fire wiring**
(mailbox/webhook → routine fire; sensors per [multi_agent.md](multi_agent.md) step 6),
which improves freshness under _both_ wake models and is likely the bigger freshness
lever than thread shape. Trial a persistent-session arm only after baseline metrics
exist (see Eval).

## Sensitivity silos (provider trust tiers)

Operator position (2026-07-05): `haku-state` is a hub of arbitrarily sensitive personal
data; provider trust ordering is **Anthropic > OpenAI > others**, with GLM/Z.AI treated
as public-data-only, and the local 5090s most trusted but least capable.

- **The mechanism already exists — don't build a second one.** The dispatch plane's
  credential lint + classifier gate (<../dispatch/README.md>) _is_ the silo gate; the
  zai zone is the "public-by-construction" bucket, and the planned oai zone
  ([multi_agent.md](multi_agent.md) step 5) is the middle tier. The generalization is
  policy, not plumbing: name the **data classes** (roughly: public / infra /
  coarse-personal / sensitive — figures, identifiers, documents, health, credentials)
  and give each zone a class ceiling encoded in its classifier prompt. "This bucket
  must not contain account numbers or amounts" is exactly the contract the zai
  classifier enforces today.
- **The main brain must read everything, so it runs only at the top trust tier**
  (Anthropic, today). Moving the orchestrator to OpenAI/Codex would send the whole
  firehose to the less-trusted provider — inconsistent with the ordering that motivated
  silos in the first place. The already-bound Codex token's place is the **oai worker
  zone**, not the main brain. Cost pressure on the Anthropic side is addressed by
  pushing grunt work down the tiers (the standing multi_agent goal), not by relocating
  judgment.
- **Local 5090s = a future `local` zone with the highest data ceiling and the lowest
  capability.** Best first jobs: host the **classifier gate itself** (removes the
  Anthropic call from the dispatch admission path and keeps rejected prompts fully
  local), embeddings/recall over `haku-state`, recurring cheap labeling. Already
  "deferred" in multi_agent.md; this defines its niche.

## Logging — the prerequisite for every decision above

There is currently **no per-run telemetry** from Claude Code web routine runs: no
runs-listing API (see `haku/PLAN.md`), and the Console session page is view-only.
Langfuse only sees LiteLLM-routed traffic (dispatch workers), never subscription-auth
runs. But **inside** the session the full transcript sits at
`~/.claude/projects/<slug>/<session-id>.jsonl`, including per-request token usage.

Plan:

- **Capture at run close-out** (extend `haku/run.md`'s manifest step + a `tools/`
  helper in state; checkpoint during long runs): copy the transcript plus a distilled
  metrics summary — tokens in/out/cache-read/cache-write, tool-call counts and
  latencies, orientation-vs-work split, wall time — into a store.
- **Store: a separate private `haku-logs` Forgejo repo**, not `haku-state`. State is a
  curated brief, not a telemetry archive; transcripts embed raw source data (email
  bodies read during the run) so the log store inherits the same sensitivity class
  (operator + haku only). The run manifest in `runs/` gets a summary row + pointer.
- **Metrics to stand up**: cost per run; **orientation share** (tokens spent before
  first new observation — the number that decides the wake-model question);
  **event→surface latency** (source-event timestamp vs item commit timestamp — git
  history already holds the commit half); staleness incidents (state's
  `memory/lessons/` already records these).

## Eval

- **Frozen simulated eval — skip.** Emulating every tool plus the internet is not
  worth it for a personal system.
- **Retrospective judge eval is nearly free**: `haku-state` git history _is_ a replay
  log. For any window, an LLM judge can diff what Haku surfaced against what the event
  stream contained and score usefulness / staleness / coverage. Works on the live
  system and on any future variant over the period it actually ran — no emulation.
- **Live A/B (long-thread vs 6h-fresh)** is viable as stage 2: two `haku-state` forks,
  two routines (persistent-session arm via trigger self-bind; a Sonnet-on-subscription
  arm is plausible), base pin and `ui/` **frozen** for the window (the co-created UI is
  otherwise a confound). Sources are read-only so arms don't interfere, with two
  exceptions to handle: Gmail labeling writes (disable on one arm) and the operator
  only interacting with one UI (compare sampled UI snapshots with the judge + operator
  spot-checks, rather than relying on click-stream parity). Decide **after** 1–2 weeks
  of baseline metrics — if orientation share is small and staleness is dominated by
  trigger latency, the A/B answers a question the numbers already settled.

## Order of work

1. **Restore sight**: the expired `google-access-token` (Gmail/Calendar/Drive blind
   since Jul 3) and the Forgejo registry 502 blocking haku-ui rollout — measuring a
   half-blind Haku measures nothing.
2. **Transcript + metrics capture** (`haku-logs` repo, run-manifest hook, `tools/`
   helper).
3. **1–2 weeks baseline** → orientation share, cost/run, event→surface latency.
4. **Event→fire wiring** (mailbox fix is a prerequisite; then sensors).
5. **Data-class policy + oai zone** (multi_agent.md step 5, already planned).
6. **Then** decide the wake-model A/B.
7. **Local zone on the 5090s** (classifier hosting first).
