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
logical and allocated bytes. Corrected measurements are pending.
