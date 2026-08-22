# AI Subscription Comparison — Apples-to-Apples (2026-08-21)

## Goal

Find AI capacity that complements an existing **Claude Max 20x + ChatGPT Pro** loadout for heavy agentic coding. That loadout is already at the ceiling of what either frontier vendor sells one person: Max 20x is the top individual Claude tier and ChatGPT Pro $200 the top consumer OpenAI tier, with nothing above either short of per-seat Team/Enterprise plans.

Three axes decide what to add, and they are independent:

1. **Cost per unit of intelligence** — how much useful complex work per dollar, at API list rates.
2. **Subsidy multiple** — how far below API list a subscription sells that work. This is why the loadout exists at all: the pair displaced a four-figure monthly API bill.
3. **Quota headroom** — whether the plan's windows are large enough that they stop being visible. Shape matters less than size; see below.

Optimizing any one alone gives the wrong answer. Axis 1 alone recommends list-price API, which is the bill being escaped. Axis 2 alone recommends the cheapest coding plan on offer, which is how the GLM Coding Plan looked good on paper and disappointed in practice.

## The cost-per-intelligence source

[**Artificial Analysis**](https://artificialanalysis.ai/) publishes the _Intelligence Index vs. Cost per Intelligence Index Task_ scatter with a Pareto frontier line — the chart that circulates on Twitter. The [Intelligence Index v4.1.1](https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index) aggregates nine evals: GDPval-AA v2, τ³-Banking, Terminal-Bench v2.1, SciCode, Humanity's Last Exam, GPQA Diamond, CritPt, AA-Omniscience, AA-LCR.

Cost per task is derived from input, cache-hit, cache-write, reasoning, and answer token prices, divided by task count and weighted by each eval's index weight. **It is API pricing** — it says nothing about subscription economics, which is the gap the rest of this document fills.

Models are scored per reasoning-effort setting, so one model appears several times.

### Intelligence Index vs. cost per task (2026-08)

`Index/$` is index points per dollar of task cost — the "cost per intelligence" ratio read the useful way round.

| Model (effort)                 | Publisher | Index | Cost/task | Index/$ |
| ------------------------------ | --------- | ----: | --------: | ------: |
| Claude Opus 5 (max)            | Anthropic |    63 |     $2.34 |      27 |
| Claude Opus 5 (xhigh)          | Anthropic |    63 |     $1.80 |      35 |
| Claude Fable 5                 | Anthropic |    62 |     $3.14 |      20 |
| Claude Opus 5 (high)           | Anthropic |    61 |     $1.23 |      50 |
| GPT-5.6 Sol (max)              | OpenAI    |    61 |     $1.23 |      50 |
| Grok 4.6 (high)                | xAI       |    61 |     $0.84 |      73 |
| Grok 4.6 (xhigh)               | xAI       |    60 |     $1.04 |      58 |
| Kimi K3 (max)                  | Moonshot  |    60 |     $0.84 |      71 |
| **GLM-5.3 (max)**              | Z.ai      |    60 | **$0.68** |  **88** |
| GPT-5.6 Sol (xhigh)            | OpenAI    |    59 |     $0.81 |      73 |
| Claude Opus 5 (medium)         | Anthropic |    59 |     $0.72 |      82 |
| Qwen3.8 Max                    | Alibaba   |    58 |     $1.13 |      51 |
| GPT-5.6 Sol (high)             | OpenAI    |    57 |     $0.55 |     104 |
| GPT-5.6 Terra (max)            | OpenAI    |    57 |     $0.51 |     112 |
| Gemini 3.7 Flash (high)        | Google    |    56 |     $0.40 |     140 |
| GPT-5.6 Sol (medium)           | OpenAI    |    56 |     $0.37 |     151 |
| Claude Sonnet 5 (max)          | Anthropic |    55 |     $1.72 |      32 |
| GLM-5.2 (max)                  | Z.ai      |    53 |     $0.44 |     121 |
| **DeepSeek V4-Pro 0813 (max)** | DeepSeek  |    53 | **$0.25** | **212** |
| Gemini 3.7 Flash (medium)      | Google    |    53 |     $0.26 |     204 |

**Read Index/$ with care.** Index points are not linear in usefulness: a model that scores 53 and fails your task costs a full retry, so the ratio systematically flatters cheap models. The frontier view is the honest one — the question is not "best ratio" but "cheapest model that clears my quality bar". Two entries matter for that:

- **GLM-5.3 at index 60 for $0.68/task** sits three points off Opus 5 at under a third of the cost. It is the current value point on the frontier at near-frontier quality.
- **DeepSeek V4-Pro 0813 at index 53 for $0.25/task** is the cheapest thing on the frontier at all, roughly 9× cheaper per task than Opus 5 (max).

### Effort setting is a bigger lever than model choice

Opus 5 spans $0.72 → $2.34 per task (medium → max) for 59 → 63 index points. Dropping `max` to `medium` costs 4 index points and saves 69% of spend — a larger swing than switching vendors. Same for GPT-5.6 Sol: $0.37 at medium vs $1.23 at max.

## Quota shape and headroom

Quota shape, worst to best for bursty use:

| Shape                        | Plans                                          | Burst behavior                                                                                        |
| ---------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| **5h rolling + weekly**      | Z.ai GLM, Claude, ChatGPT, MiniMax, Kimi, Qwen | A burst can hit the 5h wall and then idle — but only if the window is small relative to the workload. |
| **Daily reset**              | Cerebras Code                                  | A big day is capped; tomorrow is whole again.                                                         |
| **Monthly pool**             | Cursor, GitHub Copilot, Warp, Windsurf         | Bursts freely until the month's pool is gone.                                                         |
| **Pay-per-token, no window** | All raw APIs; Claude extra-usage credits       | No window to run out of — but zero subsidy, so cost scales linearly and without bound.                |

**Shape is not what bit the GLM plan — size was.** The same usage pattern rarely reaches the 5h cap on Claude Max 20x or on Codex, yet hit GLM's 5h wall routinely. Identical shape, opposite outcome, so the variable is the window's absolute size, not the fact that it is five hours long.

**And quality feeds back into quota burn.** A weaker model bills for its failures: extra iterations, retried tool calls, and re-reads all consume window. A model needing three passes where a frontier model needs one triples effective consumption of a nominally equal quota. On Z.ai's own Code Bench, GLM-5.3 completes a task in ~50k output tokens where Opus 4.8 spends ~120k. So "it wasn't very smart" and "the quotas ran out fast" are most likely one root cause rather than two — and both point at the same fix, which is a better model on a larger tier rather than a different quota shape.

**Z.ai still moved the wrong way on shape.** The [2026-07-30 plan revision](https://docs.z.ai/devpack/notice/usage-revision) switched GLM Coding Plans from prompt counts to credits, kept the 5-hour rolling window, and added a peak-hour multiplier: GLM-5.3 and GLM-5-Turbo bill at **3x during 14:00-18:00 SGT weekdays**, 1x off-peak. That is a real cost, just not the one that caused the original disappointment.

**Claude's escape valve has a price tag.** [Extra usage credits](https://support.claude.com/en/articles/11049741-what-is-the-max-plan) let Pro/Max keep working past the cap, drawn down automatically, capped at $2,000/day redemption. They bill at **standard API list rates** — no subscription discount whatsoever — so they solve the availability problem by abandoning the economics one. For a Max 20x workload, an overrun priced at Opus 5's $5/$25 reaches three figures in a session and four in a month, which is the bill the subscriptions were bought to replace. Useful as a capped emergency valve; ruinous as a capacity plan.

Two pools are easy to confuse: interactive sessions draw subscription first, then extra usage; unattended Agent SDK work draws its own monthly pool that does not roll over.

## What a subscription is actually worth

Subscriptions beat the API because they are sold below API cost. The size of that discount is the whole game, and it varies by an order of magnitude between vendors.

| Plan                                |   $/mo | Included capacity       |           Value at API rates | Subsidy |
| ----------------------------------- | -----: | ----------------------- | ---------------------------: | ------: |
| Cerebras Code Max                   |    200 | 120M tok/day (~3.6B/mo) |                      ~$3,300 |    ~17x |
| Z.ai GLM (any tier)                 | 18-168 | credits                 |     vendor states 15-30x fee |  15-30x |
| Cerebras Code Pro                   |     50 | 24M tok/day (~720M/mo)  |                        ~$660 |    ~13x |
| Claude Max 20x + ChatGPT Pro (pair) |    400 | opaque                  | displaced 4-figure API spend |  >=2.5x |

Cerebras figures use GLM-4.7 at Z.ai list ($0.60/$2.20) blended 80/20 input/output; Z.ai's multiple is [its own published figure](https://docs.z.ai/devpack/notice/usage-revision) ("approximately 15-30x the monthly subscription fee").

Restated as raw capacity, which is what a weekly ceiling actually rations:

| Plan              | $/mo | Tokens/month (approx) | Tokens per $ |
| ----------------- | ---: | --------------------: | -----------: |
| Cerebras Code Max |  200 |                 ~3.6B |         ~18M |
| Cerebras Code Pro |   50 |                 ~720M |       ~14.4M |
| Z.ai GLM Max      |  168 |            ~1.3B-2.5B |      ~8M-15M |
| Z.ai GLM Pro      |   80 |            ~600M-1.2B |      ~8M-15M |
| Claude Max 20x    |  200 |                 ~110M |        ~0.6M |

Z.ai rows convert its published 15-30x multiple at GLM-5.3 blended $2.00/M. The Claude row back-solves from a four-figure API bill displaced at Opus 5 blended ~$9/M, so it is an estimate with wide error bars. The spread is still roughly **30x between the cheapest and most expensive token pools** — and they are not interchangeable tokens.

**The frontier vendors subsidize least.** Serving Opus 5 and GPT-5.6 costs more, and the plans price accordingly. A marginal capacity dollar buys roughly 5-10x more tokens at Cerebras or Z.ai than at Anthropic or OpenAI — at correspondingly lower model quality. That trade is the only real decision here.

**Extra-usage credits are not a discount.** Anthropic's overflow bills at list API rates, which is a 1x subsidy — precisely the pricing a subscription exists to avoid. It buys availability, never economy. For a workload large enough to justify Max 20x, leaving it enabled without a hard cap reproduces the four-figure API bill that the subscriptions replaced. Treat it as an emergency valve with a cap set low enough to hurt, not as a capacity plan.

## API pricing (per 1M tokens, 2026-08)

| Model                | Input                | Output                 | Notes                                        |
| -------------------- | -------------------- | ---------------------- | -------------------------------------------- |
| Claude Fable 5       | $10.00               | $50.00                 | 1M context                                   |
| Claude Opus 5        | $5.00                | $25.00                 | 1M context                                   |
| Claude Sonnet 5      | $3.00 ($2.00 intro¹) | $15.00 ($10.00 intro¹) | 1M context                                   |
| Claude Haiku 4.5     | $1.00                | $5.00                  | 200K context                                 |
| GPT-5.6 Sol          | $5.00                | $30.00                 | after 2026-07-30 repricing                   |
| Kimi K3              | $3.00                | $15.00                 | cache hit $0.30; flat across 1M context      |
| Grok 4.6             | $2.00                | $6.00                  | $4.00/$12.00 once prompt ≥200K; cached $0.50 |
| Qwen3.8-Max          | $2.00                | $6.00                  | flat, since 2026-08-03                       |
| GLM-5.3              | $1.40                | $4.40                  | same rate as GLM-5.2                         |
| Gemini 3.7 Flash     | $0.75                | $3.75                  | promo; doubles 2027-01-01                    |
| DeepSeek V4-Pro 0813 | $0.66                | $1.98                  | off-peak; $1.32/$3.96 peak²                  |
| MiniMax M2.7         | $0.30                | $1.20                  | 205K context                                 |

¹ Sonnet 5 introductory rate runs through 2026-08-31.
² DeepSeek peak hours are 01:00–04:00 and 06:00–10:00 UTC.

## Subscription comparison

Throughput figures are order-of-magnitude. Treat them as ±2× ranges.

| Plan                    |   $/mo | Quota shape                       | Burst | Models                            | Notes                                       |
| ----------------------- | -----: | --------------------------------- | ----- | --------------------------------- | ------------------------------------------- |
| **Cerebras Code Pro**   |     50 | 24M tok/**day**, 300k TPM         | ●●○   | GLM-4.7                           | Daily reset; ~1000 tok/s                    |
| **Cerebras Code Max**   |    200 | 120M tok/**day**, 400k TPM        | ●●○   | GLM-4.7                           | Older model, very high throughput           |
| **Z.ai GLM Lite**       |     18 | 10k credits/wk + 5h window        | ○○○   | GLM-5.3 / 5.2 / 5.1 / 4.7         | 3× peak multiplier on 5.3                   |
| **Z.ai GLM Pro**        |     80 | 60k credits/wk + 5h window        | ○○○   | same                              | Best raw throughput/$; worst burst shape    |
| **Z.ai GLM Max**        |    168 | 140k credits/wk + 5h window       | ○○○   | same                              | same                                        |
| **Claude Max 20x**      |    200 | 5h + weekly (all-models + Sonnet) | ●●○ ³ | Opus 5, Sonnet 5                  | Opus 5 default on Max since 2026-07-25      |
| **Claude Max 5x**       |    100 | same, 5×                          | ●●○ ³ | Opus 5, Sonnet 5                  | Half the capacity of 20x for half the price |
| **ChatGPT Pro ($200)**  |    200 | 5h windows, 20× Plus              | ○○○   | GPT-5.6, o-series, Codex          | 250 deep research runs/mo, Sora, Operator   |
| **ChatGPT Pro ($100)**  |    100 | 5h windows, 5× Plus               | ○○○   | GPT-5.6, GPT-5.4, o3-pro, Codex   | Added 2026-04-09                            |
| **Cursor Ultra**        |    200 | 10k requests/**month**            | ●●●   | Multi-frontier routing            | Cursor-bound                                |
| **GitHub Copilot Pro+** |     39 | 1.5k premium requests/**month**   | ●●●   | Multi-frontier routing            | IDE/CLI-bound                               |
| **MiniMax Max**         |     50 | 1000 prompts/5h                   | ○○○   | M3, M2.7                          | Cheap, index ~50 tier                       |
| **Kimi Allegretto**     |   ¥199 | 5h + weekly                       | ○○○   | K3, K2.7                          | K3 is index 60; ¥699 tier adds 1M context   |
| **Qwen Standard**       |   ¥139 | 3k credits/5h, 10k/wk             | ○○○   | Qwen3.8-Max, DeepSeek-V4-Pro, GLM | Multi-vendor routing                        |
| **DeepSeek API**        | pay-go | none                              | ●●●   | V4-Pro 0813, V4-Flash             | Cheapest frontier-adjacent tokens anywhere  |

³ Claude's own windows are 5h + weekly. Extra-usage credits remove the ceiling on demand, but at list API rates — availability, not capacity. Rated ●●○ on that basis, not ●●●.

## Recommendation

The loadout is already at the ceiling of subsidized frontier capacity. Max 20x is the top individual Claude tier — nothing above it short of per-seat Team/Enterprise — and ChatGPT Pro $200 is likewise OpenAI's top consumer tier. Neither vendor sells more subsidized capacity to one person. So the moves available are: spend existing quota better, add a third vendor's subsidized plan, or pay per token somewhere far cheaper than Anthropic.

**The binding constraint is Max 20x's weekly cap, not its 5h window.** The 5h caps are rarely reached; the weekly all-models cap is hit often. That narrows the question sharply: what is wanted is a large _weekly or monthly_ pool, and burst shape barely matters. A plan metered in 5h windows is fine so long as its longer window is big — which is the opposite of the conclusion a spiky-usage framing would reach.

The GLM disappointment is best explained as one fault, not two: an undersized tier running a model two generations old, where the model's weakness drove the quota burn that made the tier feel small. Both halves have moved since.

**Quality.** GLM-5.3 (2026-08-14) scores 59.5-60 on the AA Intelligence Index against Opus 5's 63, and finishes Z.ai Code Bench tasks in ~50k output tokens against Opus 4.8's ~120k. It still loses badly on long-horizon agentic work — Terminal-Bench 3.0 has Opus 5 at 42.7% against GLM-5.3's 28.3% — so it is a plausible everyday model, not an Opus replacement.

**Tier.** Max carries 140k weekly credits against Lite's 10k, a 14x span. A wall hit on a small tier says nothing about the large one.

### Where to buy more, ranked

1. **Cerebras Code Max ($200) — the most capacity per dollar in the market.** ~3.6B tokens/month at ~18M tokens per dollar, roughly 30x the token pool of a Claude Max seat, and it resets **daily**, so there is no weekly ceiling to hit at all. That directly answers the constraint. The price is model quality: GLM-4.7, two generations behind GLM-5.3. Start at **Pro ($50)** — 720M tokens/month is already ~6x a Max 20x seat's pool, enough to prove whether GLM-4.7 output is acceptable for the work being displaced before committing $200.

2. **Z.ai GLM Max ($168) — best pool-per-dollar at a model worth using.** ~8-15M tokens per dollar, and GLM-5.3 at index 60 is three points off Opus 5 rather than a generation behind. 140k credits/week is 14x the Lite tier. The weekly window is a real ceiling, unlike Cerebras's daily reset, but at Max tier it is a high one. Test GLM-5.3 on pay-per-token API first ($1.40/$4.40) — a week of real work settles the quality question for a few dollars, and the earlier disappointment was a two-generations-old model on a small tier.

3. **A second cheap-vendor plan rather than a bigger one of the first.** Kimi K3 (index 60, same tier as GLM-5.3) and Qwen Pro (¥499, routes Qwen3.8-Max at index 58 plus DeepSeek-V4-Pro and GLM) are genuine alternates. Two $80 plans across vendors beat one $168 plan if GLM specifically turns out not to suit the work — and vendor diversity also hedges the quota re-tiering that Z.ai has now done twice in a year.

4. **DeepSeek V4-Pro API for anything that must not queue.** ~$0.92/M blended off-peak, no window at all. Not subsidized, so it scales linearly, but at roughly a tenth of Anthropic list it is the cheap way to buy the unmetered tail.

5. **Anthropic Batch API for async frontier work — the one real discount Anthropic offers.** 50% off list, so Opus 5 at $2.50/$12.50. Still expensive next to everything above, but it is the only way to get more _frontier-quality_ Claude tokens below list price, and unattended or overnight work is exactly what it fits. Unlike extra usage, the discount is real.

### Stretching what is already bought

Routing traffic to cheaper models is the other half of this and is free. Since Max is metered in tokens, cutting tokens per task raises tasks per weekly window one-for-one: Opus 5 at `medium` costs 69% less than at `max` for 4 index points, and prompt caching bills reads at ~0.1x, so a silently invalidated cache is a multiple on weekly burn rather than only on money. <aiquota/README.md> already polls Claude, Codex, and Z.ai and reports which window binds — worth reading before and after any purchase, to confirm the new plan is absorbing load rather than sitting idle.

**Do not enable Claude extra usage as a capacity strategy.** It is list API pricing wearing a subscription's clothes, and it is the mechanism that produces four-figure months.

## Skip

- **Undersized tiers of anything** — the GLM lesson generalizes: a small tier of a cheap plan buys a wall, not capacity. Buy the tier that clears the workload or skip the vendor.
- **SuperGrok Heavy ($300)** — Grok 4.6 is a strong model (index 61) but at $2/$6 the API is the sane way to buy it.
- **Cursor Ultra / Copilot Pro+** — good burst shape, but the value is in their IDE surfaces; from Claude Code the routing is redundant.
- **Perplexity Max** — search-optimized, wrong tool.
- **Claude extra usage as a capacity plan** — list API pricing by another name; see above. Cap it and treat it as an emergency valve.
- **Discounted-credit resale marketplaces** — both Anthropic and OpenAI prohibit reselling API access, so credits bought at "40–60% off" carry termination risk on an account that matters.

## TOS notes

- **Z.ai / Kimi / Qwen coding plans**: Anthropic-compatible endpoints and agent-harness integration are the documented, marketed use case.
- **DeepSeek**: standard pay-per-token API, no agent restrictions.
- **ChatGPT Pro (Codex)**: a single-user personal agent on your own subscription is documented functionality — OpenAI ships "Sign in with ChatGPT" OAuth and lists `codex exec`, the Codex SDK, and scriptable workflows as Plus/Pro features. The [Terms of Use](https://openai.com/policies/row-terms-of-use/) prohibit resale, powering third-party **services**, **multi-user** apps, and programmatic **extraction**. Gray area: "Codex access tokens for trusted automation" are gated to Business/Enterprise, so unattended automation is nudged toward enterprise tokens.
- **Claude Max**: use through Claude Code (first-party). Piping the Max consumer endpoint into _other_ third-party harnesses violates Anthropic's TOS — use the API for that. Extra-usage credits and the Agent SDK pool are the sanctioned paths for overflow and unattended work respectively.
- **GitHub Copilot**: bound to Copilot clients. No raw API key for harness reuse.

## Caveats

- **Index/$ flatters cheap models.** A failed task costs a retry plus the operator attention to notice. Weight the frontier position, not the ratio.
- **AA cost-per-task is API pricing.** It does not model subscription quotas, and a plan's effective rate can beat or trail it by several times depending on saturation.
- **Token-pool figures assume saturation.** No one sustains 24M tokens/day every day; the tokens-per-dollar column is a ceiling, not an expectation, and the realized multiple depends entirely on how much load actually moves to the new plan.
- **Benchmarks proxy for the loop, badly.** Index scores say little about tool-call reliability, long-context coherence, or structured-output discipline — the properties that actually decide whether an agent run completes.
- **Quotas drift fast.** Z.ai re-tiered twice in 2026; OpenAI added a $100 Pro tier in April; Gemini CLI quotas change without notice. Re-check before committing.
- **Geopolitical / data-handling risk for Z.ai, DeepSeek, Moonshot, Alibaba, MiniMax.** All PRC-jurisdiction. Z.ai has been US Entity-Listed since Jan 2025. APIs carry no-train/no-store clauses but no anti-government-request carveout. Don't route proprietary code through any of them.
- **Peak-hour multipliers are easy to miss** in both directions: Z.ai's 3× peak on GLM-5.3 (14:00–18:00 SGT) and DeepSeek's 2× peak (01:00–04:00, 06:00–10:00 UTC) can double or triple an expected bill.

## Sources

### Cost-per-intelligence analysis

- [Artificial Analysis](https://artificialanalysis.ai/) — Intelligence Index vs. Cost per Task chart with Pareto frontier
- [AA Intelligence Index v4.1.1](https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index) — index composition
- [AA model leaderboard](https://artificialanalysis.ai/leaderboards/models) — per-effort scores and cost per task
- [AA Intelligence Index v4.1 — shift toward agentic workloads](https://artificialanalysis.ai/articles/artificial-analysis-intelligence-index-v4-1)
- [BenchLM AA leaderboard mirror](https://benchlm.ai/benchmarks/artificialanalysis)

### Quota structures

- [Z.ai plan update announcement](https://docs.z.ai/devpack/notice/usage-revision) — 2026-07-30 credits migration, peak multipliers
- [Anthropic: what is the Max plan](https://support.claude.com/en/articles/11049741-what-is-the-max-plan)
- [Claude extra usage: three billing pools](https://agentshortlist.com/articles/claude-extra-usage)
- [Claude usage limits timeline](https://explainx.ai/blog/claude-usage-limits-2026-timeline-explained)
- [Cerebras Code](https://www.cerebras.ai/blog/introducing-cerebras-code), [Code FAQ](https://support.cerebras.net/articles/9996007307-cerebras-code-faq)
- [Coding plan comparison](https://codingplan.org/en), [AI deals roundup](https://tokenmonopoly.com/ai-deals)
- [ChatGPT subscription tiers](https://www.aipricing.guru/chatgpt-subscription-pricing/)

### Model pricing and benchmarks

- [GLM-5.3 API pricing](https://venturebeat.com/technology/glm-5-3-hits-the-api-at-1-4-4-4-per-million-tokens), [GLM-5.3 vs Claude Opus 5](https://codingfleet.com/blog/glm-5-3-vs-claude-opus-5/), [GLM-5.3 benchmarks](https://atoms.dev/blog/glm-5-3-benchmarks-api-coding-open-weights)
- [DeepSeek pricing](https://deepseek.ai/pricing), [V4-Pro-0813 rates](https://benchlm.ai/deepseek/api-pricing)
- [Kimi K3 pricing](https://benchlm.ai/moonshot/api-pricing)
- [OpenAI API pricing](https://benchlm.ai/openai/api-pricing), [GPT-5.6 price-performance](https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/)
- [xAI API pricing](https://benchlm.ai/xai/api-pricing)
- [Gemini pricing](https://felloai.com/gemini-pricing/)

### Z.ai company / risk

- [Z.ai Wikipedia](https://en.wikipedia.org/wiki/Z.ai), [Entity List addition (SCMP)](https://www.scmp.com/tech/tech-war/article/3295002/tech-war-us-adds-chinese-ai-unicorn-zhipu-trade-blacklist-bidens-exit)
- [Z.ai Terms of Use](https://docs.z.ai/legal-agreement/terms-of-use)
