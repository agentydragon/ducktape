# Two-tier AI coding-agent fleet — prior-art survey

Companion to [`README.md`](README.md). External-source survey (2026-08-28) for a Claude/Fable
orchestrator dispatching cheap OpenAI-model workers (GPT-5.6 Luna via Codex) through a LiteLLM
gateway. Confidence tags: **[OFFICIAL]** vendor docs/source; **[ISSUE]** GitHub issue/PR (may be
fixed by the time you read this — version noted where known); **[PRIMARY]** a lab's own writeup;
**[PRACTITIONER]** first-person report (unaudited); **[VENDOR]** marketing (numbers unverified);
**[GAP]** explicit negative finding.

## 1. Task→model routing tables / cost-aware dispatch

### 1.1 Academic routing/cascade frameworks (general, not coding-specific)

| Framework                     | Mechanism                                                             | Reported cost cut                                       | Coding-specific? |
| ----------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------- | ---------------- |
| **RouteLLM** (LMSYS/Berkeley) | trained classifier routes to strong/weak model on preference data     | 85% on MT-Bench @95% GPT-4 quality; 45% MMLU; 35% GSM8K | No               |
| **FrugalGPT** (Stanford)      | sequential cascade: cheapest first, escalate on low reliability score | up to 98% at matched quality                            | No               |

RouteLLM <https://www.lmsys.org/blog/2024-07-01-routellm/>; FrugalGPT
<https://arxiv.org/abs/2305.05176>. **[GAP]** Neither has a coding-task cascade — mechanism prior
art, not a ready dispatch policy.

### 1.2 Commercial routers

- **NotDiamond** — `client.model_router.select_model()`, `tradeoff="cost"`, trainable meta-model
  router; markets "Model Routing for Coding Agents". Black-box, no public table.
  <https://docs.notdiamond.ai/docs/quickstart-routing>
- **Martian** — "Model Mapping" predicts per-prompt performance; proprietary.
- **OpenRouter Auto Router (`openrouter/auto`)** — **[OFFICIAL]** the most concrete live/inspectable
  policy: classifies each prompt into ~30 task types (`code:debugging`, `agent:multi_step_planning`,
  …), ranks models within a type by **trailing 7-day community spend**, `cost_tier`
  low→medium→high→xhigh→max (each a band, not a ceiling), `allowed_models` wildcards, session-sticky.
  <https://openrouter.ai/docs/guides/routing/routers/auto-router>

### 1.3 LiteLLM's router is not task-aware — the gap this architecture must fill

**[OFFICIAL, load-bearing]** LiteLLM `Router` strategies (`simple-shuffle` default, `least-busy`,
`usage-based-routing[-v2]`, `latency-based`, `cost-based`) pick _which backend instance of one model
group_ by load/latency/cost — **not** which tier a bugfix vs a refactor gets.
<https://docs.litellm.ai/docs/routing>. **Implication:** task→tier dispatch lives in the
orchestrator's own logic, not LiteLLM. **claude-code-router** (musistudio, MIT) is the Claude-Code-
native fit: local proxy routing by named scenario (`default`/`background`/`reasoning`/`longContext`/
`webSearch`) with JS-scriptable rules; launched via `ccr code`.
<https://github.com/musistudio/claude-code-router>

### 1.4 Aider — the highest-value concrete tables (reproduced)

Polyglot leaderboard (225 hard Exercism, edit-loop), **[OFFICIAL]** <https://aider.chat/docs/leaderboards/>:

| Model                        | Accuracy | Cost (USD/run) |
| ---------------------------- | -------- | -------------- |
| gpt-5 (high)                 | 88.0%    | $29.08         |
| gpt-5 (medium)               | 86.7%    | $17.69         |
| gpt-5 (low)                  | 81.3%    | $10.37         |
| DeepSeek-V3.2 (Reasoner)     | 74.2%    | $1.30          |
| claude-opus-4 (32k thinking) | 72.0%    | $65.75         |

DeepSeek-V3.2 = 84% of gpt-5-high's score at 4.5% of cost — the cleanest "cheap is good enough" point.

Architect/editor split (reasoning model plans, cheap model formats the edit — structurally an
orchestrator/worker split), **[OFFICIAL]** <https://aider.chat/2025/01/24/r1-sonnet.html>:

| Config                           | Accuracy | Cost    |
| -------------------------------- | -------- | ------- |
| R1 (architect) + Sonnet (editor) | 64.0%    | $13.29  |
| o1 alone (prior SOTA)            | 61.7%    | $186.50 |
| DeepSeek V3 alone                | 48.4%    | $0.34   |

R1+Sonnet beat the o1 SOTA at 14× lower cost — the most directly transferable
reasoning-orchestrator/cheap-executor number found (single-turn edit benchmark, not a live fleet).

### 1.5 Practitioner / vendor task→model matrices

**[GAP]** no canonical upvoted community matrix exists. Scattered configs:

- HN Claude-Code-as-orchestrator (**[PRACTITIONER]** <https://news.ycombinator.com/item?id=47168553>):
  design/requirements → Ollama Cloud (Qwen3.5/GLM-5); testing → Sonnet 4.6; impl → Opus 4.6; review →
  GPT-5.1-Codex-Mini; git ops → Minimax-M2.5. ~$140/mo; "running this swarm eats all my anthropic
  tokens" despite offload.
- **MindStudio** [VENDOR]: orchestrator (Opus) = decomposition/review/synthesis; workers
  (Haiku/DeepSeek/Gemma) = writing functions, file reads, tests-against-signature, shell, doc search,
  reformatting. Claims 5–10× cost cut, 80–90% of token volume to cheap models.
- **ccproxy** [VENDOR]: reasoning → Sonnet; codegen → Kimi K2; large-ctx → Gemini Flash; quick →
  Qwen3. Reports $30/day → $3/day via `ANTHROPIC_BASE_URL` proxy.
- **CloudZero**: 1 Opus + 4 Sonnet workers ≈ 40% cheaper than 5 Opus.
- Developers Digest 5-agent fleet (July 2026,
  <https://www.developersdigest.tech/blog/what-parallel-claude-agents-actually-cost>): all-Fable
  $162.50/day; 1 Opus + 3 Sonnet + 1 Haiku ≈ −40% to −52% vs all-Opus baseline $81.25.

### 1.6 Anthropic's own shipped split

**[OFFICIAL]** Claude Code's `opusplan`: Opus for plan-mode reasoning, auto-switch to Sonnet for
implementation. <https://docs.anthropic.com/en/docs/claude-code/model-config>

## 2. Driving Codex CLI programmatically as a worker fleet

GPT-5.6 family: Luna (small, $0.20/$1.20 after the 2026-07-30 80% cut), Terra (mid), Sol (large).
<https://openai.com/index/gpt-5-6/>

### 2.1 `codex exec` headless

**[OFFICIAL/ISSUE]** (<https://developertoolkit.ai/en/codex/advanced-techniques/non-interactive/>):
`codex exec "prompt"`; `--json` (JSONL of every event); `--output-schema <file>` (final message
conforms to JSON Schema); `codex exec resume --last | <session-id>`; `--sandbox workspace-write`,
`-c approval_policy=never`, `--skip-git-repo-check`, `-o <file>`, `-` for stdin. Sessions at
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. **Gotcha:** `resume` historically couldn't take
`--output-schema` (openai/codex#14343 — verify on your version).

### 2.2 `codex app-server` — JSON-RPC, the real fleet interface

**[OFFICIAL+community]** <https://developers.openai.com/codex/app-server>,
<https://gist.github.com/oneryalcin/ee2c27e2d8aa040da8fbe7eebcc2ecea>:
stdio (newline-delimited JSON-RPC 2.0, no `jsonrpc` field) or experimental WS
(`--listen ws://…`). Mandatory `initialize`→`initialized`. Thread methods `thread/start|resume|fork|
list|rollback|name/set|archive` (`fork` enables best-of-N). Turn methods `turn/start` (per-turn
model/effort/sandbox/schema overrides), `turn/steer` (append mid-flight), `turn/interrupt`.
`review/start` = built-in reviewer over a diff (an automated worker-output gate). Server→client
approval requests `execCommandApproval`/`applyPatchApproval` must be handled. Busy = RPC `-32001`,
back off. **Official Python SDK** `openai-codex-app-server-sdk` (`Codex`/`AsyncCodex`,
`Thread.start/resume/fork/run/compact`, `TurnHandle.steer/interrupt`). **Reference "Claude drives
Codex" impl exists**: the Claude Code plugin's `codex-companion.mjs` with a broker mode
(`app-server-broker.mjs`, one long-lived app-server per workspace over a Unix socket — the right
pattern for many short worker tasks). RPC schemas drift per release — pin the binary.

### 2.3 `codex mcp-server` — Codex as MCP tools

**[OFFICIAL]** exactly two tools: `codex` (new conversation — `prompt`, `model`, `cwd`,
`approval-policy`, `sandbox`, `config`, `developer-instructions`; returns `threadId`) and
`codex_reply` (continue by thread). Concurrent threads multiplexed over one MCP connection.
<https://deepwiki.com/openai/codex/6.4-mcp-server-implementation-(codex-mcp-server)>

### 2.4 `@openai/codex-sdk` (TypeScript)

**[OFFICIAL]** spawns the CLI, JSONL over stdio; `Thread.run()` repeatable. **Parallelism contract:**
same-thread calls serialize; **different threads run in parallel** — so mint one `Thread` per
concurrent task, don't share. <https://github.com/openai/codex/blob/main/sdk/typescript/README.md>

### 2.5 Parallel multi-agent drivers

Augment Code survey (<https://www.augmentcode.com/tools/open-source-agent-orchestrators>), plus
direct research:

| Tool             | Pattern                                       | Notable                                                           |
| ---------------- | --------------------------------------------- | ----------------------------------------------------------------- |
| **Claude Squad** | tmux + worktrees TUI                          | 6 sessions <5s; Claude Code/Codex/Aider/Gemini via `-p`; AGPL-3.0 |
| **Vibe Kanban**  | Kanban + MCP decomposition                    | inline diff review; disk bloat from worktrees                     |
| **Bernstein**    | Goal→Planner→task graph→parallel→verify→merge | deterministic Python scheduling = zero LLM coordination tokens    |
| **oh-my-codex**  | tmux workers, worktree-per-worker, mailbox    | durable workers surviving one reasoning burst                     |
| **claude-flow**  | queen/worker "hive-mind", SQLite blackboard   | **[CAUTION]** many near-identical forks, unclear canonical repo   |

git-worktree-per-agent practical ceiling: **~8–10 concurrent** before coordination overhead wins.

### 2.6 Practitioner reports

- **claude-codex-collab** (**[PRACTITIONER]** <https://github.com/AlessioZazzarini/claude-codex-collab>):
  Claude = PM, Codex = engineer, **filesystem + bash the only middleware** (no API keys/MCP/tmux),
  runs on subscriptions. Modes: Think (sync debate, `codex exec -s read-only`, cap 2 rounds), Build
  (Claude specs → `codex exec --full-auto` → Claude polls PID + reviews diff + runs tests), Debug
  (Claude withholds its hypothesis so Codex forms an independent one). **Codex gets no cross-call
  memory — specs must eliminate ambiguity each time.**
- Orchestrator:worker ratio anecdote: **~9M orchestrator vs ~1.2M worker tokens (~8×)** for ~3.5×
  the worker's code output — the orchestrator's read/plan overhead dwarfs the workers.
- Claude Code **Agent Teams** (official): background workers, each isolated context window; commenters
  note it formalizes the tmux+file-bus pattern, and "eats tokens very quickly".

## 3. Claude Code over a LiteLLM/gateway — failure catalogue

### 3.1 Context-window mismatch — **[ISSUE, load-bearing, = the breakage we hit]**

- `anthropics/claude-code#53801`: on Opus 4.7 1M, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` silently
  ignored; auto-compact fires at ~195k (19.5% of 1M) — six events in a 192.5–196.5k band, proving
  the threshold is computed against a hardcoded 200k baseline. Reproduced on v2.1.120.
- `BerriAI/litellm#14444`: Sonnet-4 1M via Bedrock compacts at ~160k. Workaround: append **`[1m]`**
  to the model name + `anthropic-beta: context-1m-2025-08-07,interleaved-thinking-…`.
- **[OFFICIAL]** <https://code.claude.com/docs/en/llm-gateway-protocol>: named pattern is a gateway
  enforcing a smaller context than native and rewriting the error so Claude Code doesn't recognize
  it as too-long. Fix: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (clamped to [100000, model window]) and cap
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. **This repo's candidate fix uses `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
  — the var Claude Code 2.1.251 itself names in its warning; observed to remove the startup
  misdetection warning, end-to-end compaction behavior not yet verified against live traffic.**

### 3.2 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` mechanics **[OFFICIAL]**

Off by default; runs only for `ANTHROPIC_BASE_URL` gateways (not if `CLAUDE_CODE_USE_*` set, or
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` set). Request `GET /v1/models?limit=1000`, 3s timeout,
**any redirect = failure** (credential-leak guard). Sends **one** credential header (differs from
inference which sends both) — a gateway must accept `x-api-key` on `/v1/models` for helper auth.
**Keeps only entries whose `id` contains `claude`/`anthropic`** (was "begins with" before v2.1.223).
**Consequence: a GPT-5.6/DeepSeek/Kimi worker under its literal name never surfaces via discovery —
non-Claude workers must be wired manually via env, discovery is a Claude-name filter.** Cached to
`~/.claude/cache/gateway-models.json`; debug via `[gatewayDiscovery]` in the session debug log.

### 3.3 `ANTHROPIC_BASE_URL` gateway-mode quirks **[OFFICIAL]**

Read once at process start, never re-checked. Three hardcoded aliases `sonnet`/`opus`/`haiku`
remapped via `ANTHROPIC_MODEL` (main) and **`ANTHROPIC_SMALL_FAST_MODEL`** (background:
summarization, titles) — a second implicit routing decision. Credentials: `ANTHROPIC_AUTH_TOKEN`→
`Authorization: Bearer`; `ANTHROPIC_API_KEY`→`x-api-key`; `apiKeyHelper`→both, re-run on 401.
**System-prompt attribution block** is stripped positionally only by `api.anthropic.com`; any
gateway receives it as prompt content, polluting the cache key unless forwarded unchanged
(stable per-conversation since v2.1.181; `CLAUDE_CODE_ATTRIBUTION_HEADER=0` to drop). WAF gotcha:
`403` + HTML body + nothing in gateway logs → a body-inspection XSS rule blocking Claude's
tag-heavy prompts (exempt `/v1/messages`).

### 3.4 Feature pass-through — what silently breaks when a gateway strips a field **[OFFICIAL]**

| Feature                             | Symptom when broken                  | Fix                                                                      |
| ----------------------------------- | ------------------------------------ | ------------------------------------------------------------------------ |
| Adaptive reasoning (4.6+)           | 400 naming `thinking`/`adaptive`     | upgrade upstream, or `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`           |
| Context management                  | 400 "Extra inputs are not permitted" | forward beta header+field, or `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` |
| Extended ctx + interleaved thinking | silently unavailable                 | forward `anthropic-beta` verbatim                                        |
| Effort + structured outputs         | 400 naming `output_config`           | forward field+headers together (`BerriAI/litellm#22963`)                 |
| Token counting                      | burns an inference call to count     | expose `/v1/messages/count_tokens`                                       |

### 3.5 Tool-call translation fidelity **[ISSUE]**

- `BerriAI/litellm#17904` (fixed #20107): OpenAI's 64-char tool-name limit vs Anthropic's — Task-tool
  names exceed it → 400. Fixed via `{55-prefix}_{8-hash}` truncation + restore.
- Discussion #18271 (**open**): Anthropic wants all `tool_result` blocks for a turn in one user
  message; LiteLLM's adapter doesn't merge them — structural gap in OpenAI's schema.
- #25561: streaming drops `tool_use` args for `vertex_ai/gemini-*` — the bug class recurs per bridge.

### 3.6 Interleaved/extended thinking blocks — most widely reproduced **[ISSUE]**

Anthropic requires the final assistant message in a tool turn to start with a `thinking`/
`redacted_thinking` block; every OpenAI-shaped IR drops it. `BerriAI/litellm#15601`:
`"messages.5.content.0.type: Expected 'thinking' or 'redacted_thinking', but found 'tool_use'"`;
PR #15501 fixed **only** `/v1/messages` passthrough, not `/chat/completions` (closed "not planned"
there). Reproduced across 7+ repos (bedrock-access-gateway#119, opencode#3077/#8010,
openai-agents-python#765/#678, open-webui#20464, pydantic-ai#3113). **Nuance for us:** this bites
the Anthropic-format-over-OpenAI-shaped hop (`/chat/completions` internals, Bedrock/Vertex). The
ducktape `chatgpt/ant-messages/*` lane is **CLIProxyAPI `/v1/messages` passthrough**, which is
exactly why the repo chose it (`docs/personal_agents/verdicts.md` § Model routing) — so the worst
of §3.6 is dodged, as long as the orchestrator's own thinking traffic doesn't get routed through a
`/chat/completions` bridge.

### 3.7 `/v1/messages/count_tokens` behind proxies — least reliable surface **[ISSUE]**

Officially optional (absent → estimate via a live inference call). Four distinct bug classes:
admin-only misclassification 403 (#15006, fixed #15034); ignores `tools` on Bedrock → undercount
(#26436); silent wrong-tokenizer fallback on Bedrock (#27632); crash on Vertex (#15323).
**Don't build budget gates on proxied count_tokens without provider-specific verification.**

## 4. Orchestrator-side context economics

### 4.1 Anthropic multi-agent research system **[PRIMARY]**

<https://www.anthropic.com/engineering/multi-agent-research-system>: lead (Opus) plans + spawns 3–5
subagents (Sonnet) with **separate context windows**, then synthesizes. Single-agent ≈ 4× chat
tokens; **multi-agent ≈ 15× chat tokens**; token usage explained **80% of variance** in the
BrowseComp eval. Multi-agent beat single-agent by 90.2% on breadth-first, parallelizable tasks —
**not** recommended for tasks that don't decompose. Subagents return **distilled findings, not
transcripts**; near context limit, the lead saves its plan to external memory and spawns fresh
subagents — detail lives out-of-band, only a pointer stays live.

### 4.2 Cognition — directly on this architecture **[PRIMARY]**

"Don't Build Multi-Agents" (<https://cognition.com/blog/dont-build-multi-agents>) cites the same
~15× multiplier. The 10-months-later follow-up
(<https://cognition.com/blog/multi-agents-working>) validates/qualifies exactly this design:

- **Single-writer principle**: many agents may contribute intelligence (review/plan/validate) in
  parallel, but **state-changing writes stay single-threaded** — parallel writers fragment the
  codebase. → workers propose diffs/findings; only the orchestrator (or one integrator) commits.
- Reviewer/coder separation works **better** with a clean, separate reviewer context.
- **"Smart Friend"**: frontier-model pairs (Claude + GPT) with capability routing work; the helper
  suggests investigations rather than inventing answers.
- **Load-bearing verdict**: _"pairing cheaper workers with Claude works; pairing cheaper workers with
  cheaper helpers doesn't yet"_ — a training-maturity gap in weak models' escalation judgment, not a
  prompting problem. From a lab shipping Devin with 10 months of production feedback.

### 4.3 Token-spend trace **[PRACTITIONER, single session]**

<https://dev.to/slima4/where-do-your-claude-code-tokens-actually-go-we-traced-every-single-one-423e>:
644.8k-token Opus session — useful work ~490k (76%), compaction summaries ~47k (7%), never-usable
headroom ~108k (17%). ~14k constant system-prompt tokens/call; **~98% of post-first-request tokens
are cache reads**; "input tokens dwarf output" (re-sent history, not codegen, dominates). Auto-compact
sits at ~83% of a 200k window (~166k), reserving ~16.5% as never-usable buffer every session.

### 4.4 Delegation break-even **[VENDOR, mechanism sound; cent figures illustrative]**

<https://claudefa.st/blog/guide/development/multi-agent-orchestration-cost>:

| Delegate to a worker when          |                                     |
| ---------------------------------- | ----------------------------------- |
| Deep research across many sources  | Yes — cited 96% quality at 46% cost |
| Long refactor across many files    | Yes, partition by file              |
| Focused lookup with known location | No — keep in orchestrator           |
| Repeated small calls in a loop     | Only to a **persistent** worker     |

Overhead sources, each real: **(1) boundary duplication** (content crossing the line billed twice —
worker output + orchestrator input reading the report); **(2) fan-out overlap** (N workers re-reading
the same files); **(3) cache-write penalty** (a fresh one-shot worker's first call pays the 1.25–2×
uncached-write rate vs ~0.1× cached-read). Quoted break-even: a ~2k-token handoff costs ~$0.14–0.16
overhead, so a worker needs **~500k+ tokens of reading, or 3+ warm-cache reuses**, to beat doing it
in the orchestrator. Corroborated qualitatively by Anthropic's real cache-read pricing.

### 4.5 Structured worker-summary patterns **[mixed, converging]**

- "Typed context object": orchestrator passes only relevant fields (~200–500 tokens) not full
  conversation (~5–20k) — Vellum [VENDOR].
- Progressive summarization: recent turns full-res, older compressed by a cheap model preserving
  operational state.
- Anthropic's out-of-band pattern (§4.1) recurs everywhere (claude-flow SQLite blackboard, Bernstein
  `.sdd/` lineage, Vibe Kanban diff-first review) — the closest thing to cross-project consensus.
- **[GAP]** no source quantified diff-only vs full-transcript review in tokens. Addy Osmani: "the
  bottleneck is no longer generation, it's verification" (<https://addyosmani.com/blog/code-agent-orchestra/>).

## Cross-cutting takeaways

1. **The dispatch policy is yours to write** — neither LiteLLM nor Codex provides task-type routing;
   it lives in the orchestrator (or a Claude-Code-specific router like claude-code-router).
2. **Cognition's verdict is the closest thing to a ruling on this exact design**: frontier
   orchestrator + cheap workers works; cheap orchestrator + cheap workers doesn't yet.
3. **Mechanical risk concentrates in three places**: (a) Claude Code's hardcoded 200k assumption
   (§3.1 — the breakage this spike root-caused, candidate fix unverified end-to-end); (b) thinking-block loss on any Anthropic-over-OpenAI hop
   (§3.6 — dodged by the CLIProxyAPI `/v1/messages` lane); (c) `count_tokens` unreliability behind a
   gateway (§3.7 — don't gate budgets on it).
4. **Delegation has a computable floor** (§4.4): below ~500k tokens read or 3+ warm reuses, a cheap
   worker can cost more than doing the work in the orchestrator, from double-billing at the boundary
   plus the cache-write penalty on freshly spawned workers.
