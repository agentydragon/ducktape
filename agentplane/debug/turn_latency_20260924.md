# One-word turn latency on `agentplane-testing` — 2026-09-24

`//agentplane/acceptance:test_thread_latency` asks each harness to say PING and times the turn from
the app admitting the input to `TurnCompleted` arriving on the thread's event stream. Against a 3 s
ceiling it measured **7.51 s** on Claude (`claude-haiku-4-5`) and **5.22 s** on Codex
(`gpt-5.6-luna`, low effort). The harnesses report their own turns as 1.26 s and 3.82 s.

**Most of the rest is the runner's journal.** It commits every Event in its own SQLite transaction,
one after another, at about 90 ms each. The harness finishes and its output waits in the stdout
pipe while the runner works through it: 5.9 s of the Claude turn and 1.1 s of the Codex turn.

App `devel-20260923231228-b867c67`, llm-ingress `devel-20260920014010-89ebdd6`, Electric 1.8.1 on
the emptyDir it moved to that night. Threads `d9372034-4c33-4761-a92d-2818c68289d6` (Claude) and
`89c598af-43a4-46a9-a3fa-6fe6c6f1083a` (Codex).

## Where the timestamps come from

- **Runner `Event.at`**: stamped in `Journal._append` (<../runner/journal.py>) when the runner
  starts committing that entry, not when the harness wrote the line. The commit follows.
- **Codex `emittedAtMs`** on its native frames: the harness's own emit time.
- **Claude's `result` frame**: `request_sent_wall_ms`, `time_to_request_ms`, `ttft_stream_ms`,
  `duration_api_ms` and `duration_ms`.
- **llm-ingress's log**: httpx logs a line when LiteLLM's response headers arrive.
- **The test's clock**: `Turn.admitted`, when the app answers the input, and when `TurnCompleted`
  arrives.

The runner and the harness share a container clock. llm-ingress runs on another node. Comparing its
log with the sandbox's timestamps assumes the two node clocks agree to well under 0.1 s; nobody
checked that.

## Claude: 7.51 s

t = 0 is `CommandAdmitted.at`, 00:10:26.614.

| #   | Stage                                                                                     | t (s)         | Took   | Whose   |
| --- | ----------------------------------------------------------------------------------------- | ------------- | ------ | ------- |
| 1   | Commit `CommandAdmitted`, `TurnStarted` and the outbound user frame, then write the frame | 0.000 → 0.368 | 0.37 s | runner  |
| 2   | Harness builds and sends the model request (`time_to_request_ms`)                         | 0.368 → 0.403 | 0.04 s | harness |
| 3   | llm-ingress → LiteLLM → Anthropic, to response headers                                    | 0.403 → 1.291 | 0.89 s | model   |
| 4   | Stream to end of turn: 56 output tokens, 49 of them thinking                              | 1.291 → 1.624 | 0.33 s | model   |
| 5   | Runner works through the buffered output, up to `TurnCompleted.at`                        | 1.624 → 7.522 | 5.90 s | runner  |

The harness's own `duration_ms` is 1.256 s: stages 2–4. The runner accounts for 6.27 s. A
one-word answer produced 62 Events, 42 of them native frames. Every thinking delta arrives as a
`stream_event` plus a `thinking_tokens` estimate, and the adapter adds a `TextDelta` for it.

## Codex: 5.22 s

t = 0 is `CommandAdmitted.at`, 00:10:58.477.

| #   | Stage                                                                 | t (s)         | Took   | Whose   |
| --- | --------------------------------------------------------------------- | ------------- | ------ | ------- |
| 1   | Commit `CommandAdmitted` and the outbound `turn/start`, then write it | 0.000 → 0.34  | 0.34 s | runner  |
| 2   | Harness starts the turn, up to the user message item completing       | 0.34 → 0.882  | 0.54 s | harness |
| 3   | Model request to response headers                                     | 0.882 → 1.970 | 1.09 s | model   |
| 4   | Headers to first output item: 5 output tokens, no reasoning tokens    | 1.970 → 3.984 | 2.01 s | model   |
| 5   | Output to the harness's `turn/completed`                              | 3.984 → 4.152 | 0.17 s | harness |
| 6   | Runner works through the buffered output, up to `TurnCompleted.at`    | 4.152 → 5.283 | 1.13 s | runner  |

Stage 1 ends when the harness first reacts: it emits a `warning` at t = 0.343. Codex does not report
when it sent the request, so stage 3 starts at the user item's `emittedAtMs`. The harness's own
`durationMs` is 3.823 s: stages 2–5. The runner accounts for 1.47 s.

The runner's lag behind the harness grows through the turn, one commit per line:

| Frame                        | Harness emitted | Runner `at`  | Lag     |
| ---------------------------- | --------------- | ------------ | ------- |
| `item/started` (answer)      | 00:11:02.461    | 00:11:02.467 | 6 ms    |
| `item/agentMessage/delta`    | 00:11:02.461    | 00:11:02.773 | 312 ms  |
| `item/completed`             | 00:11:02.617    | 00:11:03.116 | 500 ms  |
| `thread/tokenUsage/updated`  | 00:11:02.628    | 00:11:03.290 | 663 ms  |
| `thread/status/changed` idle | 00:11:02.629    | 00:11:03.475 | 847 ms  |
| `turn/completed`             | 00:11:02.629    | 00:11:03.668 | 1040 ms |

## The app's share

The test's clock runs from the app's answer to the input until `TurnCompleted` arrives. The
runner's clock runs from `CommandAdmitted.at` to `TurnCompleted.at`. Those spans differ by 0.01 s
(Claude) and 0.07 s (Codex). So `TurnCompleted` reached the test about as fast as the admission's
answer did after its own commit. Both paths include one commit and one round trip from the test
host, about 0.11 s for a warm request. The app's ingest and event stream cost no more than the
admission's answer path, and this run cannot split that path further.

## Why a commit costs ~90 ms

- **The commits are serial and on the critical path.** `_read_stdout` (<../runner/session.py>)
  commits a line's `Native` Event, then the adapter commits each Event derived from it, before the
  next line is read. `send` commits an outbound frame before writing it to the harness. During a
  backlog, entries start at least 83 ms apart, 92 ms at the median. The Claude turn also had two
  commits of about 0.6 s. The average of 123 ms includes adapter work, so a session journals about
  8–12 Events a second.
- **Each commit is several fsyncs.** `journal_mode=DELETE` with `synchronous=EXTRA` syncs the
  rollback journal, then the database, then the directory after deleting the journal.
- **`/state` is `local-path-ovh-hdd`,** a spinning disk, per the testing SandboxTemplate's
  `volumeClaimTemplates`.

Not measured directly: fsync latency on that disk. Agents cannot exec into sandbox pods. The
~90 ms is inferred from the spacing above.

A streamed answer produces Events faster than 10 a second. So the lag grows with the length of the
turn, not just by a fixed amount at the end. The 2026-09-15 staging thread in
<thread_load_20260915.md> held 3,091 Events over eight turns. At this rate that is about five
minutes of journal time. Staging's `/state` is on the same storage class.

## Getting the runner's cost to tens of milliseconds

These are candidates, not measured fixes:

1. **Group commit in the stdout reader.** Commit every line already buffered on the pipe, and the
   Events derived from them, in one transaction, and publish after that commit as now. The
   publication-after-fence contract is unchanged. A burst then costs one commit instead of one per
   line: the Claude harness emitted all 34 lines from `message_start` through `result` within
   about 0.35 s.
2. **`/state` on `local-path-ovh-ssd`.** A constant factor on every fsync; the serial structure
   stays.
3. **WAL with `synchronous=FULL`,** one fsync per commit. This waits on a SQLite with the WAL-reset
   repair (<../docs/thread_layering.md> § Runner SQLite mode).

The outbound write-ahead in `send` is one commit per frame sent, 1–3 per turn. It stays on the
critical path by design, and costs little once each commit is cheap.

Left with the harness and the model: Codex's 0.54 s before its request, and 1.09 s to headers plus
2.01 s to a first token with no reasoning tokens. LiteLLM's access log carries no timings, so this
run cannot split LiteLLM from the ChatGPT backend.

## Opening the thread, same run

The browser's reads through the sync proxy, each timed from its first request to its last body,
from a developer host about 0.11 s from the app:

| Harness | Open | `scope` | `entity_shape` | `tail` | `body_shape` | `body` |
| ------- | ---- | ------- | -------------- | ------ | ------------ | ------ |
| Claude  | cold | 0.54 s  | 0.15 s         | 0.60 s | 0.19 s       | 0.15 s |
| Claude  | warm | 0.11 s  | 0.11 s         | 0.25 s | 0.12 s       | 0.13 s |
| Codex   | cold | 0.52 s  | 0.16 s         | 0.61 s | 0.13 s       | 0.18 s |
| Codex   | warm | 0.12 s  | 0.14 s         | 0.18 s | 0.13 s       | 0.15 s |

Every stage is under its ceiling. A warm open is one round trip per request. The cold `scope` is
the client's first request, so it includes setting up the connection.

## Reproducing

From a host with a kubeconfig that can mint the `agentplane-agent` token in `agentplane-testing`
(see <../acceptance/README.md>):

```bash
bazelisk test //agentplane/acceptance:test_thread_latency --test_output=streamed
```

In a Claude Code web session, local Bazel reaches GitHub only through BuildBuddy, and httpx needs
the session proxy's CA:

```bash
bazelisk test //agentplane/acceptance:test_thread_latency --test_output=streamed \
  --experimental_remote_downloader=grpcs://remote.buildbuddy.io \
  --test_env=SSL_CERT_FILE=/root/.ccr/ca-bundle.crt --test_env=HTTPS_PROXY
```

The per-harness timings land in the test's undeclared outputs as `thread_latency-<harness>.json`,
with the thread ids. A thread's full Event log, `at` and native frames included, is its event
stream from cursor 0:

```bash
TOKEN=$(kubectl -n agentplane-testing create token agentplane-agent --audience=agentplane --duration=600s)
curl -sS -N --max-time 8 -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" \
  "https://agentplane-testing.allegedly.works/threads/$THREAD/events/stream?after=0"
```

The model leg's headers time is llm-ingress's `HTTP Request: POST http://litellm…` log line:
`kubectl -n agentplane-testing logs deploy/agentplane-llm-ingress`.
