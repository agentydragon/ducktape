# Native harness resource measurements

This experiment complements the bounded runner journal in #7535. It uses the actual
pinned Claude Code and Codex binaries with a loopback scripted model; it makes no
paid model requests. Each harness completes 1, 10 or 100 turns with 2 KiB text markers,
then a fresh runner/native process resumes the same durable session and sends another
request. Exact prior user and assistant messages must reach the controlled upstream.

Run `bbr test //agentplane/runner:test_native_resources`. Each shard emits a JSON
artifact with kernel-reported native RSS/high-water RSS and CPU ticks, native directory
file count/size, and resumed conversation text bytes. No process arguments, environment,
native file contents or prompt content are written into those artifacts.

The native process metrics exclude Agentplane's Python process and the model server.
They are finite samples, not a proof of bounded native memory. CPU ticks at the first
resumed request include process startup, native resume and request preparation; they
are not a wall-clock latency benchmark. Native context compaction, tool-heavy sessions,
real upstream token accounting and months of native persistence remain separate from
the Agentplane conversation storage contract.

Status: implementation prepared; measurements pending.
