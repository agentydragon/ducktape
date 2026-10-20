# `MODULE.bazel.lock`: why it stays off

**Status: open.** `--lockfile_mode=off` stays. The measured gain is small next to the cost;
revisit on the triggers at the end.

Measured 2026-10-19 at devel `5d109dcd7` on an 8-unit BuildBuddy runner (max heap 5125 MB):
full `//...` analysis (`bazel build --nobuild --config=ci`), fresh output base per run.

| `--lockfile_mode` | Wall time | Note                             |
| ----------------- | --------- | -------------------------------- |
| `off`             | 121 s     |                                  |
| `update`          | 116 s     | writes the lockfile              |
| `error`           | 105 s     | consumes it, file left untouched |

The file is 13.9 MB (1.8 MB gzipped); the `rules_distroless` `apt` entry is 10.7 MB of it.
Two generations with the same command were byte-identical on that runner; a run that also
did `bazel mod deps` first was 15 KB bigger. Reproducibility across machines was not tested.

## Benefit

- A cold server skips the `apt` extension: about 16 s of the sweep (4 s against 50 s for the
  extension alone) and the ~350 MB of Debian index downloads from `snapshot.debian.org`
  (22 `404` warnings per parse pass).
- The restart loop of #11120 cannot start when the extension never runs. #11121 already
  removes it, so this adds no safety. The trigger is old-generation occupancy after a GC,
  garbage included: on 2026-10-20, 89% and 92% were logged while the live heap peaked at 1.7 GB.

## Cost

- Every `MODULE.bazel` change (a dep bump, an `apt.install` edit) must regenerate the file.
  Parallel PRs that touch `apt` conflict on the 10.7 MB entry; the resolution is regenerating.
- `update` mode ignores a stale file silently, so the speedup disappears without a failure.
  `error` mode fails CI on every stale file.
- The content depends on which command wrote it, so worktrees can end up with a dirty file.
- `tf_repositories` is keyed `os:linux,arch:amd64`; an arm machine would add a second key.

## History

Turned off in #168 (2026-01-20) because the `ape` module, a transitive dependency of
`aspect_rules_lint`, resolved differently on dev machines and CI. `ape` is no longer in the
module graph (`bazel mod explain ape`), so the cost above is the reason now.

## Revisit when

- A cold sweep runs out of memory with a single `apt` pass, or `snapshot.debian.org` outages
  break cold sweeps.
- Cold starts matter more than `MODULE.bazel` PR conflicts.

To enable, generate with `bazel build --nobuild --lockfile_mode=update --config=ci //...`.
`bazel mod deps` reports extension errors (for example `envoy_api` importing `googleapis`) and
writes a different file.
