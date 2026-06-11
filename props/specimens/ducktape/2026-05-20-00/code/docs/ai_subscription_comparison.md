# AI Subscription Comparison — Apples-to-Apples (2026-05-06)

## Goal

Find AI subscriptions that complement an existing **Claude Max + ChatGPT Pro** loadout for heavy agentic coding work. Metric the user actually cares about:

> How much useful complex work of the sort I have AI doing can I get per dollar?

Constraint: don't violate TOS (no piping consumer chat endpoints into agent harnesses where prohibited).

## The unit problem

Every provider meters differently, so direct comparison is hard:

| Provider        | Quota unit                                        |
| --------------- | ------------------------------------------------- |
| Claude Max      | Token budget per 5h window                        |
| ChatGPT Pro     | Per-model message caps + agent task quota         |
| GitHub Copilot  | "Premium requests" with per-model multipliers     |
| Cursor          | Dollar-denominated credit bucket at near-API cost |
| Z.ai GLM        | Prompts per 5h window + weekly cap                |
| Gemini AI Ultra | Opaque; backend tweaks frequent                   |
| SuperGrok Heavy | Opaque, marketed as "highest"                     |
| Kimi / Qwen     | Requests/month + parallel agent counts            |

To normalize, I use **frontier-task equivalent (FTE)** ≈ one substantive Claude-Code-style agent task: ~30–60 min wallclock, ~50–150 tool calls, ~500k–2M context tokens including reads. This is fuzzy. Treat all numbers as ±2× ranges, not precise estimates.

## Head-to-head benchmarks

The cleanest signal is third-party leaderboards that score competing models on **the same harness**. Vendor self-reports use different scaffolding and aren't comparable. The table below only includes pairings where both numbers come from the same source.

| Benchmark                                           | Sonnet 4.5       | Sonnet 4.6           | GLM-4.6   | GLM-4.7      | GLM-5.1                         | Source                                                                                                                            |
| --------------------------------------------------- | ---------------- | -------------------- | --------- | ------------ | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| **SWE-rebench** (independent agentic, same harness) | 60.0% / P@5 69.6 | **60.7% / P@5 70.2** | n/a       | 58.7% / 70.2 | **62.7% / 71.9**                | [swe-rebench.com](https://swe-rebench.com/), Jan–Mar 2026 window                                                                  |
| **SWE-bench Verified** (vendor harness)             | 77.2%            | **79.6%**            | ~68%      | 73.8%        | 77.8% (GLM-5)                   | [marc0.dev](https://www.marc0.dev/en/leaderboard), [llm-stats.com](https://llm-stats.com/benchmarks/swe-bench-verified), May 2026 |
| **SWE-bench Pro** (Scale public)                    | **43.6% ±3.6**   | —                    | 9.7% ±2.1 | —            | 58.4% (vendor self-report only) | [Scale Labs](https://labs.scale.com/leaderboard/swe_bench_pro_public)                                                             |
| **Terminal-Bench v1**                               | **0.500**        | —                    | 0.405     | 0.333        | —                               | [llm-stats.com](https://llm-stats.com/benchmarks/terminal-bench)                                                                  |
| **Terminal-Bench 2.0**                              | —                | 59.1%                | —         | 41.0%        | **69.0%**                       | llm-stats.com / [Z.ai docs](https://docs.z.ai/guides/llm/glm-4.7)                                                                 |
| **LiveCodeBench v6**                                | 64.0             | —                    | —         | **84.9**     | —                               | [llm-stats Sonnet 4.5 vs GLM-4.7](https://llm-stats.com/models/compare/claude-sonnet-4-5-20250929-vs-glm-4.7)                     |
| **AIME 2025**                                       | 87.0             | —                    | —         | **95.7**     | —                               | llm-stats compare page                                                                                                            |
| **GPQA Diamond**                                    | 83.4             | **89.9**             | —         | 85.7         | 86.2                            | [llm-stats Sonnet 4.6 vs GLM-5.1](https://llm-stats.com/models/compare/claude-sonnet-4-6-vs-glm-5.1)                              |
| **MMLU-Pro**                                        | —                | **89.3**             | —         | 84.3         | —                               | [llm-stats Sonnet 4.6 vs GLM-4.7](https://llm-stats.com/models/compare/claude-sonnet-4-6-vs-glm-4.7)                              |

**Aider Polyglot** and **BFCL v3**: no head-to-head exists on public leaderboards for current Sonnet vs current GLM. Anyone citing those is mixing runs from different harnesses.

### What the head-to-head data actually says

- On the only **independent agentic-coding eval** (SWE-rebench, identical harness): **GLM-5.1 edges Sonnet 4.6 by ~2 pts** (62.7 vs 60.7). Pass@5 is tied. This is the cleanest signal.
- Sonnet 4.6 wins vendor SWE-bench Verified by ~2 pts.
- Terminal-Bench flipped between versions: Sonnet won v1, **GLM-5.1 wins v2.0 by 10 pts** (69.0 vs 59.1).
- Math/contest coding: GLM is clearly ahead.
- Knowledge (GPQA, MMLU-Pro): Sonnet leads by ~3–5 pts.

Net: **GLM-5.1 is within noise of Sonnet 4.6 on agentic coding**, modestly behind on knowledge benchmarks, ahead on contest coding and Terminal-Bench 2.0. Earlier "GLM trails by 15–20 pts" framings were based on GLM-4.6 (Sept 2025) numbers, not the current flagship.

## Quality factor

Per-FTE quality multiplier vs Sonnet 4.6 baseline (1.00). Squared from coding-eval ratio to penalize failed-task overhead:

| Model               | Source benchmark         | Q (squared) |
| ------------------- | ------------------------ | ----------- |
| Claude Opus 4.7     | SWE-bench Verified 87.6% | 1.21        |
| GPT-5.5             | SWE-bench Verified 88.7% | 1.24        |
| Gemini 3 Pro        | SWE-bench Verified 80.6% | 1.03        |
| Claude Sonnet 4.6   | baseline                 | 1.00        |
| **GLM-5 / GLM-5.1** | SWE-rebench head-to-head | **~1.00**   |
| Claude Sonnet 4.5   | SWE-bench Verified 77.2% | 0.94        |
| **GLM-4.7**         | SWE-rebench head-to-head | **~0.94**   |
| GPT-5               | SWE-bench Verified ~75%  | 0.89        |
| Grok Code Fast 1    | SWE-bench Verified 70.8% | 0.79        |
| Kimi K2.6           | SWE-bench Verified ~70%  | 0.77        |
| GLM-4.6             | SWE-bench Verified 68.0% | 0.73        |
| Mistral Medium 3    | SWE-bench Verified ~60%  | 0.57        |

GLM Q values come from the SWE-rebench head-to-head (cleanest signal); SWE-bench Verified would give GLM-5.1 a similar ~0.96 anyway. Use these as priors, not verdicts — your actual workflow may weight long-context handling, tool-call reliability, or multimodal differently.

## Comparison — effective FTE per dollar per month

| Plan                     | $/mo | Raw FTE/mo                          | Model                                       | Q²         | **Effective FTE / $** | TOS / agent fit                                    |
| ------------------------ | ---- | ----------------------------------- | ------------------------------------------- | ---------- | --------------------- | -------------------------------------------------- |
| **Z.ai GLM Max**         | 80   | 500–1000                            | GLM-4.6 / 4.7 / 5 / 5.1                     | 0.73–1.00  | **5–13**              | Anthropic-compatible endpoint; drop-in Claude Code |
| **Z.ai GLM Pro**         | 30   | 150–250                             | GLM-4.6 / 4.7 / 5 / 5.1                     | 0.73–1.00  | **4–8**               | Same                                               |
| **Z.ai GLM Lite**        | 10   | 40–80                               | GLM-4.6 / 4.7 / 5 / 5.1                     | 0.73–1.00  | **3–8**               | Same                                               |
| **Qwen Coding Pro**      | ~50  | 200–400                             | Qwen3.5+ / GLM-5 / Kimi K2.5 / MiniMax-M2.5 | 0.77–1.00  | **3–8**               | CLI-friendly, programmatic OK                      |
| **Claude Max 20x** (ref) | 200  | 600–1200                            | Sonnet 4.6 / Opus 4.7                       | 1.00–1.21  | **3–7**               | First-party                                        |
| **Mistral Le Chat Pro**  | 15   | 30–90                               | Mistral Medium 3                            | 0.57       | **1–3**               | Quality gap; chat-focused                          |
| **Claude Max 5x** (ref)  | 100  | 150–300                             | Sonnet 4.6 / Opus 4.7                       | 1.00–1.21  | **1.5–4**             | First-party                                        |
| **GitHub Copilot Pro+**  | 39   | 30–150                              | Routes Sonnet 4.6 / Opus / GPT-5 / Gemini 3 | ~0.95–1.20 | **0.7–4**             | IDE-bound; June 2026 billing change                |
| **Kimi Vivace**          | 199  | 300–600                             | Kimi K2.6                                   | 0.77       | **1–2**               | Anthropic-compatible; long-horizon strong          |
| **Cursor Ultra**         | 200  | 150–400 (drains by day 25)          | Multi (Sonnet / GPT-5 / Gemini 3)           | ~0.95–1.20 | **0.7–2.5**           | Cursor-bound                                       |
| **ChatGPT Pro** (ref)    | 200  | 150–400                             | GPT-5 / GPT-5.5 / o-series                  | 0.89–1.24  | **0.7–2.5**           | First-party                                        |
| **Google AI Pro**        | 20   | 10–30                               | Gemini 3 Pro                                | 1.03       | **0.5–1.5**           | Casual-use tier                                    |
| **SuperGrok Heavy**      | 300  | 100–300 (unknown)                   | Grok 4 / Grok Code Fast                     | 0.79       | **<1**                | Coding trails frontier; math-strong                |
| **Google AI Ultra**      | 250  | 50–150 (CLI quota-bound)            | Gemini 3 Pro                                | 1.03       | **0.2–0.6**           | Frontier model, throttled CLI                      |
| **Perplexity Max**       | 200  | search-optimized; 10k agent credits | Routes Sonnet / GPT-5                       | ~0.95      | **<0.5**              | Wrong tool for agent work                          |

## Headline reading

- **Z.ai GLM** dominates effective-work-per-dollar by ~3–5× over Claude Max and ChatGPT Pro. With GLM-5.1 in the model menu, the quality gap to Sonnet 4.6 closes to ~within-noise on agentic coding (SWE-rebench head-to-head). Earlier framings of "GLM trails by 15-20 pts" were based on the older GLM-4.6 (Sept 2025) numbers — the current flagship is much closer. Frontier hard tasks (deep refactor, novel design) still favor Opus 4.7 / GPT-5.5.
- **Claude Max 20x** ($200) is ~2× Claude Max 5x ($100) per dollar, not just 2× total — the 20x tier is the better deal if you're saturating the 5x tier.
- **Gemini AI Ultra** has a frontier model but the CLI quota is the bottleneck (open issue google-gemini/gemini-cli#12859). Worth it only if you specifically need Gemini 3 Pro's strengths (1M context, multimodal).
- **Cursor Ultra** and **Copilot Pro+** are sensitive to your client preferences. If you live in their UIs, the multi-model routing has real value; if you're driving everything from Claude Code / Codex CLI, the per-dollar math doesn't favor them.
- **SuperGrok Heavy**'s $300 buys math/reasoning wins that don't transfer cleanly to coding.

## Recommendation

For a heavy agent user already maxed on Claude Max + ChatGPT Pro:

1. **Z.ai GLM Pro ($30)** — biggest marginal capacity per dollar by a wide margin. Set `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic` + `ANTHROPIC_AUTH_TOKEN=<glm-key>` and your existing Claude Code / harness configs work unchanged. With GLM-5.1, the quality is close enough to Sonnet 4.6 to handle most everyday work, not just routine. Reserve Claude Opus / Sonnet 4.6 for the hardest tasks where the quality factor pulls ahead.
2. **Step up to GLM Max ($80)** only after you've sustained Pro saturation for ~2 weeks. Numbers above suggest Max scales linearly with price, but heavy-tier quotas can have hidden ceilings.
3. **GitHub Copilot Pro+ ($39)** if you spend serious time in VS Code/JetBrains and want IDE-integrated multi-model routing. Reassess after the 2026-06-01 shift to usage-based billing.
4. **Google AI Ultra ($250)** only if Gemini 3 Pro's specific strengths matter to your work. Don't buy it as a workhorse.

## Skip

- SuperGrok Heavy ($300): coding trails Sonnet/Opus
- Perplexity Max ($200): search-optimized
- Mistral / DeepSeek / Poe / Kagi: quality or quota insufficient
- Cursor Ultra: redundant given Claude Max + GLM overflow

## TOS notes

- **Z.ai GLM Coding Plan**: explicitly markets Claude Code/Cursor/Cline integration via Anthropic-compatible endpoint. Programmatic use is the intended use case.
- **Kimi (Moonshot)**: same pattern — `api.moonshot.ai/anthropic` endpoint is documented for agent harnesses.
- **Qwen Coding Plan**: documented CLI/agent integration.
- **Claude Max / ChatGPT Pro**: don't pipe the consumer chat endpoints into third-party agents — use the API (separate billing) for that. Both vendors enforce.
- **GitHub Copilot**: bound to Copilot clients (IDE plugins, Copilot CLI, Copilot agent). No raw API key for harness reuse.

## Caveats — why this is fuzzy

- FTE is a made-up unit. Your actual task profile may compress or expand by 3×.
- Quotas drift. Z.ai re-tiered in early 2026; Copilot is mid-billing-model change; Gemini CLI quotas change without notice.
- Quality factor uses SWE-bench Verified / SWE-rebench as proxy. Models can be strong on these but weak in your actual loop (tool calling, long context coherence, structured output reliability).
- Multi-model plans (Copilot, Cursor) self-penalize via routing caps — headline numbers overstate real throughput.
- Z.ai Coding Plan model selection: assumes you can route to GLM-5.1 most of the time. If the plan defaults to GLM-4.6 for cost reasons, the effective Q drops to the lower bound (~0.73).
- Geopolitical / data-handling risk for Z.ai: Beijing-based, US Entity-Listed (Jan 2025); API has no-train/no-store clauses but PRC parent and no anti-government-request carveout. Don't send proprietary code through it.

## Sources

### Z.ai / GLM

- [Z.AI GLM Coding Plan](https://z.ai/subscribe), [pricing](https://docs.z.ai/guides/overview/pricing), [Claude Code integration](https://docs.z.ai/devpack/tool/claude)
- [Anthropic API format for GLM](https://aiengineerguide.com/til/anthropic-api-format-glm-coding-plan/)
- [Z.ai GLM-4.7 docs](https://docs.z.ai/guides/llm/glm-4.7)
- [GLM-4.6 vs Sonnet 4.5 review](https://medium.com/data-science-in-your-pocket/glm-4-6-the-best-coding-llm-beats-claude-4-5-sonnet-kimi-88e8e3f96863)
- [GLM-4.6 SWE-bench writeup](https://intuitionlabs.ai/articles/glm-4-6-open-source-coding-model)
- [GLM-5.1 review (Serenities)](https://serenitiesai.com/articles/glm-5-1-zhipu-coding-benchmark-claude-opus-comparison-2026)
- [apiyi GLM-5.1 vs Sonnet 4.6 6-dim test](https://help.apiyi.com/en/glm-5-1-vs-claude-sonnet-4-6-coding-comparison-en.html)

### Independent leaderboards (head-to-head data)

- [SWE-rebench](https://swe-rebench.com/) — same-harness agentic coding eval, primary head-to-head signal
- [marc0.dev SWE-bench Verified leaderboard](https://www.marc0.dev/en/leaderboard)
- [llm-stats.com SWE-Bench Verified](https://llm-stats.com/benchmarks/swe-bench-verified), [Terminal-Bench](https://llm-stats.com/benchmarks/terminal-bench)
- [llm-stats Sonnet 4.5 vs GLM-4.7](https://llm-stats.com/models/compare/claude-sonnet-4-5-20250929-vs-glm-4.7)
- [llm-stats Sonnet 4.6 vs GLM-4.7](https://llm-stats.com/models/compare/claude-sonnet-4-6-vs-glm-4.7)
- [llm-stats Sonnet 4.6 vs GLM-5.1](https://llm-stats.com/models/compare/claude-sonnet-4-6-vs-glm-5.1)
- [llm-stats Sonnet 4.6 vs GLM-5](https://llm-stats.com/models/compare/glm-5-vs-claude-sonnet-4-6)
- [Scale Labs SWE-bench Pro public](https://labs.scale.com/leaderboard/swe_bench_pro_public)
- [vals.ai SWE-bench](https://www.vals.ai/benchmarks/swebench), [Terminal-Bench 2.0](https://www.vals.ai/benchmarks/terminal-bench-2)
- [Aider chat leaderboard](https://aider.chat/docs/leaderboards/) (no current Sonnet 4.5+ / GLM-4.7+ entries)

### Other models

- [Claude Opus 4.7 benchmarks (Vellum)](https://www.vellum.ai/blog/claude-opus-4-7-benchmarks-explained)
- [GPT-5.5 benchmarks (BenchLM)](https://benchlm.ai/models/gpt-5-5)
- [Gemini 3.1 Pro benchmarks](https://benchlm.ai/models/gemini-3-1-pro)
- [Grok Code Fast 1 SWE-bench](https://x.ai/news/grok-code-fast-1)

### Z.ai company / risk

- [Z.ai Wikipedia](https://en.wikipedia.org/wiki/Z.ai)
- [Zhipu Entity List addition (SCMP)](https://www.scmp.com/tech/tech-war/article/3295002/tech-war-us-adds-chinese-ai-unicorn-zhipu-trade-blacklist-bidens-exit)
- [Z.ai Terms of Use](https://docs.z.ai/legal-agreement/terms-of-use), [Privacy Policy](https://chat.z.ai/legal-agreement/privacy-policy)
- [ChinaTalk: The Z.ai Playbook](https://www.chinatalk.media/p/the-zai-playbook)

### Other subscriptions

- [Google AI subscriptions](https://gemini.google/subscriptions/), [Gemini Apps limits](https://support.google.com/gemini/answer/16275805?hl=en)
- [Gemini CLI quota issue #12859](https://github.com/google-gemini/gemini-cli/issues/12859)
- [SuperGrok pricing](https://felloai.com/grok-pricing/), [SuperGrok Heavy launch](https://www.tesery.com/blogs/news/xai-launches-grok-4-with-new-300-month-supergrok-heavy-subscription)
- [GitHub Copilot plans](https://github.com/features/copilot/plans), [requests model](https://docs.github.com/en/copilot/concepts/billing/copilot-requests), [usage-based billing shift](https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/)
- [Cursor pricing](https://cursor.com/pricing), [tier discussion](https://forum.cursor.com/t/cursor-200-vs-claude-max-cursor-usage-limits-and-trade-offs/148298)
- [Mistral pricing](https://mistral.ai/pricing), [Le Chat tiers](https://help.mistral.ai/en/articles/347532-what-is-the-difference-between-le-chat-free-pro-team-and-enterprise)
- [Kimi K2.6 pricing](https://kimik2ai.com/pricing/), [Anthropic-compatible endpoint](https://www.atlascloud.ai/blog/guides/one-api-key-four-tools-how-to-use-kimi-k2-6-in-hermes-agent-opencode-claude-code-openclaw-full-2026-setup)
- [Alibaba Qwen Coding Plan](https://www.alibabacloud.com/help/en/model-studio/coding-plan)
- [Perplexity Max](https://www.glbgpt.com/hub/how-much-is-perplexity-max-subscription/)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing)
