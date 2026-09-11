# Gateway heap exhaustion: leaked `activeSessionStore` copies

Active. Root cause identified in OpenClaw 2026.8.1; no fix, not yet reported upstream.

`public-coder-agent` aborts every ~2h50m with container exit **134**, not 137 —
the kernel never kills it, V8 does:

```text
[7:0x...] 10200039 ms: Scavenge 2032.0 (2085.5) -> 2031.9 (2086.8) MB,
          (average mu = 0.139, current mu = 0.072) allocation failure;
FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory
```

`mu = 0.139` means ~86% of wall-clock was spent in GC reclaiming almost nothing,
which is also why `/healthz` readiness probes time out shortly before each abort.

## What leaks: copies of the store, not its contents

Two heap snapshots of one pod, at 8 and 63 minutes:

|                                                    | 8 min        | 63 min        | delta          |
| -------------------------------------------------- | ------------ | ------------- | -------------- |
| Heap, live objects                                 | 588.4 MiB    | 790.5 MiB     | +202.1 MiB     |
| Retained objects                                   | 5,284,586    | 7,466,845     | +2,182,259     |
| Distinct session keys, all stores                  | 477          | 479           | +2             |
| `coder` store copies, ~32.2 MiB each (386 keys)    | 1            | 4             | +3             |
| `haku_console_tpm` copies, ~5.3 MiB each (91 keys) | 2            | 3             | +1             |
| **Retained by all copies**                         | **42.7 MiB** | **145.0 MiB** | **+102.3 MiB** |

Session count is flat. What multiplies is the number of retained **copies**, and
key-set comparison shows they are duplicates rather than distinct data:

```text
12931827 vs 13332873:  |A|=386  |B|=386  shared=386
13332873 vs 13614619:  |A|=386  |B|=386  shared=386
 5303393 vs 12647641:  |A|=91   |B|=91   shared=90
```

Four new copies -- three `coder`, one `haku_console_tpm` -- account for
**102.3 MiB of the 202.1 MiB** of heap growth, roughly 51%, at about one copy
every 14 minutes.

Per copy, with the async resource pinning it at 63 min:

| Store                       | 8 min    | 63 min   | pinned by      |
| --------------------------- | -------- | -------- | -------------- |
| `3573141` coder             | 32.1 MiB | 32.2 MiB | `FSEvent`      |
| `12931827` coder            | --       | 32.2 MiB | `Promise`      |
| `13332873` coder            | --       | 32.3 MiB | `Timeout`      |
| `13614619` coder            | --       | 32.2 MiB | `FSReqPromise` |
| `6922245` haku_console_tpm  | 5.4 MiB  | 5.5 MiB  | `Timeout`      |
| `5303393` haku_console_tpm  | 5.2 MiB  | 5.3 MiB  | `FSEvent`      |
| `12647641` haku_console_tpm | --       | 5.3 MiB  | `FSEvent`      |

The other ~100 MiB is in `system / Context` blobs (109.8 -> 185.1 MiB) and two
new `closure finish` blobs (60.6 MiB). Those are plausibly this leak's
scaffolding -- the ALS store objects and `beforeDispatch` closures wrapping each
copy -- but that is **not established**: they are distinct blobs in the dominator
tree, not containers of the copies, and nothing yet ties their growth to it.

Every copy is retained through the same shape, differing only in which async
resource happens to pin it — observed holders include `Timeout`, `FSEvent`,
`FSReqPromise`, `Promise`, and the HTTP server's `connectionsCheckingInterval`:

```text
<async resource> --<symbol kResourceStore>--> Object
   --beforeDispatch--> closure beforeDispatch
   --context--> system / Context
   --activeSessionStore--> Object (32.2 MiB)
```

`borrowSnapshot` copies the whole store; the copy lands in an AsyncLocalStorage
store as `beforeDispatch`'s captured scope; every async resource created under
that context holds it through `kResourceStore`; nothing releases it. So each
`borrowSnapshot` call strands a full copy of the session store for the life of
the process.

AsyncLocalStorage is the retention path, not the cause. Total `kResourceStore`
pins **fell** over the interval (96,666 -> 85,164, of which `Promise` 90,178 ->
82,030), so this is not async-resource proliferation — the stores themselves
survive their runs.

## Why one copy already costs 32 MiB

The store holds **386 sessions**, restored from the SQLite state database at
startup, including every `agent:coder:cron:<uuid>:run:<uuid>` ever executed. Each
entry carries `compactionCheckpoints`. That sets the floor: even a
non-leaking gateway pays 32 MiB for this store, and every leaked copy pays it
again. Pruning session history shrinks both.

## Not established

- Which call site invokes `borrowSnapshot`, and why the copies outlive their run.
  The snapshots name the retention path, not the code path.
- Whether upstream has a fix. No issue in openclaw/openclaw matches 2026.8.1;
  the nearest in shape, openclaw/openclaw#13758, is 2026.2.3-1 with no root cause.
- Whether the ~2.17 GiB of non-heap RSS is steady state or residue from taking
  snapshots. It was ~400 MiB on a pod that had never been snapshotted.

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

- **Snapshots are not free.** Serialising one added ~200 MiB of native RSS that
  Node did not return; two captures walked a 4Gi container from 3348 MiB to
  4018 MiB in nine minutes and pushed it into an OOMKill. On a container this
  close to its ceiling, budget for that or raise the limit first.
- **Do not parse a snapshot inside the pod.** It needs multiple GiB and would
  OOMKill the gateway under study. Copy it out and analyse elsewhere.
- `kubectl cp` runs over `kubectl exec`; where exec is unavailable,
  `kubectl exec ... -- cat /diag/<file> > local` works and does not require `tar`
  in the image.
- Reading the file into a JS string caps out at V8's ~536M characters. Parse
  from a `Buffer`.
- Constructor/self-size aggregation cannot answer this question: every top
  constructor is an anonymous `Object`, `Array` or closure `Context`. It takes a
  dominator tree and retained sizes to name a holder, and a key-set comparison
  between candidate objects to tell duplicate copies from distinct data.
