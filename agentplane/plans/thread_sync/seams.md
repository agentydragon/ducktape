# Where a sync implementation plugs in

The goal: several thread-sync implementations live on `devel` at once, and a deployment picks one
without a rebuild. Image automation deploys `devel`, so trying implementation _N_ must not mean deleting the
other _N−1_.

A sync implementation is what gets a thread's rows and bodies from the materialized fold (**C1**)
into the browser and keeps them current. Below it — ingestion, the fold, its tables — and above it —
rendering, scroll anchoring, sending commands — is shared.

## Today

Electric sits behind one module on each side:

- **Backend:** `electric.py` holds `ElectricProxy` and the `/threads/{thread_id}/sync/*` router,
  mounted only when `electric_url` is set (`main.py`, `api.py`). Its only read of the fold is
  `ContentStore.current_scope`, which any implementation needs for **P7**.
- **Frontend:** `thread_sync.ts` defines the `ThreadSync` interface, and `projected_session.tsx`
  reads only that, from a context `app.tsx` fills. `thread_store.tsx` implements it as
  `electricThreadSync`, on the raw `@electric-sql/client`.

## Backend

- **Shared, below the seam:** the fold tables; `ContentStore`;
  `ThreadStore`; `ThreadUpdates.changes`, the replica-local signal that a thread was written — what
  a long poll or a push waits on. The `/threads` routes for thread metadata and commands
  (`POST /threads/{thread_id}/commands`, `/commands/reconcile`) stay shared: commands are writes,
  not sync.
- **One new shared read**, which every non-Electric option needs: rows by `entity_index` range, and
  the delta — rows in a range with `revision_cursor > since`. It is a read of the fold rather than a
  protocol, so it belongs in `agent_runtime/view/`.
- **Per implementation:** a package `agentplane/app/thread_sync/<name>/` owning a router mounted at
  `/threads/{thread_id}/sync/<name>/…` behind the same `require_caller` dependency (**C2**), and its
  own settings. Electric moves there.
  There is no Python interface to implement: each implementation is a router over the shared stores.

Which implementations are mounted is a deployment setting (§ Choosing one).

## Frontend

What the renderer needs from any implementation, per thread:

- the rows of a window in thread order, keeping their identity across updates (**P5**, **S2**);
- whether older rows exist, and a way to ask for them (**P4**);
- whether the window is caught up (**S3**);
- a body by reference, possibly still growing (**S1**, **P8**);
- the fold's rows for commands the client sent (**P9**).

`ThreadSync` in `frontend/thread_sync.ts` is that list: a `Thread` provider that syncs one thread
while mounted, and hooks for its window, command rows by ID, and a body by reference. Paging is
"load older", owned by the implementation. Electric's swap to a new epoch without a reload stays
inside it; **P7** requires only the refusal.

## Shared between implementations

Between the shared layers and a single implementation there may be pieces several implementations
use. Candidates, from the options as written:

- **Bodies by immutable reference.** The window poll, the moving window and SSE push all fetch
  content the same way: a batch of references plus how many chunks the client holds, answered with
  the missing chunks. One endpoint and one client-side body cache would serve all three.
- **The range and delta read** above, which is already in the shared layer.
- **A client row store** keyed by `(entity_kind, entity_id)` and ordered by `entity_index`, which
  any non-Electric implementation needs to merge windows and revisions.

They live in `thread_sync/` beside the implementations, not below the seam. None is extracted ahead
of time: a piece moves there when a second implementation needs it, which is also when its real
interface is known.

## Choosing one

The switch can sit at three points. Each keeps every implementation's code on `devel`; they differ
in what has to change to try another.

| Point                                       | To switch                          | Cost                                                                                        |
| ------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------- |
| Build: a Bazel flag or one image per choice | rebuild, or publish an image each  | image automation tracks one image, so a second choice means a second published image target |
| Deploy: an app setting                      | change the setting in the manifest | none beyond the setting; testing and staging can run different ones                         |
| Browser: `?sync=`, else the deploy default  | reload with a parameter            | the deployment mounts every configured implementation                                       |

**The deploy-time setting is the one to build.** It is an ordinary `Settings` field, set by the
cdk8s app like `electric_url` is today. The backend mounts the chosen implementation's routes, and
the frontend reads the choice from the app at start, so one image serves any choice. A per-browser
override can be added on top later, for comparing two implementations on one deployment. Picking
at build time buys only the absence of unused code, and loading each frontend implementation with
a dynamic `import()` already keeps it off the page.

## Complications

1. **Tests multiply.** `test_thread_browser` and `test_thread_window_browser` exercise the sync
   path end to end and already sit near their time limits; parametrizing them whole by
   implementation multiplies the slowest suite by _N_. Instead, each implementation passes the same
   contract suite on each side — its backend routes, and the frontend `ThreadSync` interface — and
   only the browser flows from <../../docs/thread_sync_requirements.md> run per implementation: open on the tail, stream,
   scroll up then stream, edit mid-window, reconnect. The rest of the browser suite runs on the
   default.
2. **Electric costs something when nobody picks it.** Its replication slot retains WAL and its shape
   storage holds state regardless of traffic. Unsetting `electric_url` turns it off.
3. **The interface must not be Electric-shaped.** Extracted from today's `thread_store.tsx` props,
   it would hand every other implementation shape and subset concepts it has no use for. It is
   written from the list above, and Electric is adapted to it.
4. **The schema is shared.** An implementation that needs a column or index adds it to the fold
   for everyone. Additive changes are fine, and under **C3** there is no migration to stage, but no
   implementation may change what another reads.
5. **Command rows.** Electric's `useCommandRows` loads them into the thread's window, and
   `local_commands.ts` reconciles against it. Keeping `useCommandRows` on the interface lets each
   implementation supply live pending state. A shared commands endpoint outside sync would be
   simpler but gives that up. To be decided when the second implementation lands.
6. **Implementations rot.** Each non-default implementation states the experiment it exists for
   and the measurement that ends it. The losers are deleted; the seam stays.
7. **Which one served a request (O3)** is in its path, and the thread's debug page should show the one in use.

## Order of work

Each step ships alone, and the first changes no behaviour.

1. Move `electric.py` into `thread_sync/electric/`, and its routes to `/threads/{thread_id}/sync/electric/…`, with the frontend following in the same
   change.
2. The deployment setting that picks an implementation, which the frontend reads at start.
3. A second implementation, with the shared range and delta read. The cheapest is the window poll,
   long-polled rather than on a timer (**E6**).
