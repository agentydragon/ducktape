# reverse_engineer skill — changelog

Append-only log of edits to `SKILL.md`. Not packaged into the skill
tarball (`skill_package` srcs in `BUILD.bazel` only include `SKILL.md`
and `examples/`).

Each entry is a dated bullet with: what changed, what failure mode it's
addressing, and how we'll know if it helped.

## 2026-04-29

- **Added an "Aim for the perfect artifact; track everything not yet
  done." axiom.** Frames the goal explicitly (one source tree, correct
  everywhere) and codifies bookkeeping discipline: TODO list in
  `/work/`, the Census string checklist, set of un-matched binary
  symbols. Most importantly: stubs and guesses MUST be marked in the
  code (`// GUESS:` / `// STUB:` comments). Failure mode: an unmarked
  guess silently turns into "RE'd fact" the next time the file is
  read, which then propagates downstream and hides a wrong inference
  behind apparently-confirmed code. How we'll know it helped: in
  re-runs, scan the agent's `/work/` for `GUESS:`/`STUB:` markers and
  count vs total functions; healthy runs should have markers early and
  reduce them as confirmation lands.

- **Added an "Expand from understood islands; don't reverse the whole
  binary in one shot." axiom.** Failure mode: agents skim the strings
  table and infer wholesale ("I see `register`, `note`, `export`, so
  the protocol probably looks like X") without having reversed any
  individual handler. Plausible-from-far recoveries fall apart on
  contact with bytes. The axiom says: pick one location with ground
  truth (syscall, magic constant, recognizable string referent,
  already-fully-RE'd function), confirm it to satisfaction, then
  follow data/control flow outward. Sweep can _prioritize_ islands
  but the islands are what you trust. How we'll know it helped: in a
  re-run, the agent reaches anchored ground (a recognized
  constant/syscall/string referent) and walks call edges from there,
  rather than sketching an end-to-end protocol from strings alone.

- **Added a "Heuristic red flags mean you mis-RE'd something.
  Diagnose, don't reroll." axiom.** Failure mode: the same Sonnet
  rollout above detected divergence (its cipher output didn't match a
  known binary-produced ciphertext) and responded by generating ~50
  alternative cipher hypotheses + ~20 alternative implementations,
  re-running end-to-end checks each time. Cost: most of the wall
  budget. The right response is the opposite — divergence localizes
  the bug; bisect by capturing the binary's intermediate state at the
  suspect boundary (gdb breakpoints, register/memory dumps, ltrace,
  single-stepping, side-by-side disasm-vs-source) and fixing the first
  step that disagrees. The binary is right by construction (we RE'd
  from it); divergence is information about where to look, not a
  signal to swap hypotheses. The axiom is phrased generally to also
  cover internal-inconsistency signals (struct fields that don't line
  up, constants that don't match across call sites) and recovered-code
  smells (dead code, unused variables, vestigial branches) — the
  binary was real production code with no dead code, so smells in the
  recovery are invention. How we'll know it helped: in a re-run, when
  the agent hits ciphertext-mismatch, it reaches for `gdb` /
  register-dumps within a few messages instead of rolling cipher
  hypotheses for an hour.

- **Added a "No speculation. Read it or test it." axiom** to the top of
  `SKILL.md`. Failure mode it targets: speculative-but-plausible asm
  reads producing recovered code that round-trips its own self-tests
  but doesn't match the binary's actual output. Specifically observed
  in the 1h Sonnet eval rollout
  (<evals/x/notes/2026_04_29_sonnet_review_haiku.md>) — Sonnet
  identified a cipher-shaped function, wrote a Feistel implementation
  that decrypted what it encrypted, but had the wrong key-derivation
  function (it had read a related-but-not-on-call-path routine), so
  the cipher was wrong. The new axiom phrases the principle generally:
  the signal for whether your RE impl is correct must be causally
  entangled with the actual artifact, not just internally consistent.
  How we'll know it helped: re-run the eval after the change. Watch
  for (a) earlier capture of a known plaintext/ciphertext pair from
  the running binary, (b) less time spent on disconnected
  cipher-shaped functions, (c) recovery that actually matches a
  binary-produced ciphertext.
