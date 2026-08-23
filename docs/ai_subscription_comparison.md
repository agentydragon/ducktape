# AI Subscription Comparison — Apples-to-Apples (2026-08-21)

## Goal

Find AI capacity that complements an existing **Claude Max 20x + ChatGPT Pro** loadout for heavy agentic coding. That loadout is already at the ceiling of what either frontier vendor sells one person: Max 20x is the top individual Claude tier and ChatGPT Pro $200 the top consumer OpenAI tier, with nothing above either short of per-seat Team/Enterprise plans.

Three axes decide what to add, and they are independent:

1. **Cost per unit of intelligence** — how much useful complex work per dollar, at API list rates.
2. **Subsidy multiple** — how far below API list a subscription sells that work. This is why the loadout exists at all: the pair displaced a four-figure monthly API bill.
3. **Quota shape and failure mode** — how fast a parallel fleet drains the plan's windows, and what happens at the wall. A 429 that clears in a minute and a five-hour lockout are not the same constraint.

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

## Quota shape, parallelism, and failure mode

The relevant variable is not how big a quota is but **how fast a parallel agent fleet drains it, and what happens at the wall.** A fleet of 5-10 concurrent agents burns 5-10x the tokens per wall-clock hour, which turns a short rolling window from a formality into a hard stop reached mid-session. It also pushes per-minute request ceilings — normally invisible — into the binding position.

That is what separates two plans with nominally identical 5h+weekly shapes:

- **Claude Max 20x** absorbs the fleet inside its 5h window; the weekly cap is what eventually binds.
- **Z.ai GLM Max** — the top tier — did not. Same shape, same usage, wall hit constantly.

So the earlier "undersized tier" reading is wrong. GLM Max is Z.ai's largest plan, and its 5h ceiling is simply low in absolute terms against a parallel fleet. Buying a bigger Z.ai tier is not available as a fix, because that was already the biggest one.

### Failure mode is a first-class criterion

What the limit does when reached matters as much as where it sits:

| Failure mode                      | Plans                         | Cost when hit                                                        |
| --------------------------------- | ----------------------------- | -------------------------------------------------------------------- |
| **429, clears within the minute** | Cerebras TPM/RPS ceilings     | Retry-with-backoff absorbs it; the session continues.                |
| **No limit at all**               | Pay-per-token APIs            | Nothing stops; only the bill grows.                                  |
| **Daily reset at a fixed hour**   | Cerebras token/day cap        | Predictable and plannable; you know when it returns.                 |
| **Weekly rolling cap**            | Claude, ChatGPT               | Bad, but visible in advance and amortized over days.                 |
| **5h rolling lockout**            | Z.ai GLM, MiniMax, Kimi, Qwen | Worst. Arrives mid-flow, unannounced, and parks the fleet for hours. |

**The 5h lockout costs more than the hours it blocks.** An unpredictable multi-hour stop in the middle of five parallel agents does not merely delay that work — it teaches you not to start work you might not be able to finish, so the plan gets used well below its nominal quota. Realized value falls far short of purchased value, and the gap never shows up in a tokens-per-dollar table. A plan that throttles gracefully at 80% of another's headline quota is worth more than the one that locks out.

### Parallelism has its own ceilings

Per-minute limits, not just per-window quotas, decide whether a fleet runs. [Cerebras Code](https://support.cerebras.net/articles/9996007307-cerebras-code-faq) publishes both:

| Plan              | RPM | RPS (10% of RPM) |  TPM | Tokens/day |
| ----------------- | --: | ---------------: | ---: | ---------: |
| Cerebras Code Pro |  50 |                5 |   1M |        24M |
| Cerebras Code Max | 120 |               12 | 1.5M |       120M |

**RPS is not the binding one.** An agent spends most of a turn generating and running tools, not issuing requests, so its cadence is one request per several seconds to tens of seconds. Ten agents therefore sit in the low single-digit RPS, inside even Pro's 5 RPS, with occasional bursts that retry backoff absorbs.

**RPM and tokens/day bind first, and Cerebras's speed makes RPM worse rather than better.** At ~1000 tok/s a turn completes in seconds, so each agent cycles faster and issues more requests per minute than it would against a slower endpoint — plausibly 10-20 RPM each. Ten agents then land at 100-200 RPM, over Pro's 50 and at or above Max's 120. The daily token cap is the other real ceiling: 24M on Pro is reachable within a heavy session, 120M on Max within a long day.

Both are numbers to size against rather than verdicts — request cadence varies enormously with task shape, and a fleet-hour of real measurement beats this arithmetic.

### Cerebras serves GLM-4.7 only — with an upgrade path

[Cerebras Code](https://www.cerebras.ai/code) lists GLM-4.7 across all three tiers (Free, Pro, Max). There is no GLM-5.2 or GLM-5.3 option at any price, so the whole plan family sits two releases behind Z.ai's own menu.

Two things soften that:

- **Cerebras tracks open weights, and it has upgraded before** — GLM-4.6 first, then GLM-4.7 in June 2026. GLM-5.3's weights are due roughly two weeks after its 2026-08-14 launch, so a 5.3 migration is plausible rather than speculative. Worth re-checking the model list before renewing, and a reason to prefer monthly over annual billing here.
- **GLM-4.7's weakness is not tool calling.** Cerebras cites it as #1 on the Berkeley Function Calling Leaderboard — a vendor claim, but the relevant axis for a fleet, where tool-call reliability governs whether a run completes more than raw index score does. A model two generations back on general intelligence can still be the right one for bulk agent work.

**Z.ai still moved the wrong way on shape.** The [2026-07-30 plan revision](https://docs.z.ai/devpack/notice/usage-revision) switched GLM Coding Plans from prompt counts to credits, kept the 5-hour rolling window, and added a peak-hour multiplier: GLM-5.3 and GLM-5-Turbo bill at **3x during 14:00-18:00 SGT weekdays**, 1x off-peak. A 3x drain rate against the window that was already the problem.

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

| Plan                    |   $/mo | Quota shape                         | Burst | Models                            | Notes                                       |
| ----------------------- | -----: | ----------------------------------- | ----- | --------------------------------- | ------------------------------------------- |
| **Cerebras Code Pro**   |     50 | 24M tok/**day**, 1M TPM, 50 RPM     | ●●○   | GLM-4.7 only                      | Daily reset; 50 RPM is the fleet constraint |
| **Cerebras Code Max**   |    200 | 120M tok/**day**, 1.5M TPM, 120 RPM | ●●○   | GLM-4.7 only                      | No 5h or weekly window at all               |
| **Z.ai GLM Lite**       |     18 | 10k credits/wk + 5h window          | ○○○   | GLM-5.3 / 5.2 / 5.1 / 4.7         | 3× peak multiplier on 5.3                   |
| **Z.ai GLM Pro**        |     80 | 60k credits/wk + 5h window          | ○○○   | same                              | Best raw throughput/$; worst failure mode   |
| **Z.ai GLM Max**        |    168 | 140k credits/wk + 5h window         | ○○○   | same                              | Top tier; 5h wall still hit by a fleet      |
| **Claude Max 20x**      |    200 | 5h + weekly (all-models + Sonnet)   | ●●○ ³ | Opus 5, Sonnet 5                  | Opus 5 default on Max since 2026-07-25      |
| **Claude Max 5x**       |    100 | same, 5×                            | ●●○ ³ | Opus 5, Sonnet 5                  | Half the capacity of 20x for half the price |
| **ChatGPT Pro ($200)**  |    200 | 5h windows, 20× Plus                | ○○○   | GPT-5.6, o-series, Codex          | 250 deep research runs/mo, Sora, Operator   |
| **ChatGPT Pro ($100)**  |    100 | 5h windows, 5× Plus                 | ○○○   | GPT-5.6, GPT-5.4, o3-pro, Codex   | Added 2026-04-09                            |
| **Cursor Ultra**        |    200 | 10k requests/**month**              | ●●●   | Multi-frontier routing            | Cursor-bound                                |
| **GitHub Copilot Pro+** |     39 | 1.5k premium requests/**month**     | ●●●   | Multi-frontier routing            | IDE/CLI-bound                               |
| **MiniMax Max**         |     50 | 1000 prompts/5h                     | ○○○   | M3, M2.7                          | Cheap, index ~50 tier                       |
| **Kimi Allegretto**     |   ¥199 | 5h + weekly                         | ○○○   | K3, K2.7                          | K3 is index 60; ¥699 tier adds 1M context   |
| **Qwen Standard**       |   ¥139 | 3k credits/5h, 10k/wk               | ○○○   | Qwen3.8-Max, DeepSeek-V4-Pro, GLM | Multi-vendor routing                        |
| **DeepSeek API**        | pay-go | none                                | ●●●   | V4-Pro 0813, V4-Flash             | Cheapest frontier-adjacent tokens anywhere  |

³ Claude's own windows are 5h + weekly. Extra-usage credits remove the ceiling on demand, but at list API rates — availability, not capacity. Rated ●●○ on that basis, not ●●●.

## Recommendation

The loadout is at the ceiling of subsidized frontier capacity. Max 20x is the top individual Claude tier — nothing above it short of per-seat Team/Enterprise — and ChatGPT Pro $200 is likewise OpenAI's top consumer tier. Neither vendor sells more subsidized capacity to one person.

Two facts set the buying criteria:

- **The workload runs 5-10 agents in parallel.** Short rolling windows drain 5-10x faster than a serial workload implies, and per-minute request ceilings stop being theoretical — though RPM and daily token caps bind well before RPS does.
- **Claude Max 20x binds weekly, not at 5h.** Its 5h window absorbs the fleet. Z.ai GLM Max — the top Z.ai tier — did not.

So the target is a big pool whose _failure mode_ is a 429 or a predictable daily reset, never a multi-hour rolling lockout. That criterion, not tokens-per-dollar, does most of the ranking.

**Consumer subscriptions are priced for one human at a keyboard.** A 5-10 agent fleet is an order of magnitude outside that design point, which is why every plan surveyed binds _somewhere_ — 5h window, weekly cap, RPM ceiling, or daily quota. No amount of tier shopping escapes that; the tiers are all drawn against single-operator assumptions. The realistic shape is a **base plus a metered layer**: keep the frontier subscriptions for interactive work, and put fleet-scale bulk on pay-per-token capacity where the only ceiling is the bill. Expect the metered layer to be a real line item rather than a rounding error, and size the cheap-model routing to keep it small.

### Where to buy more, ranked

1. **Pay-per-token API as the fleet's primary capacity.** No window of any kind is the only structure that scales with agent count — API rate limits exist but are 429-with-retry and rise with spend tier, never a multi-hour lockout. DeepSeek V4-Pro at ~$0.92/M blended off-peak is roughly a tenth of Anthropic list; GLM-5.3 at $1.40/$4.40 buys index-60 quality with no Coding Plan window attached. Unsubsidized and linear, which is the trade: it converts "everything stops for 3.7 hours" into a bounded, predictable dollar cost. Set a monthly cap and treat that cap as the real budget decision.

2. **Cerebras Code Max ($200) — the best-shaped subscription, with real caveats.** **No 5h or weekly window exists**: only a daily token cap and per-minute ceilings, so the failure mode is a 429 that clears within the minute. 120M tokens/day, 1.5M TPM, 120 RPM. Against a 10-agent fleet the 120 RPM is the number to watch — Cerebras's speed makes agents cycle faster, so requests-per-minute climbs — and 120M/day is reachable in a long day, so it raises the floor rather than removing the ceiling. **Pro ($50) is the cheaper experiment** if 50 RPM and 24M/day clear a measured fleet-hour; Max buys 2.4x the RPM and 5x the daily tokens. Caveats: independent testing has found real throughput well under the marketing figure, and the model menu is GLM-4.7 only (see above). Worth one month against real load before renewing.

3. **Anthropic Batch API for anything that can run async.** 50% off list — Opus 5 at $2.50/$12.50 — and it draws from neither the 5h nor the weekly window. Frontier quality, off the quota that currently binds. Overnight and unattended work is exactly its shape, and shifting that class of work off the weekly cap is worth more than its dollar cost suggests.

4. **Z.ai GLM: do not re-buy on current terms.** It was already tested at Max, the largest tier, and failed on precisely this axis. The credits migration kept the 5h window and added a 3x peak multiplier, so the drain rate against the binding constraint got worse. GLM-5.3 is a genuinely better model than what was tried and is worth using **through the API**, where no window exists — but the Coding Plan's shape is wrong for a parallel fleet at any tier Z.ai sells. Revisit only if the window structure changes.

5. **Kimi and Qwen plans inherit the same 5h structure** and should be assumed to fail the same way until their 5h ceilings are checked against a parallel fleet specifically. Kimi K3 matches GLM-5.3 on quality (index 60); if a plan is wanted from either, verify the short-window ceiling before the model quality.

### Stretching what is already bought

Routing traffic to cheaper models is the other half and is free. Since Max is metered in tokens, cutting tokens per task raises tasks per weekly window one-for-one: Opus 5 at `medium` costs 69% less than at `max` for 4 index points, and prompt caching bills reads at ~0.1x, so a silently invalidated cache is a multiple on weekly burn rather than only on money. Parallelism makes both levers worth more, since every agent in the fleet pays the same multiplier. <aiquota/README.md> already polls Claude, Codex, and Z.ai and reports which window binds — worth reading before and after any purchase.

**Do not enable Claude extra usage as a capacity strategy.** It is list API pricing wearing a subscription's clothes, and it is the mechanism that produces four-figure months.

## Skip

- **Any 5h-rolling-window plan, at any tier, for a parallel fleet** — the GLM Max result generalizes: a fleet drains a short window several times faster than the tier sizing assumes, and the lockout suppresses use of the plan well beyond the hours it blocks.
- **SuperGrok Heavy ($300)** — Grok 4.6 is a strong model (index 61) but at $2/$6 the API is the sane way to buy it.
- **Cursor Ultra / Copilot Pro+** — monthly pools are the right shape, but the value is in their IDE surfaces; from Claude Code the routing is redundant.
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
- **Per-agent request and burn rates here are estimates.** The 10-20 RPM per agent behind the Cerebras arithmetic is a planning number, not a measurement, and it swings with task shape, context size, and endpoint speed. Measure a real fleet-hour before sizing a plan on it.
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

### Quota structures and rate limits

- [Z.ai plan update announcement](https://docs.z.ai/devpack/notice/usage-revision) — 2026-07-30 credits migration, peak multipliers
- [Anthropic: what is the Max plan](https://support.claude.com/en/articles/11049741-what-is-the-max-plan)
- [Claude extra usage: three billing pools](https://agentshortlist.com/articles/claude-extra-usage)
- [Claude usage limits timeline](https://explainx.ai/blog/claude-usage-limits-2026-timeline-explained)
- [Cerebras Code](https://www.cerebras.ai/code) — plan tiers and model menu
- [Cerebras Code FAQ](https://support.cerebras.net/articles/9996007307-cerebras-code-faq) — published RPM/RPS/TPM/day limits per plan
- [Cerebras Inference rate limits](https://inference-docs.cerebras.ai/support/rate-limits)
- [Down and out with Cerebras Code (InfoWorld)](https://www.infoworld.com/article/4055909/down-and-out-with-cerebras-code.html) — critical field report on throughput and 429s
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
