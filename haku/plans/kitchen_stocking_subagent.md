# Kitchen/household-stocking subagent

**Status: undesigned — this doc is the first pass, from the operator's own framing
(2026-07-11).** Nothing here is built. It fleshes out the "Deferred: grocery-order
bounded-write MCP" line in [`multi_agent.md`](multi_agent.md) into a fuller shape; that
line's intent is a building block of this plan, not a separate thing.

## Goal

A dedicated subagent that owns pantry/fridge/shopping-list state end-to-end, instead of
Haku reasoning over Grocy inline on every kitchen pass (today's mode: `procedures/grocy_log.md`
and hand-curated `kitchen/board.yaml` sections in `haku-state`, all done by Haku itself in its
main session). Targets, in the operator's words: keep the shopping list current, plan
_reasonably cookable_ recipes from what's on hand, keep good fresh fruit in stock matching
what he actually likes.

## Trigger model

Operator's framing: something collects what changed since the subagent's last run (from
Grocy, and possibly other sources) and decides whether to trigger; when it does trigger, it
processes the whole batch of changes at once rather than reacting edit-by-edit.

**What exists today that this can build on:** `haku/dispatch/app.py` is Haku-initiated only
— `POST /jobs` (Haku's own bearer token), no externally-triggered job creation path.
Sensors (`changedetection.io` → webhook → `haku-state intake/`) are step 6 in
`multi_agent.md`'s build order and not built; even once they exist, they land findings in
Haku's own intake for Haku to act on, not a direct dispatch trigger.

Two ways to get the "batch since last run" behavior without waiting on sensor infra:

1. **Haku-initiated batching (buildable today, no new infra):** each Haku pass reads Grocy's
   `stock_log` since a bookmark (exactly what `kitchen/grocy_log.py` already does), and when
   there's a nonempty delta, Haku itself calls `POST /jobs` with the batch as the prompt. This
   reuses 100% of existing dispatch-plane machinery; the "trigger decision" just lives in
   Haku's own run cadence rather than a standalone watcher.
2. **A real collector/watcher (new infra):** a small always-on or cron-triggered component
   polls Grocy's `stock_log` (or a webhook, if Grocy has one — unverified) independent of
   Haku's own run cadence, and calls `POST /jobs` directly once it has a batch worth acting on.
   This is closer to the operator's framing ("something... can tell us whether we should
   trigger") but needs a place to run (a CronJob in `haku-sandbox` is the obvious fit — Haku
   already has full CRUD there) and its own bookmark storage (could reuse
   `haku-state`'s `kitchen/board.yaml:grocy_bookmark`, or keep its own).

Start with (1) — it's free — and only build (2) if latency (Haku's own run cadence isn't
tight enough) turns out to matter.

## Zone / trust fit

Operator's framing: "low sensitivity... run this one easily with z.ai." Worth checking
against the zai zone's actual admission policy before assuming it clears: `multi_agent.md`
describes the zai classifier as admitting **"public-by-construction" prompts only** — the
bar today is about the _prompt_ being safe to leak, not just "low sensitivity" in a general
sense. Household pantry contents aren't secret, but they're also not obviously
public-by-construction the way a public-repo doc audit is. Two live options if zai's actual
classifier turns out to be stricter than this task fits:

- **The oai zone** (moderate trust, described but only partially built per `multi_agent.md`'s
  step 5) explicitly allows "project/calendar-shaped facts, coarse finances" — household
  stock/shopping data fits comfortably there even if it doesn't clear zai's bar.
- **A local model** (`local_dispatch_zone.md`, fully designed, nothing landed) sidesteps the
  question — no data leaves the cluster at all, and the operator explicitly floated this
  ("maybe even a local model, IDK — we would need to actually measure that").

Whichever zone, the workload doesn't need real-time responsiveness (option 1 above ties it
to Haku's run cadence anyway), so it's a good first real workload for whichever
lower-cost lane ends up cleared for it — including being the forcing case that gets the oai
zone or the local zone from "designed" to "built."

## Write surface: needs a bounded-write MCP, not the existing grocy_mcp server as-is

`grocy_mcp/` (`server.py`) is a single full-read-write server per household
(`grocy-mcp-sf`/`grocy-mcp-vallejo`), scoped only by the _calling user's own_ Grocy
permissions via Authentik identity passthrough (`mcp_infra/authentik_auth`) — there is no
service-account/API-key path and no read-only-vs-read-write split at the tool level. That's
the right shape for an operator-driven Haku session (or the haku-console `grocy-sf`
OAuth-linked connector Haku already uses via approval-gated tool-calls), but it's the wrong
shape to hand directly to a low-trust dispatch-plane worker: the full tool surface includes
`product_delete` (irreversible), arbitrary `entities_create/update/delete`, and no per-op
allowlist.

This is exactly the "reviewed MCP server holding the credential server-side, exposing only
bounded ops" pattern `multi_agent.md` already named for grocery-order writes. Concretely, a
kitchen-stocking-subagent write surface probably wants: `stock_add` / `stock_consume` /
`stock_set` (for reconciling what changed), `shopping_list_items_add` /
`shopping_list_item_edit` / `shopping_list_items_remove` — and _not_ `product_delete`,
`entities_delete`, or raw `entities_create` on arbitrary types. Whether that's a genuinely
separate bounded-write MCP deployment or a scoped credential + tool-allowlist in front of
the existing server is an open implementation question; the trust requirement (no
irreversible ops reachable by a zai/oai-tier worker) is the fixed point.

## Model selection: the existing Grocy eval harness is already set up for this

`grocy_mcp/eval/` (`cli.py`, `cases.py`, `run.py`) is **already model-agnostic** — no new
harness code needed for a cheap-vs-expensive-vs-local comparison:

```bash
bazel run //grocy_mcp/eval:cli -- --api anthropic --model claude-haiku-4-5-20251001 ...
bazel run //grocy_mcp/eval:cli -- --api openai --model gpt-4o-mini ...
bazel run //grocy_mcp/eval:cli -- --api openai --model <local-model> --base-url <ollama/litellm endpoint> ...
```

Each run spins up a fresh, isolated Grocy container (`grocy_container.py`), seeds realistic
state (`seed.py`), runs the model against the live MCP server, and snapshots
`final_state.json` + `transcript.jsonl` + `summary.json`. Two existing cases
(`shopping_planning`, `post_cook_logging`) are close analogues of what this subagent would
do; there's no case yet for "here's a batch of `stock_log` changes since last run, reconcile
the shopping list" — adding one is small, additive work in `cases.py`.

**Missing piece: grading.** `EvalResult` carries prose `success_criteria` and a
model-generated `postmortem_text`, but nothing scores a run against the criteria
automatically today (`cases.py`'s own docstring flags this as "for future LLM-driven
grading"). `props/` — the repo's existing LLM-critic eval system — is the natural thing to
wire in rather than building a second grading mechanism; unconfirmed whether its critic
shape (built for a different eval surface) plugs into `EvalResult`/`final_state.json`
directly or needs an adapter.

Plan: (1) add a batch-reconcile eval case, (2) wire a grading step (props/ critic or a
simple rubric prompt, whichever is cheaper to stand up), (3) run it across candidate
models/zones, (4) let the results — not a guess — decide the model tier this subagent
actually ships on. "Realistic snapshots / realistic event progressions" (the operator's
phrase) means `seed.py` should grow scenarios that look like real `stock_log` history, not
just today's synthetic starting states.

## Open gaps beyond model/trust/trigger

- **Presence/occupancy signal.** The subagent needs to know roughly whether the operator is
  home / planning to be home / expecting guests (different quantities, different menu). No
  source exists for this today — `current_location.ts`'s session-local class
  (home/vallejo/sf/elsewhere, `haku-state`-side) and his calendar are the closest existing
  signals, but neither answers "is someone else eating here tonight." Real gap, not just
  reuse-what-exists.
- **Data sources beyond Grocy.** The operator floated the Thrive Market catalog data
  (currently curated by Haku in `kitchen/board.yaml`'s `thrive_order`/`incoming` sections
  through what looks like manual research, not an automated feed — **unconfirmed**, worth
  checking before assuming there's a scraper to wire up) and "maybe more" sources,
  unspecified.
- **Recipe-planning target.** `kitchen/board.yaml`'s `make_now` section (hand-curated by
  Haku today: named dishes, ingredient lists, cookability notes) is the closest existing
  artifact to what this subagent's recipe-planning output should look like — a reasonable
  seed for what "reasonably cookable" means operationally, if this gets built.

## Observability: needs the still-unbuilt Langfuse wiring, not just LiteLLM key metadata

Operator's instinct (2026-07-11) is correct: today's "existing litellm level markings" —
`litellm_key` Terraform resources with a `key_alias`/`metadata` map
(`tf/gitops/litellm-keys/main.tf`) — are key-level attribution (which lane/consumer a key
belongs to), not per-call traces. Actual prompt/completion/tool-call traces need Langfuse, and
the workers-LiteLLM the zai/oai zones route through doesn't have it wired yet — this is a
pre-existing gap, not new: see `multi_agent.md`'s "Langfuse `haku-workers` project + viewer
key" bullet (now fleshed out with what's concretely missing). This subagent shouldn't design
its own tracing story — it's a consumer of that same fix, and probably the forcing function
that finally gets it built, given it's a real recurring workload worth actually watching.

The one kitchen-specific input to that decision: whether it gets a dedicated **"Haku kitchen"**
Langfuse project (operator's suggestion) rather than sharing one flat `haku-workers` project
with every other zone workload. A dedicated project makes sense once there's more than one
real subagent — cleaner filtering, per-domain budget/cost visibility — but is one more thing to
provision (Langfuse has no Terraform provider today; see `multi_agent.md`). Worth deciding
alongside whatever workload becomes the second one, not in isolation for this one.

## Relationship to haku-state

The canonical home for the shopping list itself is Grocy (operator's call, 2026-07-11) —
`haku-state`'s `kitchen/board.yaml:shopping_list` is a stopgap mirror for the haku-ui
Shopping tab until Grocy write permissions are sorted and/or this subagent exists. Once
built, this subagent's output surfaces through the same approval/tool-call path Haku already
uses for Grocy writes (`procedures/tool_calls.md` in haku-state) unless/until its own bounded
write MCP is trusted enough to write directly.
