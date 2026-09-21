# Native harness resource measurements

This experiment complements the bounded runner journal in #7535. It uses the actual
pinned Claude Code and Codex binaries with a loopback scripted model; it makes no
paid model requests. Each harness completes 1, 10 or 100 turns with 2 KiB text markers,
then a fresh runner/native process resumes the same durable session and sends another
request. Exact prior user and assistant messages must reach the controlled upstream.

Run `bbr test //agentplane/runner:test_native_resources`. Each shard emits a JSON
artifact with kernel-reported native RSS/high-water RSS and CPU ticks, native directory
regular-file logical/allocated size grouped by top-level directory, and resumed
conversation text bytes. Symlinks are counted separately; their targets do not inflate
native persistence. No process arguments, environment,
native file contents or prompt content are written into those artifacts.

The native process metrics exclude Agentplane's Python process and the model server.
They are finite samples, not a proof of bounded native memory. CPU ticks at the first
resumed request include process startup, native resume and request preparation; they
are not a wall-clock latency benchmark. Native context compaction, tool-heavy sessions,
real upstream token accounting and months of native persistence remain separate from
the Agentplane conversation storage contract.

Initial run at `e782b5b60f` completed all six measurement cases, but two empty pytest
shards made its overall result fail. The matrix now uses two shards. That first
directory scan followed file symlinks, making Codex's native directory appear to own
about 1 GiB even after one turn; its totals are not valid persistence measurements.
The corrected scan uses `lstat`, excludes symlink targets and reports per-directory
logical and allocated bytes. All six cases passed at `813573b1c0`:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/4edd2d8a-306c-41f6-919c-11889949e058).

## Observed samples

RSS is KiB. Native file size is logical bytes before restart, excluding symlink targets.
Resume CPU is kernel ticks at 100 ticks/second, measured on the new native process at
its first model request. These are independent sessions, not repeated samples of one
process; runtime scheduling and allocation noise affect comparisons.

| Harness | Completed turns | RSS before restart | RSS at resumed request | Resume CPU ticks | Native file bytes | Resumed conversation text bytes |
| ------- | --------------: | -----------------: | ---------------------: | ---------------: | ----------------: | ------------------------------: |
| Claude  |               1 |             203556 |                 201064 |               43 |             10182 |                            6194 |
| Claude  |              10 |             219288 |                 202128 |               55 |             88696 |                           43374 |
| Claude  |             100 |             263340 |                 209988 |               41 |            876141 |                          415345 |
| Codex   |               1 |             108892 |                 107808 |               13 |           3117275 |                            6194 |
| Codex   |              10 |             112632 |                 113684 |               25 |          14138973 |                           43374 |
| Codex   |             100 |             127752 |                 116784 |               30 |          97415342 |                          415345 |

Exact prior context reached the resumed upstream in every case. This confirms that
native execution still has history-dependent work even when the Agentplane browser,
projector and runner journal use bounded windows. The small resume CPU samples do not
establish asymptotic behavior or real-world wall-clock latency.

The 100-turn Claude native directory had six regular files, 876141 logical bytes and
897024 allocated bytes; its conversation file under `projects` held 874781 bytes.
The 100-turn Codex native directory had 5442 regular files, 97415342 logical bytes and
110825472 allocated bytes. Of those, `.tmp` held 5416 files and 78972792 logical bytes;
`sessions` held 1447707 bytes. Four binary-target symlinks were excluded. The profile
does not identify why Codex retains those temporary files or establish its eventual
cleanup policy. Investigate that native retention separately before extrapolating to
multi-month execution; the conversation storage change does not control it.

Artifacts are named `native-{claude,codex}-{1,10,100}-resources.json`, with per-directory
accounting and pre/post-resume samples. No disappearing files were observed during
these directory scans.
