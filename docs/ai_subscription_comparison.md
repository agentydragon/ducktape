# AI Subscription Comparison — Apples-to-Apples (2026-08-21)

## Goal

Find AI capacity that complements an existing **Claude Max 20x + ChatGPT Pro** loadout for heavy agentic coding. That loadout is already at the ceiling of what either frontier vendor sells one person: Max 20x is the top individual Claude tier and ChatGPT Pro $200 the top consumer OpenAI tier, with nothing above either short of per-seat Team/Enterprise plans.

Three axes decide what to add, and they are independent:

1. **Cost per unit of intelligence** — how much useful complex work per dollar, at API list rates.
2. **Subsidy multiple** — how far below API list a subscription sells that work. This is why the loadout exists at all: the pair displaced a four-figure monthly API bill.
3. **Quota shape and failure mode** — how fast a parallel fleet drains the plan's windows, and what happens at the wall. A 429 that clears in a minute and a five-hour lockout are not the same constraint.

Optimizing any one alone gives the wrong answer. Axis 1 alone recommends list-price API, which is the bill being escaped. Axis 2 alone recommends the cheapest coding plan on offer, which is how the GLM Coding Plan looked good on paper and disappointed in practice.

## The landscape

Five structurally different ways to buy AI capacity. The category predicts the failure mode better than the vendor does.

| Category                       | Examples                                                                             | Meter                                      |
| ------------------------------ | ------------------------------------------------------------------------------------ | ------------------------------------------ |
| **Frontier labs, own subs**    | Anthropic, OpenAI, Google (AI Pro $20 / Ultra $250), xAI SuperGrok, Mistral          | Opaque; 5h + weekly windows                |
| **Chinese labs, coding plans** | Z.ai GLM, Moonshot Kimi, Alibaba Qwen, MiniMax                                       | Credits or prompts; 5h + weekly            |
| **Inference hosts, flat-rate** | Cerebras Code, Featherless, Synthetic, NanoGPT, Awan, Chutes                         | Tokens/day, requests, or concurrency slots |
| **Agent & IDE products**       | Cursor, Windsurf (Devin Desktop), Copilot, Zed Pro, DevPass, Cline Pass, OpenCode Go | Monthly request pools                      |
| **Aggregators**                | Perplexity, Poe, Kagi                                                                | Chat-shaped; not agent-shaped              |

Only the inference hosts sell anything other than a windowed quota, and they serve open weights only. That is the structural reason a fleet has no good subscription answer: the vendors with frontier models all meter in windows, and the vendors that do not have no frontier models.

### Concurrency pricing — a fourth meter, and why it does not help here

[Featherless](https://featherless.ai/) charges for **in-flight request slots** rather than tokens: unlimited tokens, capped concurrency, $10 / $25 / $75+ tiers. That sounds built for a parallel fleet until the unit math lands — a 70B-class model costs **4 concurrency units per in-flight request**, so the $75 Scale tier's 8 units buys two simultaneous large-model requests. The category rule holds generally: flat-rate concurrency suits high-volume, low-concurrency work, and pay-per-token wins for highly parallel traffic.

[Synthetic](https://synthetic.new/pricing) ($30/mo) is the closest near-miss and worth recording so it is not re-investigated: excellent menu (Kimi K3 at 512k context, GLM-5.2, Qwen3.8, gpt-oss-120b), no per-token billing, and **both** OpenAI- and Anthropic-compatible endpoints, so it is a genuine Claude Code drop-in. It fails on volume: **500 requests per 5h**, one concurrent request per model per pack. At 10-20 RPM per agent that is roughly one agent-hour, and packs stack linearly, so even ten packs at $300/mo falls short of a saturated fleet. Excellent for one or two agents; structurally wrong for ten.

[Chutes](https://chutes.ai/pricing) is a hybrid rather than a flat plan — the subscription buys queue priority and frontier access, per-token billing still applies underneath, and total usage is capped at 5x the pay-as-you-go value. A far thinner subsidy than Z.ai's stated 15-30x.

## How to compare plans at all

No standard unit exists. Four published approaches, and what each cannot see:

| Method                            | What it computes                                                                                            | Blind spot                                      |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| **Artificial Analysis**           | Index vs. cost per task, with a Pareto frontier                                                             | API pricing only; models no subscription at all |
| **Quality-adjusted tokens/$**     | tokens x quality / price, quality being a z-score blend of Arena text ELO, Arena code ELO, and the AA index | Needs a published token quota                   |
| **Value multiple**                | API-priced value of the quota / price                                                                       | Vendor-self-reported or reverse-engineered      |
| **"Actually unlimited" taxonomy** | Splits flat-rate into metered-but-fixed-price, value-multiplier gateway, and genuinely unmetered            | Descriptive, not quantitative                   |

Quality-adjusted tokens per dollar is the strongest single number available, and its appeal is real: it puts a GPU, a $20/mo plan and a $0.07/M API on one axis. Five things defeat it:

1. **Units do not convert.** Tokens, prompts, credits, premium-requests-with-multipliers, concurrency units. No conversion exists without vendor disclosure.
2. **The frontier subs are opaque.** Anthropic and OpenAI publish no token quotas, so every tokens-per-dollar figure for them — including the one in this document — is back-solved from a bill.
3. **Quality is multiplied linearly when it is not linear.** A model that fails costs a retry plus the attention to notice. Index 34 is not half as useful as index 63.
4. **Realized usage is not purchased usage.** A quota you are afraid to spend is not capacity. No published method models this, and it is the largest term for a plan with a harsh failure mode.
5. **Workload shape swings the answer by multiples.** Concurrency, burstiness, cache-hit rate and input/output ratio all move effective cost several-fold. Featherless above is the clean demonstration: same plan, opposite verdict depending on parallelism.

Hence the three axes this document uses. Cost per intelligence and subsidy multiple are the field's existing tools; **failure mode is the third axis they omit**, and for a parallel fleet it dominates the other two.

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
| **GLM-4.7 (reasoning)**        | Z.ai      |    34 |         — |       — |
| Gemma 4 31B (reasoning)        | Google    |    30 |         — |       — |
| gpt-oss-120b (high)            | OpenAI    |    24 |         — |       — |

The last three are what Cerebras actually serves (see below). They are included to show the gap, not because they compete: **GLM-4.7 at index 34 is roughly half Opus 5 and 26 points under GLM-5.3.**

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

### What Cerebras actually serves

Three separate surfaces, and only one of them is a coding plan:

| Surface                        | Models                           |            AA index |
| ------------------------------ | -------------------------------- | ------------------: |
| Cerebras Code ($50 / $200)     | GLM-4.7 only, all tiers          |                  34 |
| Public pay-as-you-go endpoints | gpt-oss-120b                     |                  24 |
|                                | Gemma 4 31B                      |                  30 |
| Dedicated endpoints            | "many additional model families" | enterprise contract |

That is the whole self-serve menu. There is no GLM-5.2, GLM-5.3, Kimi, Qwen, or DeepSeek option at any price short of a reserved-capacity contract.

**This is the same quality tier that already disappointed.** GLM-4.7's index 34 sits in the neighbourhood of the GLM generation that prompted this whole review — so buying Cerebras to escape the 5h lockout means accepting roughly the quality that was rejected on its own terms. The quota shape is genuinely better; the model is not better, and the earlier framing of "two generations behind" understated a 26-point gap.

It also runs into the feedback loop noted above: a weaker model bills for its retries, so 120M tokens/day of index-34 output completes fewer tasks than the headline number implies. The tokens-per-dollar advantage shrinks by however much rework the model causes.

**Where it still earns a place:** mechanical bulk with a cheap correctness check — codemods, test scaffolding, log triage, bulk summarisation — where index 34 is sufficient and 1000 tok/s with no rolling window is the point. Not for work where a wrong answer is expensive to detect.

**Upgrade path, worth tracking.** Cerebras serves open weights and has migrated before (GLM-4.6, then 4.7 in June 2026). GLM-5.3's weights are due roughly two weeks after its 2026-08-14 launch. A 5.3 migration would move Cerebras from index 34 to 60 and change this verdict entirely — so prefer monthly billing and re-check the model list before renewing.

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

Cerebras's multiple buys index-34 tokens while Z.ai's buys index-60 ones, so these rows are not comparable at face value. Z.ai rows convert its published 15-30x multiple at GLM-5.3 blended $2.00/M. The Claude row back-solves from a four-figure API bill displaced at Opus 5 blended ~$9/M, so it is an estimate with wide error bars. The spread is still roughly **30x between the cheapest and most expensive token pools** — and they are not interchangeable tokens.

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
| Gemma 4 31B          | $0.99                | $1.49                  | via Cerebras on OpenRouter; 262K context     |
| Gemini 3.7 Flash     | $0.75                | $3.75                  | promo; doubles 2027-01-01                    |
| DeepSeek V4-Pro 0813 | $0.66                | $1.98                  | off-peak; $1.32/$3.96 peak²                  |
| gpt-oss-120b         | $0.35                | $0.75                  | via Cerebras on OpenRouter; 131K context     |
| MiniMax M2.7         | $0.30                | $1.20                  | 205K context                                 |

¹ Sonnet 5 introductory rate runs through 2026-08-31.
² DeepSeek peak hours are 01:00–04:00 and 06:00–10:00 UTC.

## Subscription comparison

Throughput figures are order-of-magnitude. Treat them as ±2× ranges.

| Plan                    |   $/mo | Quota shape                           | Burst | Models                            | Notes                                       |
| ----------------------- | -----: | ------------------------------------- | ----- | --------------------------------- | ------------------------------------------- |
| **Cerebras Code Pro**   |     50 | 24M tok/**day**, 1M TPM, 50 RPM       | ●●○   | GLM-4.7 only (index 34)           | Daily reset; 50 RPM is the fleet constraint |
| **Cerebras Code Max**   |    200 | 120M tok/**day**, 1.5M TPM, 120 RPM   | ●●○   | GLM-4.7 only (index 34)           | Best shape here; weakest model here         |
| **Z.ai GLM Lite**       |     18 | 10k credits/wk + 5h window            | ○○○   | GLM-5.3 / 5.2 / 5.1 / 4.7         | 3× peak multiplier on 5.3                   |
| **Z.ai GLM Pro**        |     80 | 60k credits/wk + 5h window            | ○○○   | same                              | Best raw throughput/$; worst failure mode   |
| **Z.ai GLM Max**        |    168 | 140k credits/wk + 5h window           | ○○○   | same                              | Top tier; 5h wall still hit by a fleet      |
| **Claude Max 20x**      |    200 | 5h + weekly (all-models + Sonnet)     | ●●○ ³ | Opus 5, Sonnet 5                  | Opus 5 default on Max since 2026-07-25      |
| **Claude Max 5x**       |    100 | same, 5×                              | ●●○ ³ | Opus 5, Sonnet 5                  | Half the capacity of 20x for half the price |
| **ChatGPT Pro ($200)**  |    200 | 5h windows, 20× Plus                  | ○○○   | GPT-5.6, o-series, Codex          | 250 deep research runs/mo, Sora, Operator   |
| **ChatGPT Pro ($100)**  |    100 | 5h windows, 5× Plus                   | ○○○   | GPT-5.6, GPT-5.4, o3-pro, Codex   | Added 2026-04-09                            |
| **Cursor Ultra**        |    200 | 10k requests/**month**                | ●●●   | Multi-frontier routing            | Cursor-bound                                |
| **GitHub Copilot Pro+** |     39 | 1.5k premium requests/**month**       | ●●●   | Multi-frontier routing            | IDE/CLI-bound                               |
| **MiniMax Max**         |     50 | 1000 prompts/5h                       | ○○○   | M3, M2.7                          | Cheap, index ~50 tier                       |
| **Kimi Allegretto**     |   ¥199 | 5h + weekly                           | ○○○   | K3, K2.7                          | K3 is index 60; ¥699 tier adds 1M context   |
| **Qwen Standard**       |   ¥139 | 3k credits/5h, 10k/wk                 | ○○○   | Qwen3.8-Max, DeepSeek-V4-Pro, GLM | Multi-vendor routing                        |
| **Synthetic**           |     30 | 500 req/5h + weekly, 1 concur/model   | ○○○   | Kimi K3, GLM-5.2, Qwen3.8         | Claude Code drop-in; ~1 agent of volume     |
| **Featherless Scale**   |    75+ | unlimited tokens, 8 concurrency units | ●●○   | 30k+ open weights                 | 70B-class costs 4 units/request             |
| **Chutes Pro**          |      3 | 5x PAYGO value, hybrid billing        | ●●○   | Open weights                      | Subscription buys priority, not capacity    |
| **DeepSeek API**        | pay-go | none                                  | ●●●   | V4-Pro 0813, V4-Flash             | Cheapest frontier-adjacent tokens anywhere  |

³ Claude's own windows are 5h + weekly. Extra-usage credits remove the ceiling on demand, but at list API rates — availability, not capacity. Rated ●●○ on that basis, not ●●●.

## Recommendation

The loadout is at the ceiling of subsidized frontier capacity. Max 20x is the top individual Claude tier — nothing above it short of per-seat Team/Enterprise — and ChatGPT Pro $200 is likewise OpenAI's top consumer tier. Neither vendor sells more subsidized capacity to one person.

Two facts set the buying criteria:

- **The workload runs 5-10 agents in parallel.** Short rolling windows drain 5-10x faster than a serial workload implies, and per-minute request ceilings stop being theoretical — though RPM and daily token caps bind well before RPS does.
- **Claude Max 20x binds weekly, not at 5h.** Its 5h window absorbs the fleet. Z.ai GLM Max — the top Z.ai tier — did not.

So the target is a big pool whose _failure mode_ is a 429 or a predictable daily reset, never a multi-hour rolling lockout. That criterion, not tokens-per-dollar, does most of the ranking.

**Consumer subscriptions are priced for one human at a keyboard.** A 5-10 agent fleet is an order of magnitude outside that design point, which is why every plan surveyed binds _somewhere_ — 5h window, weekly cap, RPM ceiling, or daily quota. No amount of tier shopping escapes that; the tiers are all drawn against single-operator assumptions. The realistic shape is a **base plus a metered layer**: keep the frontier subscriptions for interactive work, and put fleet-scale bulk on pay-per-token capacity where the only ceiling is the bill. Expect the metered layer to be a real line item rather than a rounding error, and size the cheap-model routing to keep it small.

### The subsidy is real — a window is only fatal without an overflow path

Subscriptions are sold well below API cost, and that holds even when badly utilized. A GLM Max plan at $168 delivering only a quarter of its nominal quota before the walls deter you still works out near $0.42/M against GLM-5.3's $2.00/M blended list — roughly 5x cheaper per _delivered_ token than the API, despite three quarters of the plan going unspent. Giving up a 15-30x subsidy to escape a window is an expensive way to solve a scheduling problem.

What actually failed was not the subscription. It was the subscription **with nothing to spill into**. "Everything pauses, come back in 3.7 hours" is the behavior of a plan whose quota exhaustion has no fallback — and Anthropic's extra-usage credits exist precisely to be that fallback, just at a price that makes them unusable at scale.

Z.ai supports the pattern natively, on one key, with two independent meters — confirmed in <docs/zai_api.md>:

| Base URL                              | Meter                                       |
| ------------------------------------- | ------------------------------------------- |
| `https://api.z.ai/api/coding/paas/v4` | Coding Plan quota (5h + weekly)             |
| `https://api.z.ai/api/paas/v4`        | Pay-per-token; does **not** draw plan quota |

Burn the subsidized quota first; on exhaustion, spill to per-token at $1.40/$4.40. The 5h wall stops being a wall the moment there is somewhere to go, and the blended cost stays near the plan's rate for as long as the plan lasts each window.

### Where to buy more, ranked

1. **Z.ai GLM Max ($168) plus a pay-as-you-go balance on the same key, with failover.** This is the cheapest delivered token available at index-60 quality, and the failover is what makes the plan usable at fleet scale. The subsidy does the volume; the per-token spill absorbs the burst that used to stop everything. GLM-5.3 at index 60 is also two generations past the model that disappointed.

   **The work is in the harness, not the purchase.** Something has to detect quota exhaustion and switch base URL mid-flight. That is the real cost of this option and it should be scoped before buying — if the fleet's runner cannot fail over, this degrades to exactly the experience already rejected.

2. **Prove the model first, on pay-per-token alone.** Before committing $168/mo, run GLM-5.3 through `https://api.z.ai/api/paas/v4` (or the Anthropic-shaped endpoint — with no active plan every endpoint bills per token) for a week of real bulk work. A few dollars settles whether index 60 clears the quality bar. If it does not, the whole cheap tier is closed and no plan is worth buying.

3. **Anthropic Batch API for anything async.** 50% off list — Opus 5 at $2.50/$12.50 — and it draws from neither the 5h nor the weekly window. Frontier quality, off the constraint that actually binds. Independent of everything above, and worth doing regardless.

4. **Cerebras Code — only for mechanical bulk.** Best failure mode in the survey (no rolling window at all), worst model in it (GLM-4.7, index 34). Reconsider if GLM-5.3 weights land there.

5. **Synthetic ($30) if the fleet ever shrinks.** Kimi K3 and GLM-5.2, no per-token billing, Anthropic-compatible. Its 500 requests/5h caps it near one agent, so it answers a different workload — recorded so it is not re-investigated.

6. **Kimi and Qwen plans inherit the same 5h structure** and should be assumed to fail the same way until their short-window ceilings are checked against a fleet. Kimi K3 matches GLM-5.3 on quality (index 60); verify the ceiling before the model.

### Stretching what is already bought

Routing traffic to cheaper models is the other half and is free. Since Max is metered in tokens, cutting tokens per task raises tasks per weekly window one-for-one: Opus 5 at `medium` costs 69% less than at `max` for 4 index points, and prompt caching bills reads at ~0.1x, so a silently invalidated cache is a multiple on weekly burn rather than only on money. Parallelism makes both levers worth more, since every agent in the fleet pays the same multiplier. <aiquota/README.md> already polls Claude, Codex, and Z.ai and reports which window binds — worth reading before and after any purchase.

**Do not enable Claude extra usage as a capacity strategy.** It is list API pricing wearing a subscription's clothes, and it is the mechanism that produces four-figure months.

## Skip

- **A 5h-rolling-window plan with no overflow path** — that combination, not the window itself, is what produced the GLM Max result. Paired with per-token spill on the same key the subsidy is worth having; unpaired, a fleet drains the window several times faster than tier sizing assumes and the lockout suppresses use well beyond the hours it blocks.
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
- **Benchmarks proxy for the loop, badly.** Index scores say little about tool-call reliability, long-context coherence, or structured-output discipline — the properties that actually decide whether an agent run completes. GLM-4.7 is a case in point: index 34 overall, but Cerebras cites it as #1 on the Berkeley Function Calling Leaderboard, and tool-call reliability is what governs whether a fleet run finishes.
- **Quotas drift fast.** Z.ai re-tiered twice in 2026; OpenAI added a $100 Pro tier in April; Gemini CLI quotas change without notice. Re-check before committing.
- **Geopolitical / data-handling risk for Z.ai, DeepSeek, Moonshot, Alibaba, MiniMax.** All PRC-jurisdiction. Z.ai has been US Entity-Listed since Jan 2025. APIs carry no-train/no-store clauses but no anti-government-request carveout. Don't route proprietary code through any of them.
- **Peak-hour multipliers are easy to miss** in both directions: Z.ai's 3× peak on GLM-5.3 (14:00–18:00 SGT) and DeepSeek's 2× peak (01:00–04:00, 06:00–10:00 UTC) can double or triple an expected bill.

## Sources

### Cost-per-intelligence analysis

- [Artificial Analysis](https://artificialanalysis.ai/) — Intelligence Index vs. Cost per Task chart with Pareto frontier
- [AA Intelligence Index v4.1.1](https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index) — index composition
- [AA model leaderboard](https://artificialanalysis.ai/leaderboards/models) — per-effort scores and cost per task
- [AA GLM-4.7 vs gpt-oss-120b](https://artificialanalysis.ai/models/comparisons/glm-4-7-vs-gpt-oss-120b) — index 34 vs 24
- [AA Gemma 4 31B vs gpt-oss-120b](https://artificialanalysis.ai/models/comparisons/gemma-4-31b-vs-gpt-oss-120b) — index 30 vs 24
- [BenchLM AA leaderboard mirror](https://benchlm.ai/benchmarks/artificialanalysis)

### Quota structures and rate limits

- [Z.ai plan update announcement](https://docs.z.ai/devpack/notice/usage-revision) — 2026-07-30 credits migration, peak multipliers
- [Anthropic: what is the Max plan](https://support.claude.com/en/articles/11049741-what-is-the-max-plan)
- [Claude extra usage: three billing pools](https://agentshortlist.com/articles/claude-extra-usage)
- [Claude usage limits timeline](https://explainx.ai/blog/claude-usage-limits-2026-timeline-explained)
- [Cerebras Code](https://www.cerebras.ai/code) — plan tiers and model menu
- [Cerebras Code FAQ](https://support.cerebras.net/articles/9996007307-cerebras-code-faq) — published RPM/RPS/TPM/day limits per plan
- [Cerebras model catalog](https://inference-docs.cerebras.ai/models/overview) — public-endpoint models
- [Cerebras on OpenRouter](https://openrouter.ai/provider/cerebras) — served models and per-token pricing
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
