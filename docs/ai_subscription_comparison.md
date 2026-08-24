# AI Subscription Comparison — Apples-to-Apples (2026-08-23)

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

**Jurisdiction splits the table before price does.** Every vendor in the second row is PRC-hosted. Where the work is already public that is a non-issue and they are simply the cheapest capacity available; where it is not, the row is unavailable at any price and the comparison reduces to the frontier labs and the US inference hosts. Decide that first — it changes which half of this document applies.

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

OpenAI's current generation is a size ladder — **Luna (small), Terra (medium), Sol (large)** — so GPT-5.6 rows are three different models, not three settings of one. Luna is the interesting end: index 52 at $0.20/$1.20 after an 80% price cut on 2026-07-30, and it turns out to dominate the whole cheap half of the frontier.

### Intelligence Index vs. cost per task (2026-08)

The underlying per-model data is committed at
<artificial_analysis/intelligence_index_2026_08_23.csv> (169 models, fetched
2026-08-23), so every figure below is re-derivable and the doc is not quoting a
summary of a summary. Provenance and column meanings:
<artificial_analysis/README.md>.

#### The frontier

The cheapest model at each new index high — the Pareto line of the chart, in full.
Nothing off this list is worth buying for capability per dollar, because something
on it is both smarter and cheaper.

| Index | Cost/task | Index/$ | Model                      | Publisher | Juris. |
| ----: | --------: | ------: | -------------------------- | --------- | ------ |
|  33.9 |   $0.0088 |   3,847 | GPT-5.6 Luna (low)         | OpenAI    | US     |
|  38.0 |   $0.0104 |   3,658 | MiMo-V2.5                  | Xiaomi    | PRC    |
|  38.9 |   $0.0113 |   3,443 | GPT-5.6 Luna (medium)      | OpenAI    | US     |
|  47.0 |   $0.0216 |   2,174 | GPT-5.6 Luna (high)        | OpenAI    | US     |
|  50.1 |   $0.0316 |   1,584 | GPT-5.6 Luna (xhigh)       | OpenAI    | US     |
|  52.3 |   $0.0471 |   1,111 | **GPT-5.6 Luna (max)**     | OpenAI    | US     |
|  53.2 |   $0.2521 |     211 | DeepSeek V4-Pro 0813 (max) | DeepSeek  | PRC    |
|  53.4 |   $0.2629 |     203 | Gemini 3.7 Flash (medium)  | Google    | US     |
|  55.8 |   $0.3601 |     155 | Grok 4.5 (high)            | xAI       | US     |
|  56.8 |   $0.3992 |     142 | Muse Spark 1.2 (xhigh)     | Meta      | US     |
|  57.3 |   $0.5477 |     105 | GPT-5.6 Sol (high)         | OpenAI    | US     |
|  59.0 |   $0.6679 |      88 | Grok 4.6 (medium)          | xAI       | US     |
|  59.5 |   $0.6829 |      87 | GLM-5.3 (max)              | Z.ai      | PRC    |
|  60.9 |   $0.8367 |      73 | Grok 4.6 (high)            | xAI       | US     |
|  61.5 |   $1.2268 |      50 | Claude Opus 5 (high)       | Anthropic | US     |
|  62.5 |   $1.8012 |      35 | Claude Opus 5 (xhigh)      | Anthropic | US     |
|  63.0 |   $2.3369 |      27 | Claude Opus 5 (max)        | Anthropic | US     |

Four things fall out of it, and three of them contradict what this document said
before the data was checked.

**Luna owns the whole cheap half of the frontier.** Every frontier point between
index 33 and 52 is a Luna effort setting; nothing from any other vendor is priced
competitively in that band. That is the durable finding, and it is worth separating
from the size of the step above it, which is easy to overstate.

**The step above Luna is a factor of 2-3, not 5.** The raw frontier arithmetic says
5.4x — Luna (max) at 52.3 for 4.7 cents against DeepSeek V4-Pro 0813 at 53.2 for
25.2 cents — but that number is an artifact of two things and should not be quoted
on its own. AA prices V4-Pro 0813 at DeepSeek's **peak** rate ($1.32/$3.96); off-peak
is half that, putting the step at about 2.7x. And comparing at equal index rather
than at the next frontier point, **DeepSeek V4-Flash 0731 scores 51.8 for 11.2
cents** — half an index point below Luna (max) at 2.4x the cost. Luna is meaningfully
cheaper than the nearest DeepSeek, by roughly 2-3x rather than 5x.

**What actually makes Luna cheap is terseness, not headline price.** Cost per task is
tokens times price, and Luna wins on both. Its per-token price is ~3x under V4-Pro
0813's, and it spends far fewer reasoning tokens reaching a comparable score:

| Model (effort)             | Index | Reasoning tok/task | Input tok/task | $/task |
| -------------------------- | ----: | -----------------: | -------------: | -----: |
| GPT-5.6 Luna (max)         |  52.3 |             14,400 |        116,000 | 0.0471 |
| DeepSeek V4-Pro 0813 (max) |  53.2 |             31,200 |        270,000 | 0.2521 |
| DeepSeek V4-Flash 0731     |  51.8 |             37,100 |        288,000 | 0.1122 |

Luna reaches index 52 on **less than half** the reasoning tokens and **40% of the
input tokens**. That matters beyond price: fewer tokens per task is also less
context pressure and lower latency, and it is the property that survives a price cut
by any competitor.

Reasoning effort is the dial underneath all of this, and Luna's ladder is unusually
wide — 624 → 1,416 → 4,865 → 8,726 → 14,400 reasoning tokens per task from low to
max, index 33.9 → 52.3, cost $0.0088 → $0.0471. Choosing the effort setting is a
bigger decision than choosing between Luna and its neighbours.

**The frontier is mostly US-hosted.** Fourteen of seventeen points are US vendors;
only MiMo-V2.5, DeepSeek V4-Pro and GLM-5.3 are PRC. Ruling out PRC hosting for
sensitive work costs almost nothing on this axis — the frontier barely moves.

**GLM-5.3 is on the frontier but not distinctive on it.** Grok 4.6 (medium) sits
directly beneath it at index 59.0 for $0.6679 — half an index point and one and a
half cents apart, from a US vendor. On capability per dollar there is no measurable
reason to prefer one over the other. Any case for Z.ai has to be made on plan terms,
not model terms.

**Cerebras's model is off the frontier, and not by a little.** GLM-4.7 costs
$0.3586/task at index 34.5. Gemini 3.7 Flash (medium) is 19 index points better for
$0.2629 — cheaper _and_ far smarter. GLM-4.7 is not a budget choice; it is simply
dominated.

#### What a "task" is, and what the cost figure does not include

**A task is one benchmark item** — one HLE question, one Terminal-Bench episode, one
GDPval work product — not a unit of anyone's actual work. The headline figure is a
weighted average of the per-item cost across the nine evals, using the index weights:
summing each eval's `weightedCostPerTask` reproduces the total exactly.

**One attempt per item. Failures are not retried, and the cost of being wrong is not
in the number.** This is the single most important thing to know about the column, and
the data settles it two ways:

- **The prompt set is sent once, identically, to every model.** Across 582 model
  configurations, GPQA Diamond input tokens have a median of 49,132 and a p10-p90 of
  48,874-53,015 — a ±4% band spanning models from index 24 to index 63. HLE is the
  same (median 670,811, p10-p90 630k-776k). A model that gets a third of the items
  wrong sends no more input than one that gets them right, which is only possible if
  nothing is re-asked. Output tokens vary 46x over the same set — that is reasoning
  effort, not retries.
- **Cost rises with capability rather than falling.** Median cost per task is $0.195
  for models at index 20-35 and $1.041 for models at index 58+. If the figure priced
  attempts-until-success, the weakest models would be the most expensive.

So `$/task` is **the cost of one attempt, whether or not it succeeded.** Real work
pays for the failures too, and that gap widens as the model gets weaker. A first-order
correction — dividing by the coding solve rate, i.e. the naive cost of retrying until
one lands — shows how much:

| Model (effort)          | Coding | $/attempt | $/success (1st-order) | Penalty |
| ----------------------- | -----: | --------: | --------------------: | ------: |
| Claude Opus 5 (max)     |   78.0 |    2.3369 |                2.9968 |   1.28x |
| GPT-5.6 Sol (xhigh)     |   78.3 |    0.8072 |                1.0302 |   1.28x |
| Gemini 3.7 Flash (high) |   76.1 |    0.4022 |                0.5284 |   1.31x |
| GLM-5.3 (max)           |   74.8 |    0.6829 |                0.9135 |   1.34x |
| **GPT-5.6 Luna (max)**  |   71.5 |    0.0471 |                0.0659 |   1.40x |
| DeepSeek V4-Flash 0731  |   69.1 |    0.1122 |                0.1625 |   1.45x |
| GLM-4.7 (reasoning)     |   45.3 |    0.3586 |                0.7923 |   2.21x |

The correction is mild across the top of the table (1.28-1.45x) and does not disturb
the ranking there — Luna stays roughly 45x cheaper than Opus 5 rather than 50x. It
bites only at the bottom, where **GLM-4.7 more than doubles** and ends up costing more
per landed task than Gemini 3.7 Flash costs per landed task at 31 index points higher.

Treat that column as an illustration, not a measurement. Retries are not independent —
a model that fails a task usually fails it again, so `1/p` understates the penalty on
hard items and overstates it wherever you would give up or escalate instead. The point
is the direction: cheap-model ratios are flattered, and the flattery grows as quality
falls.

**One asymmetry worth knowing when reading Index/$.** Cost and score are not weighted
alike. Across models, 82-95% of the cost figure comes from just three evals — GDPval-AA,
Terminal-Bench and τ³-Banking, the agentic and long-context ones — while the index
those costs are divided by is diluted by five academic knowledge tests that contribute
almost nothing to spend. GPQA Diamond is 0.2-1.3% of cost and a ninth of the score.
So Index/$ divides an agentic-weighted numerator by a knowledge-diluted denominator,
which is another reason to read the coding and agentic columns instead.

#### What the index actually measures, and why it is the wrong column here

Worth being precise, because the number invites a reading it does not support.

**Mechanically it is close to "fraction of benchmark items solved."** The index is a
weighted mean of nine per-eval scores, each a solve rate in 0-1. So 0 means nothing
solved and 100 means everything solved, and the arithmetic is honest.

**But 100 is unreachable, so the scale does not mean what it looks like.** The best
model in the dataset scores 63.0, and nothing in 169 models exceeds it, because three
of the nine evals are built to resist saturation. Opus 5 (max) is made of:

| Eval               | Opus 5 (max) |
| ------------------ | -----------: |
| GPQA Diamond       |         93.2 |
| Terminal-Bench 2.1 |         89.1 |
| AA-LCR             |         75.7 |
| GDPval-AA          |         67.2 |
| SciCode            |         55.7 |
| HLE                |         54.9 |
| τ³-Banking         |         42.1 |
| AA-Omniscience     |         37.1 |
| CritPt             |         29.1 |

Index 52 is therefore not "half the tasks completed." It is 52 on a scale whose
practical ceiling today is 63.

**And the task mix is not this workload's.** Four of the nine are academic or
scientific knowledge tests (HLE, GPQA Diamond, CritPt, SciCode) and a fifth scores
knowledge with a hallucination penalty. Only three — GDPval-AA, τ³-Banking,
Terminal-Bench — resemble work. GPQA Diamond is PhD-level science multiple choice;
scoring 93 there says nothing about whether an agent run completes.

**AA publishes the columns that do fit, and they change the answer.** `coding_index`
and `agentic_index` are in the committed CSV:

| Model (effort)          | General | Coding | Agentic | $/task | Coding/$ | Agentic/$ |
| ----------------------- | ------: | -----: | ------: | -----: | -------: | --------: |
| Claude Opus 5 (max)     |    63.0 |   78.0 |    59.2 | 2.3369 |       33 |        25 |
| GPT-5.6 Sol (xhigh)     |    59.0 |   78.3 |    53.6 | 0.8072 |       97 |        66 |
| GPT-5.6 Terra (max)     |    56.6 |   76.7 |    50.2 | 0.5080 |      151 |        99 |
| Grok 4.6 (high)         |    60.9 |   76.8 |    58.7 | 0.8367 |       92 |        70 |
| Gemini 3.7 Flash (high) |    56.0 |   76.1 |    45.1 | 0.4022 |      189 |       112 |
| GLM-5.3 (max)           |    59.5 |   74.8 |    59.1 | 0.6829 |      109 |        87 |
| Claude Opus 5 (medium)  |    58.6 |   74.3 |    50.4 | 0.7243 |      103 |        70 |
| **GPT-5.6 Luna (max)**  |    52.3 |   71.5 |    46.9 | 0.0471 |    1,517 |       996 |
| DeepSeek V4-Flash 0731  |    51.8 |   69.1 |    48.4 | 0.1122 |      616 |       431 |
| GPT-5.6 Luna (high)     |    47.0 |   63.3 |    41.0 | 0.0216 |    2,932 |     1,900 |
| GLM-4.7 (reasoning)     |    34.5 |   45.3 |    26.2 | 0.3586 |      126 |        73 |

Four things this says that the general index hides.

**Coding is far more compressed than the headline suggests.** Luna (max) scores 71.5
against Opus 5's 78.0 — a 6.5-point gap where the general index shows 10.7 — at 1/50th
the cost. On coding per dollar Luna is twenty times better than anything else on the
board, and **Opus 5 (max) is off the coding frontier entirely**: Sol (xhigh) matches
its coding score (78.3 vs 78.0) for a third of the price.

**Agentic is where the spread is real, and where Luna is weakest.** Luna (max) drops
to 46.9 against Opus 5's 59.2 — a 12.3-point gap, twice its coding gap. Agentic
capability is what decides whether a long tool-use loop finishes, so this is the
number that should govern how much of the fleet moves to Luna. Expect it to do well
on bounded, well-specified work and to need supervision on long autonomous runs. It
is also the column that degrades fastest as models get cheaper, which is why picking
a bulk model on general index alone goes wrong — see the Haiku example below.

**GLM-5.3 ties Opus 5 on agentic** — 59.1 against 59.2 — at 3.4x less per task. That
is a stronger claim for it than anything in the general-index comparison, and it is
the one place in this document where Z.ai's model genuinely stands out.

**GLM-4.7 collapses on agentic to 26.2**, worse relative to the field than its general
index of 34.5 implies. For Cerebras that is the relevant number, because agent loops
are what the plan would be bought for.

**The caveat above still applies, harder.** These are benchmark aggregates, not this
codebase. They rank models; they do not tell you whether index 71.5 of coding clears
your bar on ducktape. Only running it does.

#### Worked example: Haiku 4.5 costs more than Luna and is far worse in a loop

A concrete case where the headline figures mislead and the sub-indices do not. Both
are "the cheap model" in their vendor's lineup, so intuition puts them in the same
class. They are not close.

|                        | Claude 4.5 Haiku (Reasoning) | GPT-5.6 Luna (max) |
| ---------------------- | ---------------------------: | -----------------: |
| Cost per task          |                  **$0.2174** |        **$0.0471** |
| General index          |                         29.9 |               52.3 |
| Coding index           |                         43.9 |               71.5 |
| **Agentic index**      |                     **16.5** |           **46.9** |
| τ³-Banking (tool use)  |                          9.3 |               31.1 |
| Terminal-Bench 2.1     |                         44.2 |               80.9 |
| Price /M in-out        |                $1.00 / $5.00 |      $0.20 / $1.20 |
| Cache read /M          |                        $0.10 |              $0.02 |
| Released               |                   2025-10-15 |         2026-07-09 |
| **Agentic per dollar** |                       **76** |            **995** |

Luna (max) is 2.8x Haiku's agentic score at 4.6x less per task — **13x the agentic
capability per dollar**. Three separate things produce that, and each is worth
extracting because each generalises.

**The cost inversion is price, not verbosity.** Haiku spends _fewer_ tokens per task
than Luna (88,626 input against 115,994; 17,216 reasoning against 14,393). It costs
4.6x more because it is priced 5x higher per token, cache reads included. **"The
vendor's cheap model" is a vendor-relative label, not a market position** — it means
cheap next to Opus, and says nothing about cheap next to another lab's small model.
The nine months between these two release dates covered an 80% cut to Luna, and
nothing in either model's name reflects that.

**Agentic capability collapses faster than coding as models get weaker.** This is the
structural finding, and it is why cheap models disappoint in agent loops more than
their benchmark scores suggest. Median agentic score by coding band, across all 169
models:

| Coding band |   n | Median agentic | Agentic / coding |
| ----------- | --: | -------------: | ---------------: |
| 70-80       |  34 |           49.5 |             0.66 |
| 60-70       |  22 |           40.8 |             0.63 |
| 50-60       |  29 |           30.6 |             0.56 |
| 40-50       |  23 |           21.6 |             0.48 |
| 30-40       |  19 |           11.1 |             0.32 |
| 20-30       |  21 |            4.6 |             0.19 |
| 10-20       |  15 |            1.9 |             0.12 |

The ratio falls monotonically: halving a model's coding score more than halves its
agentic score, and below coding ~40 agentic is essentially gone. A general index that
averages a gracefully-degrading skill with a cliff-edge one will always overstate the
weak end for this workload. **Read the agentic column, and treat coding 40 as the
floor below which a model cannot drive a loop at all**, whatever its other scores say.

**Haiku is weak even for its own coding level.** Among the eighteen models scoring
coding 40-48, its agentic 16.5 is second from bottom against a band median of 21.6.
Luna (low) — the cheapest setting of the cheapest frontier-adjacent model, at $0.0088
— scores coding 44.2 and **agentic 25.7**: the same coding ability, 56% more agentic
capability, at **one twenty-fifth the cost per task**.

So the surprise is not that the cost figures disagree with lived experience. It is
that the general index and cost per task were the wrong two columns to compare on, and
the dataset had the right one all along.

#### Off-frontier models worth naming

Included because they come up in plan comparisons, not because they compete on ratio.

| Model                   | Publisher | Index | Cost/task | Index/$ |
| ----------------------- | --------- | ----: | --------: | ------: |
| Claude Fable 5 (max)    | Anthropic |  62.1 |    $3.140 |      20 |
| GPT-5.6 Sol (max)       | OpenAI    |  60.9 |    $1.231 |      49 |
| Kimi K3 (max)           | Moonshot  |  59.7 |    $0.838 |      71 |
| Claude Opus 5 (medium)  | Anthropic |  58.6 |    $0.724 |      81 |
| Qwen3.8 Max             | Alibaba   |  58.1 |    $1.132 |      51 |
| GPT-5.6 Terra (max)     | OpenAI    |  56.6 |    $0.508 |     111 |
| Gemini 3.7 Flash (high) | Google    |  56.0 |    $0.402 |     139 |
| GPT-5.6 Sol (medium)    | OpenAI    |  55.6 |    $0.372 |     150 |
| Claude Sonnet 5 (max)   | Anthropic |  55.3 |    $1.717 |      32 |
| GLM-5.2 (max)           | Z.ai      |  52.6 |    $0.445 |     118 |
| DeepSeek V4-Flash 0731  | DeepSeek  |  51.8 |    $0.112 |     461 |
| GLM-4.7 (reasoning)     | Z.ai      |  34.5 |    $0.359 |      96 |
| Gemma 4 31B (reasoning) | Google    |  29.7 |      free |       — |
| gpt-oss-120b (high)     | OpenAI    |  24.1 |    $0.073 |     332 |

The last three are what Cerebras actually serves (see below), included to show the
gap rather than because they compete.

**Read Index/$ with care.** Index points are not linear in usefulness: a model that
scores 53 and fails your task costs a full retry, so the ratio systematically
flatters cheap models. The frontier view above is the honest one — the question is
not "best ratio" but "cheapest model that clears my quality bar."

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

## Converting between API prices and subscriptions

The two are only comparable through one number: **tokens consumed per month.** With it, API cost is arithmetic and a subscription's worth is the API price of the tokens it covers, divided by its fee. Without it, the comparison cannot be made at all — which is why most published analyses quietly compare models instead of plans.

**Estimating it here — and the naive estimate is wrong by an order of magnitude.** A loadout of Claude Max 20x plus top-tier ChatGPT Pro, both regularly exhausted, displaced a four-figure monthly API bill. Dividing that by an Opus-5-class blended rate of ~$9/M (80/20 input/output, no cache) gives ~100M tokens/month. **That rate does not describe agentic traffic at all.** An agent loop re-sends its whole context every turn, and those re-sends are cache reads billed at a tenth of input price, so they dominate the token count while contributing little to the bill.

Measured on one real Claude Code session (788 assistant turns, this document's own):

|                |          Tokens |
| -------------- | --------------: |
| Cache reads    |     311,444,006 |
| Cache writes   |       6,176,800 |
| Output         |         954,205 |
| Uncached input |           1,576 |
| **Total**      | **318,576,587** |

**Cache reads outnumber output 326 to 1.** At Opus 5 list — $0.50/M cache read, $10/M
cache write at the 1-hour TTL, $5/$25 in-out — that session costs **$241**, an
effective **$0.76/M**. Twelve times under the $9/M the estimate assumed. So the same
four-figure bill corresponds to **1.3-2.6B tokens/month**, not 100M.

Two consequences worth carrying forward. Any capacity figure quoted in tokens is
meaningless without the cache-hit ratio behind it — which is why third-party estimates
like "220,000 tokens per 5-hour session on Max 20x" are off by orders of magnitude
against a single session's 318M. And a long single-threaded conversation is the
best case for caching; a fleet of short-lived agents re-warms more often, so $0.76/M
is a floor and $9/M a ceiling, with real fleet traffic somewhere in $1-3/M.

**Do not mix your own token counts with AA's cost per task.** They embed different
cache behaviour: AA's benchmark runs re-warm far more often than a long conversation
does, so its implied rate for Opus 5 (max) is $3.13/M against this session's $0.76/M.
Compare AA-to-AA or measured-to-measured, never across.

### What 100M tokens/month costs at API rates

Blended 80/20 input/output, **no cache hits** — so these are worst-case rates, useful for ranking models against each other rather than for predicting a bill. Real agentic traffic runs far under them, per the measurement above.

| Model                           | $/M in | $/M out | 100M tok/mo | AA index |
| ------------------------------- | -----: | ------: | ----------: | -------: |
| **GPT-5.6 Luna**                |  $0.20 |   $1.20 |     **$40** |       52 |
| DeepSeek V4-Pro 0813 (off-peak) |  $0.66 |   $1.98 |         $92 |       53 |
| Gemini 3.7 Flash                |  $0.75 |   $3.75 |        $135 |       56 |
| Muse Spark 1.2                  |  $1.25 |   $4.25 |        $185 |       57 |
| DeepSeek V4-Pro 0813 (peak)     |  $1.32 |   $3.96 |        $185 |       53 |
| GLM-5.3                         |  $1.40 |   $4.40 |        $200 |       60 |
| Grok 4.6                        |  $2.00 |   $6.00 |        $280 |       61 |
| Claude Sonnet 5                 |  $3.00 |  $15.00 |        $540 |       55 |
| Kimi K3                         |  $3.00 |  $15.00 |        $540 |       60 |
| Claude Opus 5                   |  $5.00 |  $25.00 |        $900 |       63 |
| GPT-5.6 Sol                     |  $5.00 |  $30.00 |      $1,000 |       61 |

**This is the finding that reframes the API question.** "API is ruinously expensive" is true at frontier rates and false a few index points down. The same volume that costs $900/mo on Opus 5 costs $40 on GPT-5.6 Luna — OpenAI cut Luna 80% on 2026-07-30 — and $135 on Gemini 3.7 Flash. A fear calibrated on Opus and GPT-5.6 Sol does not transfer to the models a bulk tier would actually use.

Two things move these numbers materially. **Prompt caching** bills reads at roughly a tenth, and agent work re-sends large stable prefixes, so a well-cached loop can land far under the table. **Reasoning effort** moves spend several-fold on the models that expose it, in the same direction.

## Subscriptions, side by side

Every plan below is agent-usable; chat-only products are omitted. Blank cells are unpublished, which is itself a finding — a plan whose capacity cannot be stated cannot be compared, only tried.

| Provider    | Plan                  |       $/mo | Models (AA index)                      | Quota                               | Concurrency             | Endpoint             | Juris. |
| ----------- | --------------------- | ---------: | -------------------------------------- | ----------------------------------- | ----------------------- | -------------------- | ------ |
| Anthropic   | Max 20x               |        200 | Opus 5 (63), Sonnet 5 (55)             | 5h + weekly, sizes unpublished      | —                       | native               | US     |
| Anthropic   | Max 5x                |        100 | same                                   | same, 1/4 the size                  | —                       | native               | US     |
| OpenAI      | ChatGPT Pro           |        200 | GPT-5.6 family (52-61), Codex          | 5h windows, 20x Plus                | —                       | native (Codex)       | US     |
| OpenAI      | ChatGPT Pro           |        100 | same                                   | 5x Plus                             | —                       | native               | US     |
| Google      | AI Ultra              |     199.99 | Gemini 3.1 Pro, Deep Think             | **2,000 req/day, 120 RPM**          | —                       | CLI / Antigravity    | US     |
| Google      | AI Ultra (5x)         |      99.99 | same                                   | lower                               | —                       | same                 | US     |
| xAI         | SuperGrok Heavy       |        300 | Grok 4.5, Grok 4 Heavy                 | "maximum", unpublished              | —                       | —                    | US     |
| xAI         | Grok Build SuperHeavy | 99 (intro) | Grok Build                             | unpublished                         | **8 sub-agents**        | —                    | US     |
| Cerebras    | Code Max              |        200 | GLM-4.7 (34)                           | **120M tok/day**, 1.5M TPM, 120 RPM | —                       | OpenAI-compatible    | US     |
| Cerebras    | Code Pro              |         50 | GLM-4.7 (34)                           | 24M tok/day, 1M TPM, 50 RPM         | —                       | same                 | US     |
| Z.ai        | GLM Max               |        168 | GLM-5.3 (60), 5.2, 5.1, 4.7            | 140k credits/wk + 5h, 3x peak       | —                       | Anthropic-compatible | PRC    |
| Z.ai        | GLM Pro               |         80 | same                                   | 60k credits/wk + 5h                 | —                       | same                 | PRC    |
| Z.ai        | GLM Lite              |         18 | same                                   | 10k credits/wk + 5h                 | —                       | same                 | PRC    |
| Alibaba     | Qwen Pro              |        ~68 | Qwen3.8-Max (58), DeepSeek-V4-Pro, GLM | 12k credits/5h, 40k/7d              | **6-8 agents**          | Claude Code          | PRC    |
| Alibaba     | Qwen Standard         |        ~18 | same                                   | 3k credits/5h, 10k/7d               | **3-4 agents**          | same                 | PRC    |
| Moonshot    | Kimi Vivace           |        199 | Kimi K3 (60)                           | ~1,200 calls/5h (30x base)          | —                       | Anthropic-compatible | PRC    |
| Moonshot    | Kimi Allegro          |         99 | same                                   | 15x base                            | —                       | same                 | PRC    |
| Moonshot    | Kimi Allegretto       |         39 | same                                   | 5x base                             | —                       | same                 | PRC    |
| Moonshot    | Kimi Moderato         |         19 | same                                   | ~300 calls/5h (1x)                  | —                       | same                 | PRC    |
| MiniMax     | Max                   |         50 | M3, M2.7 (~50)                         | 1,000 prompts/5h                    | —                       | —                    | PRC    |
| Synthetic   | Pack                  |         30 | Kimi K3 (60), GLM-5.2 (53), Qwen3.8    | 500 req/5h + weekly                 | 1/model/pack            | Anthropic + OpenAI   | US     |
| Featherless | Scale                 |        75+ | 30k+ open weights                      | unlimited tokens                    | 8 units (2 large-model) | OpenAI-compatible    | US     |
| Cursor      | Ultra                 |        200 | multi-frontier routing                 | 10k requests/month                  | 8 agents                | Cursor-bound         | US     |
| GitHub      | Copilot Pro+          |         39 | multi-frontier routing                 | 1.5k premium req/month              | —                       | Copilot-bound        | US     |

### What running many agents at once actually changes

Almost nothing, on most of these plans — and it is worth being precise about that,
because "sized for a fleet" is easy to say and mostly means nothing.

**A parallel fleet is not a distinct thing a plan can be built for.** Ten agents
consume roughly ten times the tokens per wall-clock hour that one does. Against a
plan metered in tokens, credits, or requests over a period, that is the whole
effect: the same quota, reached ten times sooner. Total capacity per period versus
monthly volume is the number that decides whether a plan works, and it is the same
number whether the volume arrives from one agent or ten.

**Window length is the second-order term, and it is the one that bit.** A quota
reached ten times sooner is only a problem if reaching it stops you, and how long it
stops you for is set by the window. On a weekly cap, burning ten times faster means
hitting Friday's wall on Tuesday — visible days in advance and amortisable. On a
5-hour rolling window, it means hitting a wall you had no reason to expect,
mid-flow, and losing hours. Same arithmetic, entirely different cost. That is the
real content of the GLM Max experience, and it is a fact about window length rather
than about fleets.

**Hard concurrency caps are a separate, rarer thing, and only three vendors have
one:** Featherless prices in-flight request slots (a 70B-class model consumes 4 of
Scale's 8 units, so two simultaneous large requests), Synthetic allows one
concurrent request per model per pack, and Qwen states a supported agent count (6-8
on Pro). On those three, the eleventh agent is refused regardless of remaining
quota. Everywhere else there is no concurrency limit to speak of and the only
ceiling is per-minute request and token rates, which a fleet of this size does not
approach.

So a published agent count is **a convenience for prediction, not a merit.** Qwen
saying "6-8 agents" is more useful than Anthropic saying nothing, because it can be
checked against the intended workload without a trial — but it does not make the
plan larger, and a plan with a bigger unpublished quota can still be the better buy.
Cerebras publishing RPM/TPM/day is the same kind of usefulness: numbers to size
against, not extra capacity.

Two readings the table supports:

- **Qwen Pro is the closest published match to a 5-10 agent workload** at roughly $68, and it routes Qwen3.8-Max, DeepSeek-V4-Pro _and_ GLM behind one plan — vendor diversity inside a single subscription.
- **Z.ai is unremarkable on this table.** Comparable price, comparable models, no published concurrency, and the only plan that charges a 3x peak multiplier. Its distinguishing feature was an Anthropic-compatible endpoint, which Kimi and Synthetic also have.

## What a subscription is actually worth

Subscriptions beat the API because they are sold below API cost. The size of that discount is the whole game, and it varies by an order of magnitude between vendors.

| Plan                                |   $/mo | Included capacity       |           Value at API rates | Subsidy |
| ----------------------------------- | -----: | ----------------------- | ---------------------------: | ------: |
| Cerebras Code Max                   |    200 | 120M tok/day (~3.6B/mo) |                      ~$3,300 |    ~17x |
| Z.ai GLM (any tier)                 | 18-168 | credits                 |     vendor states 15-30x fee |  15-30x |
| Cerebras Code Pro                   |     50 | 24M tok/day (~720M/mo)  |                        ~$660 |    ~13x |
| Claude Max 20x + ChatGPT Pro (pair) |    400 | opaque                  | displaced 4-figure API spend |  >=2.5x |

Cerebras figures use GLM-4.7 at Z.ai list ($0.60/$2.20) blended 80/20 input/output; Z.ai's multiple is [its own published figure](https://docs.z.ai/devpack/notice/usage-revision) ("approximately 15-30x the monthly subscription fee").

Restated as raw capacity, which is what a weekly ceiling actually rations. This is the
intermediate step — tokens are not comparable across models, so the section below
converts these into tasks, which are:

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

### How many tasks does a plan actually buy

Tokens per dollar is still the wrong unit, because a token buys different amounts of
work at different models. The AA dataset closes that gap: it publishes **tokens
consumed per task** per model, so a plan's capacity converts into tasks, and the fee
divided by tasks gives **cost per delivered task** — directly comparable to the API
column and to every other plan.

Tokens per task for the models these plans actually serve (input including cached
replay, plus output):

| Model                  | Tokens/task | API $/task | Agentic |
| ---------------------- | ----------: | ---------: | ------: |
| GLM-4.7 (reasoning)    |      79,226 |     0.3586 |    26.2 |
| GPT-5.6 Luna (max)     |     136,040 |     0.0471 |    46.9 |
| Kimi K3 (max)          |     187,773 |     0.8375 |    54.3 |
| Claude Opus 5 (medium) |     177,829 |     0.7243 |    50.4 |
| GLM-5.3 (max)          |     440,984 |     0.6829 |    59.1 |
| Claude Opus 5 (max)    |     746,743 |     2.3369 |    59.2 |

Agentic tasks are input-dominated — the loop replays its context every turn — so these
counts are 3-10x what a single-shot prompt would suggest.

#### Where capacity is published or measured

| Plan              | $/mo | Capacity     | Tasks/mo | **$/task** | API $/task | Subsidy |
| ----------------- | ---: | ------------ | -------: | ---------: | ---------: | ------: |
| Cerebras Code Max |  200 | 120M tok/day |   45,400 | **0.0044** |     0.3586 |     81x |
| Cerebras Code Pro |   50 | 24M tok/day  |    9,100 | **0.0055** |     0.3586 |     65x |
| Z.ai GLM Max      |  168 | ~5B tok/mo¹  |   11,300 | **0.0149** |     0.6829 |     46x |

¹ Extrapolated from a 4-day sample of 665M tokens across 5,765 model calls recorded
against a Coding Max plan in <zai_api.md> (May 2026, GLM-5.1 era, previous metering).
Treat as an order of magnitude. Z.ai's own published claim is a 15-30x subsidy, which
back-converts to 3,700-7,400 tasks/mo and $0.023-$0.046/task — **two to three times
more conservative than the measurement**, which is worth knowing before sizing on
either.

#### Where only requests are published

These meter calls, not tokens, so the conversion needs a calls-per-agentic-task figure
that no vendor publishes and this document has not measured. Shown across a 10-30 band
so the assumption is visible rather than buried:

| Plan                |   $/mo | Requests/mo | $/task @30 calls | $/task @10 calls | API $/task |
| ------------------- | -----: | ----------: | ---------------: | ---------------: | ---------: |
| Synthetic Pack      |     30 |      72,000 |           0.0125 |           0.0042 |     0.8375 |
| MiniMax Max         |     50 |     144,000 |           0.0104 |           0.0035 |     0.1387 |
| Kimi Vivace         |    199 |     172,800 |           0.0345 |           0.0115 |     0.8375 |
| Google AI Ultra     | 199.99 |      60,000 |           0.1000 |           0.0333 |     0.4022 |
| Cursor Ultra        |    200 |      10,000 |           0.6000 |           0.2000 |     0.7243 |
| GitHub Copilot Pro+ |     39 |       1,500 |           0.7800 |           0.2600 |     0.7243 |

**The agent-product plans barely subsidize at all.** Cursor Ultra and Copilot Pro+ land
within a factor of two of API list under either assumption — they sell routing,
interface and support, not cheap inference. Read against a 300x spread across this
whole table, "10,000 requests a month" is not a large allowance.

#### Where it cannot be derived at all: Claude and ChatGPT

Both expose a usage API, and both report the _included_ quota as a percentage with no
denominator — the same limitation Z.ai's quota endpoint has. So the honest entry for
the two largest line items in this loadout is **unknown**, and any figure quoted for
them is an assumption wearing a number's clothes. Back-solving from a displaced API
bill shows how wide that is:

| Assumed monthly tokens | On Opus 5 (max)                  | On Opus 5 (medium)               |
| ---------------------- | -------------------------------- | -------------------------------- |
| 55M                    | 74 tasks, $2.72/task — **0.9x**  | 309 tasks, $0.65/task — **1.1x** |
| 110M                   | 147 tasks, $1.36/task — **1.7x** | 619 tasks, $0.32/task — **2.2x** |

The subsidy ranges from _worse than API_ to 2.2x depending entirely on an input that
was guessed, and the effort setting moves it as much as the volume does. That is not a
measurement, and it should not be treated as one.

**It is measurable locally, and cheaply — the raw responses carry more than the
summary.** `aiquota` reduces each provider to a utilization figure, but what the
endpoints return underneath is richer, and three parts of it are exactly what a
calibration needs:

- **Anthropic reports separate weekly windows per model family** — `seven_day_opus`
  and `seven_day_sonnet` alongside the combined `seven_day`. Burn can therefore be
  attributed per family instead of disentangled from a mixed total, which is what
  makes a short calibration accurate rather than indicative.
- **Anthropic reports extra-usage spend in absolute money** (`limit`, `used`, currency
  and exponent — e.g. a $700.00 cap against $598.86 drawn), and extra usage bills at
  list API rates. That is a dollar-denominated meter on real work, at known prices.
- **OpenAI reports per-feature windows** in `additional_rate_limits`, each with its own
  `used_percent`, `limit_window_seconds` and `reset_at`.

The percentage gauge is half an instrument; the other half is tokens counted at the
client. If a known N tokens of Opus work moves `seven_day_opus` by X%, the window's
capacity is `N / (X/100)`. One instrumented week converts both frontier plans exactly,
closes the largest gap in this document, and needs no vendor cooperation. The
extra-usage meter gives an independent cross-check in dollars. Until that is run, the
two rows stay blank rather than estimated.

#### What the table is not

**Every figure above is a ceiling that assumes saturation, and no one saturates.** The
constraint that stops you is window shape, not monthly capacity: Cerebras Code Max's
45,400 tasks/month is 1,500 a day, unreachable in practice, and Z.ai's 11,300 assumes
never hitting the 5-hour wall — which is precisely what did happen. Realized cost per
task is the fee divided by tasks _actually completed_, so a plan used at a quarter of
its ceiling costs four times the number shown.

That is the number worth acting on, and it is the one a subscription's marketing never
states. It also means the ranking here does not change what to buy on its own — a 46x
subsidy realized at 25% is still 11x, and better than a 2x subsidy realized in full.
Pair it with the failure-mode table: capacity says how much is theoretically bought,
window shape says how much survives contact with a fleet.

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

So the target is a big pool whose _failure mode_ is a 429 or a predictable daily reset, never a multi-hour rolling lockout. Between two plans of similar cost that criterion decides, but it does not decide the first question — because the frontier data says the cheapest adequate capacity has no window at all.

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

**Why not Google, given it has frontier models and no jurisdiction problem.** Because it fails the binding constraint harder than anyone. [AI Ultra](https://gemini.google/subscriptions/) ($99.99 for 5x Pro, $199.99 for 20x) caps Gemini CLI at **2,000 requests/day and 120 RPM** — at 10-20 RPM per agent a ten-agent fleet exceeds the rate limit outright and burns the daily allowance inside an hour, and Google's own docs note agent mode turns one prompt into several requests while Code Assist and the CLI share the pool. [Issue #12859](https://github.com/google-gemini/gemini-cli/issues/12859) has been open since the quota was raised and still reports single developers exhausting Ultra in 2-3 hours. Google also retired the free CLI login path on 2026-06-18, routing consumer terminal use through Antigravity. Gemini is worth buying **as API** — Gemini 3.7 Flash is index 56 at $0.40/task, the cheapest non-PRC point on the frontier — but the subscription is the wrong instrument for a fleet. Ultra's bundled $100/mo of Google Cloud credit is the one unexplored angle, if it turns out to buy Vertex inference outside the CLI quota.

### Where to buy more, ranked

The committed frontier data reorders this. The cheapest useful capacity is not a
subscription at all, and it is testable this week for the price of a coffee.

1. **Route bulk to GPT-5.6 Luna on the API, before buying anything.** Index 52 at
   $0.20/$1.20 — **$40 per 100M tokens**, against $900 for the same volume on Opus 5.
   It is the best capability-per-dollar on the board — roughly 2-3x under the nearest
   DeepSeek at the same index, and far more than that against anything above index 53
   — it needs no plan, it has no window and therefore no lockout, and it is a US
   vendor already in the loadout. Point the bulk agents at it and measure. Every other option below
   costs more and answers a question this one may close.

   **The open question is quality, and only a week of real work settles it.** Index
   52 is eleven points under Opus 5 (max). Where that clears the bar it is nearly
   free capacity; where it does not, retries eat the ratio and the cliff above
   applies.

2. **If index 52 is not enough, the next real step up is index ~59, and it costs 14x
   per task.** (Index 53-57 is available for 2-8x, but see the frontier section: the
   whole band buys 1-4 index points for multiples of Luna's cost.) Two options are indistinguishable on capability per dollar — **Grok
   4.6 (medium)** at $0.6679 and **GLM-5.3 (max)** at $0.6829 — so pick on
   jurisdiction and endpoint, not on the numbers. Grok is US-hosted and works for any
   workload; GLM is PRC-hosted, so it is available for ducktape and non-sensitive
   service logs and unavailable for anything else. Both are pay-per-token first;
   neither needs a subscription to try.

   Nothing between index 53 and 57 is worth stopping at. Gemini 3.7 Flash (medium),
   Grok 4.5 (high) and Muse Spark 1.2 sit on the frontier there, but the whole band
   costs 5-8x Luna for 1-4 index points.

3. **A Z.ai GLM Max plan ($168) only if step 2 lands on GLM _and_ the volume
   justifies it.** The plan's value is the 15-30x subsidy, and it is real even when
   badly utilized (below). What makes it usable at fleet scale is the spill path:
   Z.ai's two base URLs meter separately on one key, so quota exhaustion becomes a
   base-URL switch rather than a 3.7-hour stop.

   **The work is in the harness, not the purchase.** Something has to detect
   exhaustion and switch mid-flight. Scope that before buying — without failover this
   degrades to exactly the experience already rejected.

4. **Anthropic Batch API for anything async.** 50% off list — Opus 5 at $2.50/$12.50 —
   and it draws from neither the 5h nor the weekly window. Frontier quality, off the
   constraint that actually binds. Independent of everything above, and worth doing
   regardless.

5. **Cerebras Code — only for mechanical bulk.** Best failure mode in the survey (no
   rolling window at all), worst model in it. GLM-4.7 is not merely weak, it is
   dominated: $0.3586/task at index 34.5, where Gemini 3.7 Flash (medium) is 19 points
   better for less. Reconsider only if GLM-5.3 weights land there.

6. **Synthetic ($30) if the fleet ever shrinks.** Kimi K3 and GLM-5.2, no per-token
   billing, Anthropic-compatible. Its 500 requests/5h caps it near one agent, so it
   answers a different workload — recorded so it is not re-investigated.

7. **Kimi and Qwen plans inherit the same 5h structure** and should be assumed to fail
   the same way until their short-window ceilings are checked against a fleet. Kimi K3
   matches GLM-5.3 on quality (index 59.7); verify the ceiling before the model.

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

- **Index/$ flatters cheap models, and the dataset proves it rather than merely suggesting it.** AA's cost per task is one attempt, priced whether or not it succeeded — the eval prompt set goes out once regardless of score. A failed task costs a retry plus the operator attention to notice, and none of that is in the figure. Weight the frontier position, not the ratio.
- **AA cost-per-task is API pricing.** It does not model subscription quotas, and a plan's effective rate can beat or trail it by several times depending on saturation.
- **Token-pool figures assume saturation.** No one sustains 24M tokens/day every day; the tokens-per-dollar column is a ceiling, not an expectation, and the realized multiple depends entirely on how much load actually moves to the new plan.
- **Per-agent request and burn rates here are estimates.** The 10-20 RPM per agent behind the Cerebras arithmetic is a planning number, not a measurement, and it swings with task shape, context size, and endpoint speed. Measure a real fleet-hour before sizing a plan on it.
- **Benchmarks proxy for the loop, badly.** Index scores say little about tool-call reliability, long-context coherence, or structured-output discipline — the properties that actually decide whether an agent run completes. GLM-4.7 is a case in point: index 34 overall, but Cerebras cites it as #1 on the Berkeley Function Calling Leaderboard, and tool-call reliability is what governs whether a fleet run finishes.
- **The committed index data is a 2026-08-23 snapshot.** AA re-scores on new releases and index revisions, and models are added weekly. Re-fetch before treating any ranking here as current; the refresh recipe is in <artificial_analysis/README.md>.
- **Quotas drift fast.** Z.ai re-tiered twice in 2026; OpenAI added a $100 Pro tier in April; Gemini CLI quotas change without notice. Re-check before committing.
- **Jurisdiction is a per-workload boundary, not a vendor verdict.** Z.ai, DeepSeek, Moonshot, Alibaba and MiniMax are all PRC-jurisdiction; Z.ai has been US Entity-Listed since Jan 2025, and their APIs carry no-train/no-store clauses but no anti-government-request carveout. That disqualifies them for anything confidential and is irrelevant for anything already public — so the question is which repo the work is in, not which vendor is cheapest. Route by workload: public repositories to the cheap tier, private code and anything touching credentials to the first-party subscriptions. A blanket reading of this caveat silently removes the entire cheap tier from consideration, which is the wrong answer for a workload that is mostly open source.
- **AA prices peak-rate models at their peak rate.** DeepSeek V4-Pro 0813's $1.32/$3.96 in the committed data is the peak tariff; off-peak halves it, and any cost-per-task or Index/$ figure for a DeepSeek row is correspondingly a ceiling. Comparisons that straddle a peak-priced and a flat-priced vendor need this checked before the ratio means anything.
- **Peak-hour multipliers are easy to miss** in both directions: Z.ai's 3× peak on GLM-5.3 (14:00–18:00 SGT) and DeepSeek's 2× peak (01:00–04:00, 06:00–10:00 UTC) can double or triple an expected bill.

## Sources

### Cost-per-intelligence analysis

- <artificial_analysis/intelligence_index_2026_08_23.csv> — **the committed snapshot every index and cost-per-task figure in this document comes from** (169 models, fetched 2026-08-23); provenance in <artificial_analysis/README.md>
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
