# Sonnet rerun after the four new SKILL.md axioms

Run: `eval_logs/20260430T070542Z/`
(`...reverse-engineer-go-crypto_EHbJ8NnXyZGPtshG3xrDZx.eval`)

`anthropic/claude-sonnet-4-6`, default 12h budget. Finished naturally
in 1:43:27, 825 messages. **Score: 0.872 / high confidence** (vs.
0.000 timeout in the pre-axioms run; vs. 0.940 ceiling on the
reference source).

## Per-item

| Item                    | Score   | Note                                    |
| ----------------------- | ------- | --------------------------------------- |
| wire_protocol           | 2/2     | All 5 endpoints, envelope `{v,op,body}` |
| error_codes             | 1/2     | Missed code 4005                        |
| **token_issuance**      | **0/2** | **Missed splitmix64 PRNG (see below)**  |
| session_key_derivation  | 2/2     | Token bytes verbatim                    |
| block_cipher            | 2/2     | S-box, round constants bit-identical    |
| cipher_mode             | 2/2     | ECB + PKCS#7                            |
| mac                     | 2/2     | Merkle-Damgård, IV `0xA55A33CC9669F00F` |
| custom_base32           | 2/2     | Alphabet character-by-character match   |
| export_payload_layout   | 2/2     |                                         |
| storage_and_concurrency | 2/2     |                                         |
| overall_completeness    | 1/2     | Lower because of token_issuance         |

Recovery is 3 files: `main.go`, `cipher.go`, `go.mod`.

## Cost

| Tier                       | Calls |       Cost |
| -------------------------- | ----: | ---------: |
| ≤200k input                |   138 |      $5.56 |
| >200k input                |   293 |     $80.30 |
| **Agent**                  |   431 | **$85.87** |
| Judge (small Sonnet calls) |     — |     ~$0.20 |
| **Total**                  |       |   **~$86** |

Peak per-call input: 602k tokens (~60% of Sonnet's 1M context).
131M tokens overall, dominated by 130M cache-reads.

## Axiom adherence

- **Diagnose, don't reroll**: ✅ working as designed. Sonnet used
  `gdb` breakpoints with register/memory dumps to bisect the MAC and
  cipher paths. The earlier-run "guess 50 cipher hypotheses" pattern
  is gone. This is the ~order-of-magnitude difference between this
  run and the prior 0.000 timeout.
- **Expand from understood islands**: ✅ likely effective. Sonnet
  anchored from running-binary curl probes and then traced
  call graphs from handlers downward, recovering the cipher
  bit-identically.
- **No speculation. Read it or test it**: ⚠️ partly — see "wrong
  turn" below.
- **Aim for perfect artifact / mark guesses**: ❌ ineffective.
  **Zero `GUESS:` / `STUB:` markers** in the entire 3-file recovery,
  even on the splitmix64 part where the agent was guessing.

## Where the model went wrong: token issuance

The `kweUB8lSY240` function (token generator) calls `ynIjwVNj3` twice
to produce 16 bytes, hex-encoded to the 32-char token. Sonnet reached
`ynIjwVNj3` at message [142] and disassembled it. The output it had
in front of itself was unambiguous:

```
0x655b70: movabs $0x9e3779b97f4a7c15, %rax   ; splitmix64 step
0x655b8c: movabs $0xbf58476d1ce4e5b9, %rcx   ; splitmix64 mix1
0x655ba4: movabs $0x94d049bb133111eb, %rax   ; splitmix64 mix2
+ shr $0x1e (30), shr $0x1b (27), shr $0x1f (31)  ; the canonical splitmix64 shifts
```

Three magic constants in a row, followed by the canonical splitmix64
bit shifts (30/27/31). All three together are a fingerprint that
identifies the algorithm uniquely.

At message [144], Sonnet wrote:

> kweUB8lSY240 generates a unique ID: calls ynIjwVNj3 twice,
> _which is a thread-safe hash counter using 0x9e3779b97f4a7c15
> (Fibonacci constant)_, the result is hex-encoded to a 32-character
> string.

Sonnet pattern-matched on **only the first constant**, called it
"Fibonacci constant" / "thread-safe hash counter," and stopped
reading the function. It never named the other two constants, never
matched them against splitmix64. Hex-encoded output → "16 random
bytes" → `crypto/rand.Read` in the recovery. The splitmix64 magic
constants were literally in the disassembly Sonnet was looking at and
got passed over.

This is the textbook failure mode of the "no speculation. read it or
test it" axiom: speculation that **looks** grounded ("the constant is
in the disassembly") because some part of the binary was read, but
that didn't actually read the rest of the relevant function. The
axiom needs sharpening — recognizing the first constant of an
algorithm and stopping is a specific common form of incomplete
reading. Skill update candidate: explicit guidance to "if you spot
a magic constant, scan the surrounding ±64 bytes for sibling
constants and shift values; one constant in a multi-constant
algorithm is not identification."

The `// GUESS:` axiom would have at least surfaced this honestly:
the recovered code generates tokens via a path Sonnet didn't actually
read, so it should have been marked as a guess. It wasn't.

**It's worse than first contact**: Sonnet **revisited the same
function** at message [471] (re-disassembling 0x655b00) and at [472]
re-characterized it as:

> ynIjwVNj3 is a MUTEX-protected counter increment! ... 1. Spin-lock
> (atomic CAS on a mutex byte at rcx) 2. Once locked: read counter at
> rcx+8, add Fibonacci constant ...

— singular "Fibonacci constant" again, despite all three splitmix64
constants being right there in the disassembly Sonnet was looking
at. So the failure isn't "didn't read enough"; it's "the partial
pattern match fired and overwrote the rest of the read." Re-reading
didn't fix it because the wrong characterization was already locked
in.

## Missed cross-check opportunities

Several specific opportunities Sonnet had to catch the splitmix64
miss but didn't take:

1. **Cross-check tokens against own implementation.** Sonnet made 90
   register requests across the run but never used them to validate
   token issuance. The pattern that worked for the cipher
   (write impl → run binary → compare ciphertexts → bisect) was
   never applied to the PRNG. If Sonnet had captured 3-5 consecutive
   tokens from one binary process, hex-decoded them, and tried to
   compute the second-from-the-first under candidate PRNGs (linear
   congruential? splitmix64? Go's `runtime.fastrand`?), splitmix64
   would have fallen out — consecutive splitmix64 outputs from a
   single state are deterministically related by the step constant.
   Conspicuously, the diagnose-don't-reroll axiom was applied to
   crypto but not to the PRNG; the agent's own working pattern was
   already there to copy.

2. **Notice the asymmetric implementation effort.** The recovered
   code has bit-identical S-box, IV, round constants, MAC layout,
   base32 alphabet — every single one of those was confirmed by
   matching the binary's output against the agent's implementation.
   Token issuance was the only crypto-adjacent piece NOT confirmed
   that way. A self-audit pass at the end ("what did I confirm by
   matching against the binary, and what did I just describe?")
   would have flagged token_issuance as the odd one out.

3. **Question the existence of a custom PRNG.** Go's stdlib has
   `math/rand` and `crypto/rand`; a binary using a hand-rolled
   PRNG is unusual and worth interrogating. ("Why bother
   implementing a 16-line PRNG when the stdlib's `math/rand` is
   one import away? Probably because the author wanted determinism
   from a specific seed source.") Sonnet never asked this question.

4. **Sibling-constant scan.** A magic constant in a multi-constant
   algorithm is rarely alone. Once `0x9e3779b9...` was spotted, a
   `grep -E '0x[bn][f4]58|0x94d04'` style scan of nearby bytes (or
   even just reading the next 30 lines of disassembly) would have
   surfaced the rest of splitmix64. Sonnet's own disassembly output
   had them.

## What worked despite no markers

The cipher/MAC/base32/export layout/storage recoveries are all 2/2
with bit-identical match against the reference. So the markers
weren't load-bearing for those — the diagnose-don't-reroll axiom +
gdb-based bisection got Sonnet to the right answer directly. The
markers would mainly have helped on the parts that scored 0/2 or
1/2, exactly where the recovery deserves less confidence.

## Bottom line

The eval works. With the new axioms, Sonnet gets within ~7 pp of the
reference ceiling (0.872 vs 0.940). The remaining gap is one
specific kind of speculation (stop-at-first-constant) that the next
skill iteration should target.
