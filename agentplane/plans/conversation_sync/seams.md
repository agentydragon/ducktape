# Where a sync implementation plugs in

The goal: several thread-sync implementations live on `devel` at once, and a browser picks one at
runtime. Image automation deploys `devel`, so trying implementation _N_ must not mean deleting the
other _N−1_.

A sync implementation is what gets a thread's rows and bodies from the materialized fold (**C1**)
into the browser and keeps them current. Below it — ingestion, the fold, its tables — and above it —
rendering, scroll anchoring, sending commands — is shared.

## Today

Electric already sits behind one module on each side, but both leak into the shared layers.

| Side     | Electric-specific                                                                                                                             | Leak                                                                                                                                                                                                                                                     |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backend  | `electric.py`: `ElectricProxy` and the `/threads/{thread_id}/sync/*` router, mounted only when `electric_url` is set (`main.py`, `api.py`)    | `ContentStore.entity_interest` and `payload_selection` in `agent_runtime/view/content.py` exist only to pin shape predicates — including `ThreadInterestExpiredError`, the "must rotate" rule that exists because a shape's bounds are fixed at creation |
| Frontend | `thread_store.tsx`: `ThreadCollection`, `CommandSelection` and `PayloadBody`, on `@electric-sql/client` and TanStack DB's Electric collection | `ThreadCollection` pages by `beforeCursor` and hands the renderer Electric's `EntityInterest`; the epoch double buffer and the `RefreshThread` context live inside it                                                                                    |

`projected_session.tsx` imports only those three components plus `ThreadEntity`, `PayloadRef` and
`decimalBigInt`, so the frontend seam mostly exists already.

## Backend

- **Shared, below the seam:** the fold tables; `ContentStore` minus the two Electric reads;
  `ThreadStore`; `ThreadUpdates.changes`, the replica-local signal that a thread was written — what
  a long poll or a push waits on. The `/threads` routes for thread metadata and commands
  (`POST /threads/{thread_id}/commands`, `/commands/reconcile`) stay shared: commands are writes,
  not sync.
- **One new shared read**, which every non-Electric option needs: rows by `entity_index` range, and
  the delta — rows in a range with `revision_cursor > since`. It is a read of the fold rather than a
  protocol, so it belongs in `agent_runtime/view/`.
- **Per implementation:** a package `agentplane/app/thread_sync/<name>/` owning a router mounted at
  `/threads/{thread_id}/sync/<name>/…` behind the same `require_caller` dependency (**C2**), and its
  own settings. Electric moves there, taking `entity_interest` and `payload_selection` with it.
  There is no Python interface to implement: each implementation is a router over the shared stores.

Every configured implementation is mounted at once, and no backend flag chooses between them — the
browser chooses by which routes it calls.

## Frontend

What the renderer needs from any implementation, per thread:

- the rows of a window in thread order, keeping their identity across updates (**P5**, **S2**);
- whether older rows exist, and a way to ask for them (**P4**);
- whether the window is caught up (**S3**);
- a body by reference, possibly still growing (**S1**, **P8**);
- the fold's rows for commands the client sent (**P9**).

A sketch of the interface — its shape, not a final signature:

```ts
interface ThreadSync {
  useThreadWindow(threadId: string): {
    rows: ThreadEntity[];
    caughtUp: boolean;
    olderAvailable: boolean;
    loadOlder(): void;
    error: string | null;
  };
  usePayload(threadId: string, reference: PayloadRef, follow: boolean): string | null;
  useCommandRows(threadId: string, commandIds: readonly string[]): ThreadEntity[];
}
```

Paging becomes "load older", owned by the implementation; the renderer stops passing `beforeCursor`.
Epoch rotation and its double buffer move inside the Electric implementation, since only its fixed
shape bounds need them (**P7** requires only the refusal).

**Choosing one.** A `ThreadSync` context is set once at app start, from the `sync` query parameter,
else a per-browser setting, else the deployment's default. The backend lists which implementations
it has configured, so a browser cannot pick one the deployment lacks. Each implementation is loaded
with a dynamic `import()`, so one that isn't picked costs no bytes on page load.

## Complications

1. **Tests multiply.** `test_thread_browser` and `test_thread_window_browser` exercise the sync
   path end to end and already sit near their time limits; parametrizing them whole by
   implementation multiplies the slowest suite by _N_. Instead, each implementation passes the same
   contract suite on each side — its backend routes, and the frontend `ThreadSync` interface — and
   only the browser flows from <requirements.md> run per implementation: open on the tail, stream,
   scroll up then stream, edit mid-window, reconnect. The rest of the browser suite runs on the
   default.
2. **Electric costs something when nobody picks it.** Its replication slot retains WAL and its shape
   storage holds state regardless of traffic. Unsetting `electric_url` turns it off.
3. **The interface must not be Electric-shaped.** Extracted from today's `thread_store.tsx` props,
   it would hand every other implementation interest and rotation concepts it has no use for. It is
   written from the list above, and Electric is adapted to it.
4. **The schema is shared.** An implementation that needs a column or index adds it to the fold
   for everyone. Additive changes are fine, and under **C3** there is no migration to stage, but no
   implementation may change what another reads.
5. **Command rows.** `CommandSelection` is Electric-backed, and `local_commands.ts` reconciles
   against it. Keeping `useCommandRows` on the interface lets each implementation supply live
   pending state. A shared commands endpoint outside sync would be simpler but gives that up. To be
   decided when the second implementation lands.
6. **Implementations rot.** Each non-default implementation states the experiment it exists for
   and the measurement that ends it. The losers are deleted; the seam stays.
7. **Which one served a request (O3)** is in its path, and the thread's debug page should show the one in use.

## Order of work

Each step ships alone, and the first two change no behaviour.

1. Define `ThreadSync` and put `thread_store.tsx` behind it as the Electric implementation;
   `projected_session.tsx` consumes only the interface.
2. Move `electric.py`, `entity_interest` and `payload_selection` into `thread_sync/electric/`, and
   its routes to `/threads/{thread_id}/sync/electric/…`, with the frontend following in the same
   change.
3. Runtime selection, and the endpoint listing configured implementations.
4. A second implementation, with the shared range and delta read. The window poll is the cheapest.
