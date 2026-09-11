# Gateway heap exhaustion: per-run session stores retained via AsyncLocalStorage

Active. Mechanism and call site identified in OpenClaw 2026.8.1; no fix, not yet
reported upstream.

`public-coder-agent` aborts every ~2h50m with container exit **134**, not 137 —
the kernel never kills it, V8 does:

```text
[7:0x...] 10200039 ms: Scavenge 2032.0 (2085.5) -> 2031.9 (2086.8) MB,
          (average mu = 0.139, current mu = 0.072) allocation failure;
FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory
```

`mu = 0.139` means ~86% of wall-clock was spent in GC reclaiming almost nothing.

## What leaks: one session store per run, never released

Two heap snapshots of one pod, at 8 and 63 minutes:

|                                                    | 8 min        | 63 min        | delta          |
| -------------------------------------------------- | ------------ | ------------- | -------------- |
| Heap, live objects                                 | 588.4 MiB    | 790.5 MiB     | +202.1 MiB     |
| Retained objects                                   | 5,284,586    | 7,466,845     | +2,182,259     |
| Distinct session keys, all stores                  | 477          | 479           | +2             |
| `coder` stores, ~32.2 MiB each (386 keys)          | 1            | 4             | +3             |
| `haku_console_tpm` stores, ~5.3 MiB each (91 keys) | 2            | 3             | +1             |
| **Retained by all stores**                         | **42.7 MiB** | **145.0 MiB** | **+102.3 MiB** |

Session count is flat. What multiplies is the number of live store **objects**,
holding identical data:

```text
12931827 vs 13332873:  |A|=386  |B|=386  shared=386
13332873 vs 13614619:  |A|=386  |B|=386  shared=386
 5303393 vs 12647641:  |A|=91   |B|=91   shared=90
```

Four new stores account for **102.3 MiB of the 202.1 MiB** of heap growth,
roughly 51%, at about one every 14 minutes. Each is pinned by a different async
resource:

| Store                       | 8 min    | 63 min   | pinned by      |
| --------------------------- | -------- | -------- | -------------- |
| `3573141` coder             | 32.1 MiB | 32.2 MiB | `FSEvent`      |
| `12931827` coder            | --       | 32.2 MiB | `Promise`      |
| `13332873` coder            | --       | 32.3 MiB | `Timeout`      |
| `13614619` coder            | --       | 32.2 MiB | `FSReqPromise` |
| `6922245` haku_console_tpm  | 5.4 MiB  | 5.5 MiB  | `Timeout`      |
| `5303393` haku_console_tpm  | 5.2 MiB  | 5.3 MiB  | `FSEvent`      |
| `12647641` haku_console_tpm | --       | 5.3 MiB  | `FSEvent`      |

The remaining ~100 MiB is in `system / Context` blobs (109.8 -> 185.1 MiB) and
two new `closure finish` blobs (60.6 MiB). Plausibly this leak's scaffolding, but
**not established**: they are distinct blobs in the dominator tree, not
containers of the stores.

## The retention path, and the call site

Every store is held through the same shape, differing only in which async
resource pins it:

```text
<async resource> --<symbol kResourceStore>--> Object
   --beforeDispatch--> closure beforeDispatch
   --context--> system / Context
   --activeSessionStore--> Object (32.2 MiB)
```

`kResourceStore` is Node's `AsyncLocalStorage`. The scope is a process-wide
singleton (`src/agents/prepared-model-runtime-generation-scope.ts`):

```js
const preparedModelRuntimePluginGenerationScope = resolveGlobalSingleton(
  Symbol.for("openclaw.preparedModelRuntimePluginGenerationScope"),
  () => new AsyncLocalStorage()
);

function withPreparedModelRuntimePluginGenerationScope(generation, run, borrowSnapshot) {
  const inherited = preparedModelRuntimePluginGenerationScope.getStore();
  const borrow = borrowSnapshot ?? (inherited?.generation === generation ? inherited.borrowSnapshot : void 0);
  return preparedModelRuntimePluginGenerationScope.run(
    { generation, ...(borrow ? { borrowSnapshot: borrow } : {}) },
    run
  );
}
```

`borrowSnapshot` is a **closure** parked in the ALS store. Node copies that store
into every async resource created inside the scope, so any resource outliving the
run — a timer, an `fs` watcher, a pending promise — keeps the whole run scope
reachable. The scope reaches the run's session map
(`src/agents/agent-runner-memory.ts`):

```js
const activeSessionStore = params.sessionStore ?? {};
```

That is a reference, not a copy: each agent run materialises its own session map,
and the ALS-captured closure then prevents any of them being collected when the
run ends. Hence one ~32 MiB store per run, all holding the same 386 sessions.

AsyncLocalStorage is the retention path, not the cause. Total `kResourceStore`
pins **fell** over the interval (96,666 -> 85,164, `Promise` 90,178 -> 82,030),
so this is not async-resource proliferation.

The same file already carries the escape hatch, unused on run completion:

```js
/** Detached queue drains re-admit on the current generation, never a predecessor's scope. */
function runOutsidePreparedModelRuntimePluginGenerationScope(run) {
  return preparedModelRuntimePluginGenerationScope.exit(run);
}
```

## Where the container's memory actually goes

The heap is a minority of the footprint, and `kubectl top` does not show that.
A healthy pod, fresh process:

|                                                | at 7 min                             | at 15 min      |
| ---------------------------------------------- | ------------------------------------ | -------------- |
| `slab_reclaimable` — kernel dentry/inode cache | --                                   | **2277.1 MiB** |
| `anon` — the process                           | 1448 MiB                             | 994.2 MiB      |
| `file` — page cache                            | 1570 MiB                             | 780.7 MiB      |
| node RSS                                       | 1488 MiB                             | 998 MiB        |
| V8 heap                                        | 1000 MiB committed, **415 MiB live** | --             |
| glibc `brk` arena                              | 78.8 MiB                             | 77.4 MiB       |

**Over half the 4Gi charge was kernel slab, not openclaw**, on that pod: the
agent's filesystem activity — dozens of `fs_event` watchers (88 on a later pod),
workspace and git churn, session-file indexing — grows the dentry/inode cache
until it fills the cgroup. The gateway process itself is ~1 GiB, of which
~415 MiB is genuine live heap.

The split is not stable across pods, so read `memory.stat` per incident rather
than carrying these numbers forward: a later pod under the same limit measured
`slab_reclaimable` at 19.7 MiB with `anon` 1543 MiB and `file` 1315 MiB.

Only the V8 heap grows without bound, and this leak is what fills it, from
~415 MiB to the 2096 MiB cap. Slab and page cache are reclaimable, which is why
the failure is always a clean V8 abort and never a 137: the 4Gi limit is not the
binding constraint, the heap cap is.

## Second, separate problem: the container lives in I/O stall

```text
memory.current = 4095 MiB   max = 4096 MiB
memory.events:  max 12182   oom_kill 0
```

The cgroup has hit its ceiling **12,182 times**, reclaiming cache each time and
never being killed. Reclaim is not what costs the `/healthz` probe, though —
pressure stall accounting puts it squarely on I/O (`io.pressure full avg10=24.58`
against `memory.pressure some avg10=0.05`). What reads the databases, and what
fills them, is measured in <2026_09_state_db_io.md>.

## Why one store costs 32 MiB

It holds **386 sessions**, restored from the SQLite state database at startup,
including every `agent:coder:cron:<uuid>:run:<uuid>` ever executed, each with
`compactionCheckpoints`. `openclaw.json5` exposes no retention or pruning knob.
That sets the floor and multiplies every leaked store.

`session_nodes` is where they come from: 386 rows whose `entry_json` averages
64,209 bytes, 24.5 MiB on disk, inflating to ~32 MiB of live JS objects.

## Not established

- Why the copies outlive their run — whether the scope is never exited, or
  exited but already captured. The fix hinges on this.
- Whether the remaining ~49% of heap growth is the same leak's scaffolding.
- Whether upstream has a fix. No issue in openclaw/openclaw matches 2026.8.1;
  the nearest in shape, openclaw/openclaw#13758, is 2026.2.3-1 with no root cause.

## Measuring this again

The image carries the flags (<../default.nix>): `--heapsnapshot-signal=SIGPWR`
for an on-demand snapshot, `--heapsnapshot-near-heap-limit=1` for an unattended
one at the abort, `--report-on-fatalerror`, all writing to the `/diag` claim so a
capture outlives the Pod.

```bash
# snapshot on demand; tini forwards the signal
kubectl -n public-coder-agent exec deploy/public-coder-agent -c openclaw -- kill -30 1
```

**Confirm `--heapsnapshot-signal` is in the process's `NODE_OPTIONS` before
signalling.** Without that flag SIGPWR's default disposition terminates the
gateway.

Gotchas that cost time here:

- **Read `memory.stat`, not `kubectl top`.** `kubectl top` reports the whole
  cgroup charge, which here is dominated by reclaimable kernel slab. It read
  ~3 GiB above the process's anonymous memory and made a healthy container look
  nearly full.
- **Snapshots are not free.** Serialising one added ~390 MiB of native RSS that
  Node did not return; two left a 1523 MiB `brk` arena where a never-snapshotted
  pod under the same load has 78 MiB. Budget for it near a ceiling.
- **Do not parse a snapshot inside the pod.** It needs multiple GiB and would
  OOMKill the gateway under study. Copy it out and analyse elsewhere.
- `kubectl cp` runs over `kubectl exec`; where exec is unavailable,
  `kubectl exec ... -- cat /diag/<file> > local` works and needs no `tar` in the
  image.
- Reading a snapshot into a JS string caps out at V8's ~536M characters. Parse
  from a `Buffer`.
- Constructor/self-size aggregation cannot answer this: every top constructor is
  an anonymous `Object`, `Array` or closure `Context`. It takes a dominator tree
  for retained sizes, and a key-set comparison to tell duplicate stores from
  distinct data.
- The gateway bundle ships unminified at
  `/nix/store/*openclaw-gateway-*/lib/openclaw/dist/`, as thousands of small
  chunks with readable names. `grep -l` over `dist/*.js` finds a symbol in
  seconds; a recursive grep on an HDD-backed node times out.
