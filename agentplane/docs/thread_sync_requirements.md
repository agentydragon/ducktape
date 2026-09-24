# Thread sync requirements

What any implementation that gets a thread's rows and bodies into the browser has to do, with
stable IDs to cite. Each says where it comes from, because a requirement nobody can source is a
preference and should be argued as one. The deployed design, and where it falls short of these, is
[Thread view synchronization](thread_view_sync.md).

## Constraints — not traded away

| ID  | Constraint                                                                                                                          | Source                                             |
| --- | ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| C1  | The thread fold is **materialized in Postgres**. Implementations differ in how a browser learns about it, not in whether it exists. | Owner, 2026-09-22                                  |
| C2  | Every read is **authorized by the app**, and no browser reaches a sync engine directly.                                             | `electric.py`; the proxy exists for this           |
| C3  | Deployed state is **disposable** — a schema or epoch change resets staging and testing rather than migrating.                       | <../../AGENTS.md> § Refactoring                    |
| C4  | The runner event log is the source of truth; the fold is derived and rebuildable under a new `projection_epoch`.                    | [Thread view synchronization](thread_view_sync.md) |

**C2 is about auth, not about who picks the window.** A client asking for rows 50–150 of a thread it
may read asks for nothing it is not entitled to. What C2 forbids is a bound reaching outside the
authorized thread, a request letting one reader pull unbounded volume, and any path that skips the
app.

## Product behaviour

| ID  | Requirement                                                                                                                                                                                | Source                                             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------- |
| P1  | Opening a thread shows its **tail** quickly — not its history.                                                                                                                             | Product decision, 2026-09-22                       |
| P2  | New items and revisions appear **live**, with no user action.                                                                                                                              | Product behaviour                                  |
| P3  | An existing item can change **anywhere in history**, not only at the tail.                                                                                                                 | [Thread view synchronization](thread_view_sync.md) |
| P4  | A reader can scroll back **arbitrarily far**, incrementally, with bounded work per step.                                                                                                   | Product behaviour                                  |
| P5  | The reader's **place survives every sync event**: no DOM teardown, no scroll jump, nothing already shown withdrawn.                                                                        | Owner, 2026-09-22                                  |
| P6  | A **transient disconnect** keeps what is on screen and resumes without refetching it.                                                                                                      | Owner, 2026-09-22                                  |
| P7  | A **stale-epoch read is refused, never served.** A rebuild may cost the reader a full reload.                                                                                              | <../debug/conversation_acceptance.md>              |
| P8  | **The client chooses its content selection** — text, reasoning, tool arguments, output — and that choice composes with streaming. Selecting nothing still returns metadata and references. | [§ Queries](thread_view_sync.md#queries)           |
| P9  | Pending and optimistic commands reconcile after a lost reply.                                                                                                                              | Product behaviour                                  |
| P10 | A tab left open for months holds **bounded state**: a limited tail and reading window, with eviction.                                                                                      | [Thread view synchronization](thread_view_sync.md) |

**P7 is cheap.** The epoch exists for forward compatibility, not for a runtime event: nothing at
runtime mints one. `THREAD_FOLD_EPOCH` (`agent_runtime/view/recording.py`) is stamped when a
thread's fold is first created, and a later batch under a different constant raises rather than
refolding. Only a deploy that changes the fold's output shape changes it, and under C3 the answer is
to reset the data rather than swap it under a reader. So P7 requires only the refusal — a `410`
instead of rows from the new epoch, which is what stops mixed-shape rows being served. A seamless
swap that keeps a draft is nicer than required, and an implementation need not build one.

**P8 is not an optimisation to bake into the protocol.** "Reasoning is lazy, text is eager" is the
client's choice to make, per reader.

## Sync semantics

| ID  | Requirement                                                                                                                                | Source                                                                |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| S1  | A body never renders content its **own reference does not name** — no showing a later revision's bytes under an older revision's metadata. | `thread_store.test.tsx`: bodies render as far as each reference spans |
| S2  | Rows render in **thread order** whatever order they arrive in.                                                                             | P2, P4                                                                |
| S3  | A reader can distinguish **"caught up"** from "still arriving", per whatever unit it subscribes in.                                        | The `view_state` catch-up gate                                        |
| S4  | A revision to a row the reader **currently holds** always reaches it. No silent staleness.                                                 | P2, P3                                                                |

**S4 forbids the tail-only assumption.** Threads are usually edited near their tail. That is an
observation about traffic and must not become an assumption in the protocol: an edit in the middle
of a reader's window is delivered on the same terms as an edit to the last row. The trap is that
`entity_index` and `revision_cursor` are independent axes — a row written long ago and edited just
now has a low index and a high revision. A delta filters on both, the window by index and freshness
by revision; one that conflates them into "everything after cursor X" implements the tail-only
assumption. So do scanning only the last _K_ rows for changes, a changes feed that keeps only recent
entries, and ordering a window query by revision and truncating it.

A reader may stay subscribed to the tail while it looks elsewhere, so it can tell the thread is
moving and keep `view_state` current. That is a permission, not a requirement.

## Efficiency

| ID  | Requirement                                                                                                                           | Source                                                                                                                |
| --- | ------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| E1  | Requests on open are **O(1)** in thread size.                                                                                         | Measured on `agentplane-testing`, 2026-09-22: about 0.4 s a request, cold shape or warm, about sixty on the open path |
| E2  | Bytes on open are bounded by **what is rendered**, not by thread length.                                                              | P1                                                                                                                    |
| E3  | Streaming adds **≈0 requests** per arriving item.                                                                                     | E1's round-trip cost                                                                                                  |
| E4  | Scrolling back loads **only the new page** — not the window a reader already holds.                                                   | Owner, 2026-09-22                                                                                                     |
| E5  | Nothing already held is **re-transferred**; revalidation over retransfer.                                                             | E2, E4                                                                                                                |
| E6  | **No timer polling.** Updates arrive over a held connection — WebSocket, SSE or long poll — never a request re-issued on an interval. | Owner, 2026-09-23                                                                                                     |

**E6 allows a long poll.** A request the server holds until something changes, and the client
re-issues when it returns, waits on a change rather than on a clock. What E6 rules out is a request
sent every _n_ seconds whether or not anything changed: an idle reader makes no requests.

## Operability

| ID  | Requirement                                                                                               | Source                             |
| --- | --------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| O1  | Per-reader server state is **bounded**, and preferably **shared** between readers of one thread.          | `ELECTRIC_MAX_SHAPES`, our setting |
| O2  | The sync tier scales **horizontally**; staging runs two app replicas.                                     | <../app/README.md>                 |
| O3  | **Debuggable**: what a client is subscribed to, and why it received a given row, is answerable from logs. | Owner, 2026-09-22                  |
| O4  | **Few moving parts.** A design that needs a new engine owes an argument that the problem needs one.       | Owner, 2026-09-22                  |

## Desires

Weighed, not required. A design that fails a desire owes an argument that what it wins elsewhere is
worth more.

- **D1 — moving the window never re-sends what the client holds.** Closing the previous watch and
  opening `O(1)` new ones is fine; the new watch must not send data the client already has. A client
  holds rows 100–200 at revision 9932 and the reader scrolls up to 50–150: it fetches 50–99, learns
  of any change to 100–150 since revision 9932, and drops 151–200. It must not receive 100–150 again.
  Subscription count is free; re-transfer is the cost. A viewport that is one changing predicate
  re-sends its overlap; non-overlapping partitions, a client that states what it holds, and subset
  reads inside a shape whose predicate never moves do not. A reader that drops a page and scrolls
  back to it re-downloads it — that is the cost of bounding held pages (P10), not a D1 violation.
- **D2 — no seam between windowed and on-demand content.** Whether a body streams or is fetched is
  one mechanism with a parameter, not two code paths that behave differently.
- **D3 — incrementally reachable.** A design reachable in steps, each shippable and better than the
  last, beats one that has to land whole.
- **D4 — streaming costs the delta.** Tokens appended to a body cost the client traffic in
  proportion to what was appended, not a re-send of the prefix it holds. Owner, 2026-09-23.
- **D5 — a completed body is eventually stored once.** Once a message has finished streaming, it is
  at some point compacted to its final version: a constant number of rows per completed body, not
  one chunk per appending batch and one manifest per revision. Compaction does not change the
  content, so it is not a new version: a reader holding the body refetches nothing. Owner,
  2026-09-23.

**D4 is mostly given by the storage.** A body is insert-only chunks: each ingestion batch that
appends to it writes one chunk row holding only that batch's text (`agent_runtime/view/payloads.py`).
A design that syncs chunk rows transfers the delta, at batch rather than token granularity. What
fails D4 is re-sending a body whole on each change. Two costs ride on each append regardless: the
entity row whose reference moved, and a replacement, which starts a new generation and so is a new
body.

**D5 is safe because the fold is derived (C4):** an intermediate revision's body can always be
rebuilt from the event log, so compaction deletes nothing unrecoverable.
