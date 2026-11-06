# Isolated-edit fixture timeout

PR #14613's existing `isolated_edit_test` timed out at 60 seconds twice.
Public CI invocations are `67fd1d82-463b-5513-b23e-824561c8f2ac` and
`0d6486e6-6b04-5a78-be00-118cad75025a`. The other 240 tests passed in the
first run. The parent PR's same fixture passed in 37.196 seconds; that result
was a remote cache hit. Its neutral source contains no Set construction.
These facts do not establish that the new source proof caused the timeout.

The first failed execution consumed 53.861 seconds of CPU over 60.661 seconds
of execution, peaked at 432,939,008 bytes, and reported 251,859,304 downloaded
bytes with 61.179 seconds of cumulative download duration. Input-fetch stage
time was 0.804 seconds; cumulative lazy filesystem download time must not be
reported as that stage's duration. The execution was active, but this does not
locate its unfinished operation.

The fixture previously created undeclared diagnostics only after its initial
pipeline run and both snapshot creations. Its timeout therefore left no
per-command evidence. Record those three commands through the existing
`recorded_run` helper, creating diagnostics first. A surviving `.started.json`
without its `.result.json` will identify the interrupted subprocess. Keep the
same functional assertions and timeout.

Snapshot creation copies and hashes the executable and graphs, then verifies
the staged digests (`cli/snapshot.rs`, `copy_verified`, `copy_file`,
`digest_reader`, and `verify`). Repeating this for two snapshots is a possible
cost, not a proven cause. Inspect the newly recorded step before changing the
copy/hash implementation or fixture structure.

## History-query friction

`bbapi target history --label ... --count 8` still retrieved a long repository
history and printed over a thousand entries for this one target. In
`devinfra/buildbuddy/bbapi/cmd_target.go`, `fetchAllTargetHistory` runs before
label/count filtering, and `count` bounds target groups, not statuses within a
group. Use `--since` to bound routine investigations. A future CLI improvement
should make per-target entry limits and server-side filtering explicit; do not
assume a small `count` bounds fetch cost or output for one label.
