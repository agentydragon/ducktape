# reverse_engineer evals — TODO

This file tracks future specimens and eval variants. The
`go_crypto_server` reference under `specimens/` is the seed; everything
below is a separate planning round to scope and land.

## Additional language specimens

Each shares the same protocol shape and crypto contract as
`specimens/go_crypto_server` so that the rubric items (s-box, round
constants, base32 alphabet, MAC construction, endpoint set) can be reused
verbatim across implementations. What changes per specimen is the
build-time scrambling and the language-specific recovery angles.

- **JavaScript / Node.js** — Express-shaped server, two artifacts:
  - `terser --mangle --compress --toplevel` (mild)
  - `javascript-obfuscator` strong preset (heavy: control-flow
    flattening, string array, dead-code injection)
- **Rust** — same protocol via `axum` or `hyper`, built with
  `rules_rust` `--release` + `cargo-strip`. Tests recovery without the
  Go pclntab shortcut; relevant for measuring how much the existing skill
  leans on Go-specific tooling.

## Eval variants on top of the existing specimen

- **Garble feature coverage** — additional garble variants of the same
  source: `-literals` (literal obfuscation) and
  `GARBLE_EXPERIMENTAL_CONTROLFLOW=1` (control-flow flattening). Run
  the matrix once the skill documents defeats for these features
  (per <devinfra/skills/reverse_engineer/TODO.md>).
- **Delta RE eval** — ship `v1_garbled` + an already-RE'd reference for
  v1 + a `v2_garbled` with known protocol changes (added `/v1/note/del`
  endpoint, swapped s-box, rotated key schedule). Agent is asked to
  produce a _diff_, not a full recovery. Exercises
  <devinfra/skills/reverse_engineer/examples/binary_diff_recipe.sh>.
- **Cross-skill ablation** — same specimen, agent given the _wrong_
  skill (e.g. `info_gathering`). Should perform comparable to skill-off;
  sanity-checks that observed gains aren't mere "skill priming".
- **Skill-off control** — vanilla agent without any skill mounted, same
  binary, same prompt. Include it in the evaluation matrix below.
- **Hinted arm — per-specimen "efficient solve path"** —
  alongside `skill-on` / `skill-off`, add a `skill-on-hinted` arm where
  the agent gets specimen-specific procedural hints in addition to the
  skill. Hints describe an efficient route through this particular
  binary without revealing rubric answers. Examples for `go_crypto_server`:
  "the binary is an HTTP server; start it and probe with curl before
  reading asm", "capture a known plaintext/ciphertext pair from the
  running binary before guessing the cipher", "the protocol uses a JSON
  envelope; deduce its shape from a single registration request rather
  than from struct types in the binary". The arm answers: how much of
  the gap between skill-on and a perfect run is the agent's triage
  vs the skill's generality? Drives where to invest —
  per-specimen-runbook generation vs skill prose. Origin: the 1h
  Sonnet timeout review at
  <evals/x/notes/2026_04_29_sonnet_review_haiku.md> showed Sonnet's
  failure was triage (over-disassembly, no checkpointing,
  cipher-guessing without ground truth) rather than skill knowledge,
  and the hinted arm would isolate that.

## More Go specimens (future, distinct shapes)

- **Stack-based VM** — opcode-table recovery (each opcode is a small,
  inlined handler). Tests function-boundary detection under garble's
  function name mangling.
- **State-machine protocol** — line-based protocol with per-state
  command dispatch tables. Tests recovery of control-flow graphs and
  enum/state name reconstruction.

## Inspect AI eval followups

The Inspect AI implementation and rubric judge are in `x/`. These
followups remain open:

- **Anthropic strict tool mode** — native Anthropic tools do not yet get
  `strict: true`; consider a provider client subclass like the one used
  by `skills/eval_infra/af_chat_client.py`.
- **Multi-specimen support** — make the Inspect task and rubric selection
  work across a dataset of specimens, rather than only
  `go_crypto_server`.
- **Evaluation matrix and aggregation** — run configured skill-on/off,
  provider, model, and repeat-run combinations, then summarize the
  resulting scores so comparisons do not require manual one-off runs.
- **Automated judge checks** — exercise empty-work and reference-work
  score expectations as live-API tests behind the `live_openai_api` tag.
  The `validate_empty` and `validate_reference` targets currently provide
  manual judge checks.
- **Judge determinism** — measure score variation across repeated runs
  against the same recovered tree.
- **Conditional judge execution-cost trade-off** — if judge-container startup
  becomes the bottleneck, compare the current separate-container judge with
  stuffing the needed context into a separate judge prompt. Account for the
  extra input tokens and keep reference material isolated from the solve agent;
  change the current design only if a measured comparison favors the alternative.
- **Differential verification** — compile the recovered Go source in the
  grader and adapt the protocol checks in
  `tasks/go_crypto_server/test_smoke.py` to exercise the recovered binary,
  reporting behavioral verification separately from rubric quality.
