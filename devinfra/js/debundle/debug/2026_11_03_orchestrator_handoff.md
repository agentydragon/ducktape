# Debundle handoff: paused after completing the current round

The user requested that already-started work finish, agents spin down, and no
new work start. Resume only on a new user instruction. The broad goal remains
readable, idiomatic, runnable recovered modules with preserved behavior under
explicit assumptions, a shared interpreter/effect validator, and cheap iteration.

## Durable state

- [#14200](https://github.com/agentydragon/ducktape/pull/14200) merged as
  `9dcb155e32b42aed8ef9d368c34c24ab77769f9c`. Analysis compatibility now hashes
  the configured compiled `:staged_analysis` producer/reader closure, excluding
  lowering and CLI. Source/projection guards remain; graphs/snapshots retain
  full executable identity. See the [build comparisons](2026_11_03_analysis_stage_identity.md).
- [#14217](https://github.com/agentydragon/ducktape/pull/14217) merged as
  `40e16bd71aeaded772dfd6041e7effe02663d230`. Workflow/CLI docs now distinguish
  actual assignment blockers from conditional
  `tdz_after_collapsing_pass_one` reports; pruning every hypothetical owner
  unnecessarily discarded passing candidates.
- Private integration and corpus evidence belong in
  [gaffer-private #814](https://github.com/agentydragon/gaffer-private/pull/814).
  Read `devinfra/debug/debundle/2026-11-03-handoff.md` in that PR for exact
  producer heads, roster PR, completed checks, private architecture follow-ups
  and local worktree/artifact paths. Re-query merge/head state before resuming.

Both selected production pipelines completed fresh analysis and configured
builds, matching their native output byte for byte. An independent roster
restoration also improves a retained passing assignment without adding
assertions. Full canonical assignments still reject; effect validation remains
incomplete/report-only and full-app runtime equivalence is unproved.

## Resume guidance

Read [the shared workflow](../skills/shared/workflow.md#drive-specs-and-tool-improvements)
and the current project adapter. Preserve verified passing lower bounds, try
coarse candidates before fine bisection, and validate combined assignments;
ownership changes are not monotone. Count recovered bindings and inspect output
quality, including empty shells and numbered fragments.

The remaining compiled-identity decision is documented in
[staged pipeline](../plans/staged_pipeline.md). The effect-authority boundary
remains in [synchronous fallback](../plans/synchronous_execution_fallback.md)
and [translation validation](../plans/translation_validation_gate.md). Do not
promote bounded reach to gate authority without complete represented effects.

Local task scratch lives under `/tmp/debundle-cutover-20261101/` and may vanish.
Local `bazelisk` output is under `bazel-bin`; bbr materializes `bb-out`.
Confusing those roots produced a false stale-output diagnosis in this round.
The private handoff records the safe local-Bazel alternative used after
automatic review rejected remote credential forwarding. No legacy artifact
reader or compatibility bypass was added.
