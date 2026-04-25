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
  (per <skills/reverse_engineer/TODO.md>).
- **Delta RE eval** — ship `v1_garbled` + an already-RE'd reference for
  v1 + a `v2_garbled` with known protocol changes (added `/v1/note/del`
  endpoint, swapped s-box, rotated key schedule). Agent is asked to
  produce a _diff_, not a full recovery. Exercises
  <skills/reverse_engineer/examples/binary_diff_recipe.sh>.
- **Cross-skill ablation** — same specimen, agent given the _wrong_
  skill (e.g. `info_gathering`). Should perform comparable to skill-off;
  sanity-checks that observed gains aren't mere "skill priming".
- **Skill-off control** — vanilla agent without any skill mounted, same
  binary, same prompt. Already implied by the eval matrix but worth
  calling out explicitly.

## More Go specimens (future, distinct shapes)

- **Stack-based VM** — opcode-table recovery (each opcode is a small,
  inlined handler). Tests function-boundary detection under garble's
  function name mangling.
- **State-machine protocol** — line-based protocol with per-state
  command dispatch tables. Tests recovery of control-flow graphs and
  enum/state name reconstruction.

## Eval protocol itself (deferred to next planning round)

Land after at least one specimen + rubric is checked in:

- Sandbox shape — Microsoft Agent Framework + `scratch_exec_server` +
  the **whole skill tar** (`//skills/reverse_engineer:reverse_engineer_tar`)
  mounted at `/work/.skill/`. Stock `python:3.13-slim` base; agent has
  internet (proxy already wired in
  <skills/eval_infra/docker_exec.py>) and installs `binutils`,
  `radare2`, `file`, etc. on demand.
- Judge LLM rubric harness driven by `RUBRIC.yaml` — one critique per
  rubric item, scored 0/1/2, weighted to a 0–100 final.
- Matrix runner ({skill on/off} × {anthropic/openai} × {model} × seed),
  result aggregation following the `twenty_questions` pattern under
  <skills/info_gathering/evals/twenty_questions/>.
- Live-API offline tests for the judge, gated by
  `--test_tag_filters=-live_openai_api` per
  `//openai_utils/testing:testing.bzl`.
