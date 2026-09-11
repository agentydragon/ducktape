# Gateway I/O starvation: what reads the state databases, and what fills them

Active. Measured on `public-coder-agent` pod `-stx9g` (OpenClaw 2026.8.1, 4Gi
limit, state PVC on `local-path-ovh-hdd`). Separate problem from the heap leak
in <2026_09_session_store_copy_leak.md>, which shares the same databases.

The container spends ~24% of wall-clock fully stalled on I/O:

```text
io.pressure      some avg10=24.60  full avg10=24.58
memory.pressure  some avg10=0.05
cpu.pressure     some avg10=0.20
```

That stall, not GC, is what makes the 5 s `/healthz` probe miss.

## Two consumers

### 1. A daily integrity verifier that never gets to be daily

`server-start` imports `startOpenClawDatabaseIntegrityVerifier`, which forks
`dist/state/openclaw-database-verify.worker.js`. The worker **copies** each agent
database to `~/.cache/openclaw/openclaw-sqlite-readonly-<pid>-XXXXXX/database.sqlite`
— on the container overlay, not the PVC — and scans the copy. Measured 17 minutes
in, still running:

|                                  |          |
| -------------------------------- | -------- |
| `read_bytes` (state PVC, `8:16`) | 2.25 GiB |
| `write_bytes` (node root, `8:0`) | 1.59 GiB |
| copy resident in `~/.cache`      | 1.49 GiB |

Its cadence is two bundle constants, **not** environment variables despite the
names:

```js
const OPENCLAW_DATABASE_VERIFY_INITIAL_DELAY_MS = 5 * 6e4; // 5 min after start
const OPENCLAW_DATABASE_VERIFY_INTERVAL_MS = 1440 * 6e4; // then every 24 h
```

The pod aborts every ~2h50m and Flux rolls the image every 1.5-8h, so it never
survives to the 24 h interval: it runs the five-minute pass on **every** restart,
and the pass outlives the gap between restarts. ~4 GiB of disk traffic per pod
start.

`OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK=none` does not gate this. That string
occurs in exactly one bundle module, `openclaw-agent-db-maintenance-*.js`; the
verifier is a different module reached from `server-start`.

It has not been observed to finish. On a pod 35 minutes into its life the worker
was still running after 27 minutes of scanning — `write_bytes` flat at 1.59 GiB,
so the copy phase was long done — having read 2.72 GiB, with **no** verification
result of any kind in the log. Whether it completes before the ~2h50m abort is
unmeasured.

### 2. The gateway main thread, synchronously

```text
20,255 pread()/s, mean 4,093 B   (= SQLite page size)
rchar 27.3 GB / 24 min           read_bytes 9.0 GB
tid 7 (main): 258 s CPU, state R
libuv threadpool threads:        ~1 s CPU each
```

83 MB/s of logical page traffic against a 2.1 GB working set. Most is served from
page cache, but each page still costs a syscall and a B-tree step **on the event
loop thread** — so the gateway is blocked, not merely slow. Page cache confirms
scanning rather than a hot set:

```text
workingset_refault_file 2,396,558   (~9.8 GB re-read after eviction)
inactive_file 1312 MiB   active_file 3.2 MiB
```

OpenClaw names the callers itself, 64 times as `slow SQLite transaction hold`
(`sqlite/transaction`, threshold 1000 ms):

```json
{"async":false,"database":".../coder/.../openclaw-agent.sqlite","elapsedMs":1188,"operation":"agent.write"}
{"async":false,"database":".../haku_console_tpm/.../openclaw-agent.sqlite","elapsedMs":5128,"operation":"agent.write"}
{"async":false,"database":".../haku_console_tpm/.../openclaw-agent.sqlite","elapsedMs":2771,"operation":"session transcript fenced read"}
```

plus unattributed holds at 11,266 ms and 31,869 ms, and RPC timings
`system-prompt 34242ms`, `sessions.subscribe 18002ms`, `sessions.diff 7055ms`,
`sessions.list 3588ms`, `system.info 4310ms`.

## What fills the databases

`dbstat`, exact. **`freelist_count` is 0 on both agent databases** — none of this
is unreclaimed deleted space, so `VACUUM` returns nothing.

`agents/coder/agent/openclaw-agent.sqlite`, 1533.2 MiB:

| object                                    |    MiB | share |
| ----------------------------------------- | -----: | ----: |
| `transcript_events`                       |  652.1 | 42.5% |
| `memory_embedding_cache`                  |  343.8 | 22.4% |
| `memory_index_chunks`                     |  297.4 | 19.4% |
| `memory_index_chunks_vec_vector_chunks00` |   90.1 |  5.9% |
| `session_nodes`                           |   24.5 |  1.6% |
| `memory_index_chunks_fts_content`         |   17.5 |  1.1% |
| ~60 further objects                       | <16 ea |   ~7% |

Measured column averages over the most recent 200 rows:

```text
transcript_events        120,201 rows   event_json  avg  5,745 B
memory_embedding_cache    10,551 rows   embedding   avg 32,293 B
memory_index_chunks        9,069 rows   text 1,464 B, embedding avg 32,288 B
session_nodes                386 rows   entry_json  avg 64,209 B
```

**Every embedding is stored three times, twice as JSON text.** 32 KB per vector
against 4.2 KB for the same vector in sqlite-vec's packed
`memory_index_chunks_vec_vector_chunks00` — an array of decimal floats at roughly
8x the binary form, held both in `memory_index_chunks.embedding` and again in
`memory_embedding_cache.embedding`. Together ~624 MiB, **41% of the file**,
duplicating a 90 MiB index.

`transcript_events` is the other 42%: every message, tool call and tool result the
agent has emitted, ~5.7 KB each, with nothing pruning it.

`session_nodes` ties back to the heap leak: 386 rows of 64 KB `entry_json` is
precisely the object the AsyncLocalStorage retention pins, 24 MiB of JSON
inflating to the ~32 MiB live session store that is then duplicated per run.

`state/openclaw.sqlite`, 96.3 MiB: `audit_events` 44.3 MiB plus **nine indexes on
that one table** totalling 38.5 MiB — 86% of the database.
`agents/haku_console_tpm/...`, 458.3 MiB: same shape (`transcript_events` 162.2,
`memory_embedding_cache` 88.9, `memory_index_chunks` 83.6,
`trajectory_runtime_events` 40.2, vec store 40.1).

So the size is not the problem a bigger limit would solve. 41% is a text
re-encoding of vectors that already exist packed alongside it, 42% is an unbounded
transcript log, and the code walks all of it synchronously on the event loop, on a
rotational disk.

## Turning the verifier off

Upstream has no switch. The call site in `server-start` carries one guard:

```js
if (!minimalTestGateway) {
  const { startOpenClawDatabaseIntegrityVerifier } = await import("./openclaw-database-verify-<hash>.js");
  kernel.addGatewayLifetimeSidecar(startOpenClawDatabaseIntegrityVerifier({ env: process.env }));
}
```

and that flag is
`isVitestRuntimeEnv() && process.env.OPENCLAW_TEST_MINIMAL_GATEWAY === "1"` —
unreachable outside a vitest run, and it strips most of the gateway besides.
Below it, driver and worker read no `process.env` at all; `options` is `{ env }`,
used only to resolve target paths and persist quarantine records. The one
config-shaped lever is target collection — `resolveOpenClawStateSqlitePath` plus
`listOpenClawRegisteredAgentDatabases`, filtered by `existsSync` — so exempting
an agent database means deregistering the agent.

<../patch-openclaw-npm-dist.mjs> therefore mints `OPENCLAW_DATABASE_VERIFY`
(`on`, the upstream behaviour, or `off`), guarding the initial `schedule()` call
and logging the disable. That script content-matches `dist/*.js` rather than
filenames, whose chunk hashes churn every release, and fails the build when a
pattern does not match exactly once — the same mechanism behind
`OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK`. `public-coder-agent` sets it `off`;
the Haku spike, sharing the same image build, keeps the default.

It takes effect only once the image is rebuilt: the variable does not exist in a
bundle that has not been through the patch script.

What gating it costs: this verifier is the only thing that quarantines a
corrupted state or agent database.

## Measuring this again

No `sqlite3` binary in the image; the pod's own Node has `node:sqlite`, and its
build does carry `dbstat`:

```bash
kubectl -n public-coder-agent exec deploy/public-coder-agent -c openclaw -- sh -c \
  '$(readlink -f /proc/7/exe) --experimental-sqlite /tmp/dbsize.js <db>'
```

Gotchas that cost time here:

- **A full `dbstat` scan of the 1.5 GB database outlives the exec gateway's
  timeout** (504 at ~90 s). Detach it (`nohup ... > /tmp/out &`) and poll the file.
- **`pread(2)` does not move the file offset**, so sampling `/proc/<pid>/fdinfo`
  positions cannot localize SQLite reads — every fd sits at `pos=0` no matter how
  hard it is being read. Use `/proc/<pid>/io` (`rchar` vs `read_bytes`, `syscr`)
  and the per-thread CPU split instead.
- **`--report-on-signal` yields an empty `javascriptStack`** when the main thread
  is inside native code, which is exactly when you want it. The report's `libuv`
  handle census is still useful (88 `fs_event` watchers here).
- `MAX(rowid)` is a cheap row-count proxy, but FTS5 shadow `_data` tables use
  structured rowids, so theirs read as absurd values (e.g. 3.16e12).
