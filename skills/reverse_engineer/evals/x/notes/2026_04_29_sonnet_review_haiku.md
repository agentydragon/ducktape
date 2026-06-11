# Haiku review of the 1h Sonnet RE rollout

**Reviewer**: `claude-haiku-4-5-20251001`, single-shot, 140k input / 2k output
**Source rollout**: `eval_logs/20260430T032735Z/...EALC3qaPdATzUgFQ5XiHx9.eval`
**Quick take**: The skill is fine. The agent's failure modes are
behavioral (triage / checkpointing), not knowledge gaps.

## Inefficiencies and loops Haiku flagged

- **[100-290]** Spent ~100 messages decoding HTTP request format via
  trial-and-error curl, only to discover at [389] that requests use an
  envelope `{"v":"ncs/1","op":"<op>","body":{...}}`. Should have been
  deduced from the struct analysis earlier.
- **[042-098]** Repeatedly grep'd for route strings instead of
  collecting all of them in one pass.
- **[306-380]** 74 messages on Go struct tag / type-descriptor offsets
  without cross-referencing the calling code.
- **[415-600]** ~50 different key-derivation hypotheses + 20 cipher
  implementations without first capturing a known plaintext/ciphertext
  pair from the running binary.

## Brute-force where it should have read off

- **[054-087]** Tried `redress`, `GoReSym`, manual pclntab parsing
  before running the binary with `--help`.
- **[100-244]** Decoded request formats from asm rather than testing
  the running server with curl (would have revealed the envelope in 2
  requests).
- **[305-400]** 95 messages on struct field analysis from hex of type
  metadata, instead of either (a) `strace`ing the running binary, or
  (b) following the `0x6561a0` validation function to its string checks.

## Workflow phases

- Phase 1 (Census) — done well early.
- Phase 2 (Skeleton) — adequate, but skeletons should land in `/work/`
  by hour 0.5. Sonnet didn't write `main.go` until [468].
- Phase 3 (Reconstruction) — major failure. Spent the entire budget on
  cryptography RE without ever fully recovering the HTTP protocol or
  shipping a working stub server.
- Phase 4 (Verification) — never reached (timed out).

## Skill improvements Haiku proposed

Concrete additions to SKILL.md (paraphrased):

1. **Early behavioral analysis before reading asm**: run with `--help`,
   probe the running binary with curl to capture protocol shape, use
   `strace`/`ltrace` for call argument capture.
2. **Capture ground-truth crypto pairs from the running binary FIRST**:
   register / put / export with known plaintext to get a reference
   ciphertext under a known key, then validate RE'd cipher against
   that pair instead of guessing.
3. **Phase-2 checkpoint discipline**: write stub source to `/work/`
   after Census, before deep asm analysis. Compile and route-test
   before digging into crypto.
4. **Crypto stub-and-move-on rule**: if cipher remains unclear after
   ~30 min, write a stub with TODOs documenting the evidence captured
   and move on; finish the rest of the recovery.
5. **Loop-detection self-prompt**: if "let me disassemble X again" or
   "let me try this key derivation" repeats, stop and ask "do I have a
   running binary? can I test this directly?"

## Checkpointing observation

**1 file write to `/work/` in 298 bash calls.** Skill should push
harder on incremental checkpointing — every ~50 messages or after
each substantive finding, dump notes to `/work/PROTOCOL_NOTES.txt`,
`/work/CRYPTO_NOTES.txt`, etc., and run `go build ./work` to keep the
implementation honest.

## Haiku's bottom line

> The agent's failure was **not** due to insufficient reverse-engineering
> skill, but rather:
>
> 1. Over-reliance on disassembly before testing the running binary
> 2. No incremental checkpointing, delaying awareness of divergence
> 3. Lack of constraint-propagation: once a known plaintext/ciphertext
>    pair was captured at [392], the agent should have pivoted to
>    brute-search over key derivations rather than guessing 50 ciphers
>
> The skill itself is well-designed; the agent needed better triage
> prioritization and earlier validation loops.

## Followup

- Land Haiku's checkpointing + run-the-binary-first guidance in
  `skills/reverse_engineer/SKILL.md`. The crypto-stub-and-move-on rule
  in particular addresses the loop Sonnet was stuck in at timeout.
- Validate by re-running the eval after the skill update: did Sonnet
  checkpoint earlier? Did it stop guessing ciphers and start
  brute-forcing key derivations against a known pair?
- See `evals/TODO.md` for the related "hinted arm" eval-variant
  proposal.
