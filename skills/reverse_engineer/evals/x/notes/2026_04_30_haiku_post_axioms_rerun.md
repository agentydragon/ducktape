# Haiku rerun after the four new SKILL.md axioms

Run: `eval_logs/20260430T064156Z/`
(`...reverse-engineer-go-crypto_aQPuTuUJZXh38GLZVzJekV.eval`)

`anthropic/claude-haiku-4-5-20251001`, default 12h budget. Finished
naturally at 9:51, 149 messages, ~$0.80 ($4.4M tokens). Score:
**0.000 / high confidence**.

## Result

What Haiku produced (`work_go_crypto_server/`):

```
README.md                  # claims "complete reverse-engineered implementation"
ncs/go.mod
ncs/main.go                # 268 lines: HTTP routing + JSON shapes + atomic counter
```

What's missing per judge: cipher (Feistel-8, S-box, round constants),
MAC (Merkle-Damgård), custom base32, ECB+PKCS#7, splitmix64 token PRNG,
session-keyed storage. Wrong endpoint paths (`/register` vs
`/v1/register`), wrong envelope (flat JSON vs `{v,op,body}`), wrong
error codes (HTTP statuses vs the 4001-4005 numeric table).

## Axiom adherence: ~zero

- **No speculation / read it or test it**: violated. Token format
  invented as `fmt.Sprintf("token_%s_%d", ...)` with no asm reading.
- **Aim for perfect artifact / mark guesses**: violated. Zero
  `GUESS:` / `STUB:` / `TODO` markers in 268 lines despite the new
  axiom requiring them. README header literally claims "complete
  reverse-engineered implementation" while implementing none of the
  crypto.
- **Expand from understood islands; don't reverse the whole binary
  in one shot**: violated. Did exactly the anti-pattern — enumerated
  strings (`"register"`, `"note.put"`, `"token"`, `"plaintext"`, ...)
  and inferred protocol shape from them without reversing any handler.
- **Diagnose, don't reroll**: not exercised — Haiku never produced
  anything to diverge against the binary, so the diagnostic axiom
  didn't have a chance to fire.

## Bottom line

Haiku is below the capability threshold for this eval. Score went
slightly _down_ compared to the no-axioms run (0.000 vs 0.085), but
that earlier 0.085 was lucky noise — Haiku's behavior is
"guess-from-strings then write a confident README about it,"
regardless of skill content. Re-running Haiku is not informative;
the signal we want comes from Sonnet.

Next: launch Sonnet at 12h budget and check whether the axioms
actually steer a model that has the capability to follow them.
