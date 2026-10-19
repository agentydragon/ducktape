# Sonnet 4.6 hits 1h time limit without submitting

Run: `eval_logs/20260430T032735Z/`
(`...reverse-engineer-go-crypto_EALC3qaPdATzUgFQ5XiHx9.eval`)

`anthropic/claude-sonnet-4-6`, `--time-limit 3600`. Agent timed out at
1:01:03 wall, 598 messages, never called `submit`.

## Where the hour went

| Phase                 | Calls |            Total |   Mean |  p95 | Max  |
| --------------------- | ----: | ---------------: | -----: | ---: | ---- |
| **Model generation**  |   306 | **3284 s (91%)** | 10.7 s | 37 s | 86 s |
| Bash in agent sandbox |   298 |       341 s (9%) |  1.1 s |    — | 33 s |
| Scorer (judge)        |     1 |             33 s |      — |    — | —    |
| Init                  |     1 |             14 s |      — |    — | —    |

The agent was **thinking**, not running tools. Median model gen = 5.7 s,
p95 = 37 s, max = 86 s — those long ones are extended-thinking blocks
during cipher key-schedule reasoning. Bash side is fine (298 calls /
341 s, mean 1.1 s, three 28-33 s outliers consistent with `apt-get
install` or wide `objdump`).

No pathological loop, no stuck operation. Sonnet just kept thinking
thoroughly and never decided to commit to source.

## Context window utilization

| Metric                                     | Value       |
| ------------------------------------------ | ----------- |
| Peak per-call input (uncached + cache R/W) | **373,815** |
| Sonnet 4.6 nominal context                 | 1,000,000   |
| **Fraction of context used at peak**       | **~37%**    |
| Cumulative cache-read tokens               | 51,540,662  |
| Cumulative output tokens                   | 165,447     |
| Uncached input tokens                      | 1,651       |

So context-window pressure was a non-issue — Sonnet had ~63% of its
window left when time ran out.

The cache hit rate is striking: only 1,651 uncached input tokens across
the entire run vs 51.5M cache reads. Anthropic's cache absorbed the
full conversation context on every turn.

## Cost

Sonnet 4.6 has tiered pricing — calls with >200k input tokens are
charged 1.25× the base rate. Cost broken out by tier:

| Tier        | Calls | Uncached in | Cache write | Cache read |  Output |                                        Cost |
| ----------- | ----: | ----------: | ----------: | ---------: | ------: | ------------------------------------------: |
| ≤200k input |   186 |       1,532 |     207,861 | 19,445,721 |  52,456 |                                       $7.40 |
| >200k input |   119 |         119 |     174,089 | 32,094,941 | 112,991 |                                      $23.11 |
| **Total**   |   305 |             |             |            |         | **$30.51 (agent) + $0.20 (judge) ≈ $30.71** |

Notable:

- Cache reads alone were ~$30 — 45% of agent cost. At 51.5M cache-read
  tokens × $0.30-0.60/M they add up even though each token is cheap.
- Without prompt caching the run would have cost ~$300+ at base rates
  ($3/$6 per M input). Caching saved ~10×.
- ~40% of calls hit the >200k tier, accounting for ~75% of cost.

For the 12h follow-up: linear extrapolation puts cost in the
$350-450 range if Sonnet keeps generating at the same rate, with
larger and larger fraction of calls in the >200k tier as the
conversation grows.

## What the agent was actually doing

Last 12 messages: deep in a cipher-key-derivation candidate hunt. It
had:

- Located encrypt/decrypt entry points (`0x6512a0` vs `0x651320`).
- Written a Go cipher implementation that round-trips its own self-test
  (`Self-test OK: True`).
- Noticed its output didn't match the expected ciphertext on a known
  plaintext.
- Was iterating through candidate key-derivation schemes, hitting
  `"No key found in candidates!"` when wall-time fired.

So real RE progress, but stuck in a verify-then-revise loop on the
crypto core. **1 file write to `/work/` in 298 bash calls** — almost
all the work was in the agent's head, not on disk.

Even with the snapshot-on-timeout fix in place, that means the
recovered dir would have ended up nearly empty, because Sonnet hadn't
checkpointed its analysis to disk before timing out.

## Implications

1. **Sonnet needs more wall time.** 1h was insufficient; bumping to 12h
   for the next run.
2. **Sonnet may need explicit nudging to checkpoint.** Either:
   - Disclose the deadline to the agent so it knows when to stop
     verifying and start writing.
   - Add a periodic middleware that injects "checkpoint your current
     understanding to `/work/`" every N messages.
3. **Context compaction is irrelevant here.** We only used 37% of the
   1M window; no need for `inspect_ai` `compaction=` strategies.
4. **Caching is working perfectly.** `1,651` uncached input tokens
   across a 305-turn conversation — anything we change about prompt
   prefixes will tank this; tread carefully.
