# AI Subscription Comparison — Apples-to-Apples (2026-08-21)

## Goal

Find AI capacity that complements an existing **Claude Max + ChatGPT Pro** loadout for heavy agentic coding. Two metrics decide it, and they are independent:

1. **Cost per unit of intelligence** — how much useful complex work per dollar.
2. **Burst tolerance** — whether the plan's quota shape survives spiky usage. A plan metered in 5-hour windows is unusable for a workload that does nothing for two days and then wants six hours of saturated agent work.

Ignoring (2) is how the GLM Coding Plan looked good on paper and disappointed in practice. It is now a first-class axis here.

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

## Burst tolerance — the axis that actually bit

Quota shape, worst to best for spiky use:

| Shape                        | Plans                                          | Spiky-usage behavior                                          |
| ---------------------------- | ---------------------------------------------- | ------------------------------------------------------------- |
| **5h rolling + weekly**      | Z.ai GLM, Claude, ChatGPT, MiniMax, Kimi, Qwen | Worst. A burst hits the 5h wall in under an hour, then idles. |
| **Daily reset**              | Cerebras Code                                  | A big day is capped, but tomorrow is whole again.             |
| **Monthly pool**             | Cursor, GitHub Copilot, Warp, Windsurf         | Bursts freely until the month's pool is gone.                 |
| **Pay-per-token, no window** | All raw APIs; Claude extra-usage credits       | Best. No window exists to run out of.                         |

**Z.ai moved the wrong way.** The [2026-07-30 plan revision](https://docs.z.ai/devpack/notice/usage-revision) switched GLM Coding Plans from prompt counts to credits, but kept the 5-hour rolling window _and_ added a peak-hour multiplier: GLM-5.3 and GLM-5-Turbo bill at **3× during 14:00–18:00 SGT weekdays**, 1× off-peak. The specific thing that made GLM frustrating is now more punishing, not less.

**Claude has an escape valve the others don't.** [Extra usage credits](https://support.claude.com/en/articles/11049741-what-is-the-max-plan) let Pro/Max keep working past the cap at standard API rates, drawn down automatically, capped at $2,000/day redemption. Enabling it costs nothing until a burst actually overruns the plan — it converts a windowed subscription into a windowless one exactly when needed. Note the separate pools: interactive sessions draw subscription → extra usage, while unattended Agent SDK work draws its own monthly pool that does not roll over.

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
| **Claude Max 20x**      |    200 | 5h + weekly (all-models + Sonnet) | ●●● ³ | Opus 5, Sonnet 5                  | Opus 5 default on Max since 2026-07-25      |
| **Claude Max 5x**       |    100 | same, 5×                          | ●●● ³ | Opus 5, Sonnet 5                  | Half the capacity of 20x for half the price |
| **ChatGPT Pro ($200)**  |    200 | 5h windows, 20× Plus              | ○○○   | GPT-5.6, o-series, Codex          | 250 deep research runs/mo, Sora, Operator   |
| **ChatGPT Pro ($100)**  |    100 | 5h windows, 5× Plus               | ○○○   | GPT-5.6, GPT-5.4, o3-pro, Codex   | Added 2026-04-09                            |
| **Cursor Ultra**        |    200 | 10k requests/**month**            | ●●●   | Multi-frontier routing            | Cursor-bound                                |
| **GitHub Copilot Pro+** |     39 | 1.5k premium requests/**month**   | ●●●   | Multi-frontier routing            | IDE/CLI-bound                               |
| **MiniMax Max**         |     50 | 1000 prompts/5h                   | ○○○   | M3, M2.7                          | Cheap, index ~50 tier                       |
| **Kimi Allegretto**     |   ¥199 | 5h + weekly                       | ○○○   | K3, K2.7                          | K3 is index 60; ¥699 tier adds 1M context   |
| **Qwen Standard**       |   ¥139 | 3k credits/5h, 10k/wk             | ○○○   | Qwen3.8-Max, DeepSeek-V4-Pro, GLM | Multi-vendor routing                        |
| **DeepSeek API**        | pay-go | none                              | ●●●   | V4-Pro 0813, V4-Flash             | Cheapest frontier-adjacent tokens anywhere  |

³ Claude's own windows are 5h + weekly, but extra-usage credits remove the ceiling on demand — the only windowed plan here with a first-party overflow path.

## Recommendation

The GLM disappointment had two causes. Only one of them has been fixed.

**Quality: fixed.** GLM-5.3 (2026-08-14) scores 59.5–60 on the AA Intelligence Index against Opus 5's 63. Whatever was tried during the quarterly plan predates it by two model generations. On Z.ai's own Code Bench at high effort, GLM-5.3 beats Opus 4.8 (31.4% vs 29.5%) while spending ~50k output tokens per task against ~120k. It still loses badly on long-horizon agentic work — Terminal-Bench 3.0 has Opus 5 at 42.7% vs GLM-5.3 at 28.3% — so it is a good everyday model, not an Opus replacement.

**Quota shape: worse.** The credits migration kept the 5h rolling window and added the 3× peak multiplier. Renewing Z.ai would re-buy the exact problem.

In priority order:

1. **Enable Claude extra usage credits.** Free until a burst overruns the plan, then bills at standard API rates with no window at all. This is the single highest-value change available: it directly targets the spiky-overflow failure mode, on a subscription already owned, with no TOS question and no new vendor. Set a monthly cap ($50–100) to bound the downside. If it consistently exceeds $100/mo, that is the signal to move up a Max tier rather than keep paying overage.

2. **Add a DeepSeek V4-Pro API key as unmetered overflow.** $0.66/$1.98 off-peak, index 53 at $0.25/task — the cheapest point on the AA frontier, roughly 9× under Opus 5 (max) per task. Zero commitment, no window, Anthropic-compatible endpoint so existing Claude Code configs work unchanged. This is the right shape for spiky use precisely because there is nothing to run out of. Data caveat below.

3. **Cerebras Code Pro ($50) only if load turns out to be sustained rather than spiky.** Daily reset, 24M tokens/day, ~1000 tok/s. Better burst shape than Z.ai and the speed is genuinely different in an agent loop, but it serves GLM-4.7 — two generations behind, and the quality complaint applies with more force, not less.

4. **Re-test GLM-5.3 before renewing anything at Z.ai**, and test it via pay-per-token API rather than a plan. $1.40/$4.40 with no window answers the quality question without re-buying the quota problem. Only convert to a Coding Plan if the answer is yes _and_ usage has flattened out.

5. **Drop effort settings before buying capacity.** Running Opus 5 at `medium` instead of `max` is a 69% cost cut for 4 index points. On the existing Max plan that is a direct throughput increase for free — likely worth more than any tier of anything below.

## Skip

- **Another 5h-windowed subscription of any brand** — MiniMax, Kimi, Qwen tiers all repeat the GLM failure mode regardless of their per-token economics.
- **SuperGrok Heavy ($300)** — Grok 4.6 is a strong model (index 61) but at $2/$6 the API is the sane way to buy it.
- **Cursor Ultra / Copilot Pro+** — good burst shape, but the value is in their IDE surfaces; from Claude Code the routing is redundant.
- **Perplexity Max** — search-optimized, wrong tool.
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
