# Conversation sync — what a design has to do

Stable IDs so options and the fit matrix can cite them. Each says where it comes from, because a
requirement nobody can source is a preference and should be argued as one.

## Constraints — not traded away, assumed by every option

| ID  | Constraint                                                                                                                               | Source                                   |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| C1  | The conversation fold is **materialized in Postgres**. Options differ in how a browser learns about it, not in whether it exists.        | Owner, 2026-09-22; already built         |
| C2  | Every read is **authorized by the app**. No browser reaches a sync engine directly, and no bound a browser sends can widen what it sees. | `electric.py`; the proxy exists for this |
| C3  | Deployed state is **disposable** — a schema or epoch change resets staging and testing rather than migrating.                            | <../../../AGENTS.md> § Refactoring       |
| C4  | The runner event log is the source of truth; the fold is derived and rebuildable under a new `projection_epoch`.                         | <../../docs/thread_view_sync.md>         |

## Product behaviour

| ID  | Requirement                                                                                                                                                                                | Source                                                  |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| P1  | Opening a conversation shows its **tail** quickly — not its history.                                                                                                                       | Product decision, recorded 2026-09-22                   |
| P2  | New items and revisions appear **live**, with no user action.                                                                                                                              | Deployed behaviour                                      |
| P3  | An existing item can change **anywhere in history**, not only at the tail.                                                                                                                 | <../../docs/thread_view_sync.md>                        |
| P4  | A reader can scroll back **arbitrarily far**, incrementally, bounded work per step.                                                                                                        | Deployed behaviour                                      |
| P5  | The reader's **place survives every sync event**: no DOM teardown, no scroll jump, nothing already shown withdrawn.                                                                        | Owner, 2026-09-22                                       |
| P6  | A **transient disconnect** keeps what is on screen and resumes without refetching it.                                                                                                      | Owner, 2026-09-22; `test_projected_browser…` asserts it |
| P7  | An **epoch replacement** is invisible past a catch-up state — no reload, draft preserved.                                                                                                  | <../../debug/conversation_acceptance.md>                |
| P8  | **The client chooses its content selection** — text, reasoning, tool arguments, output — and that choice composes with streaming. Selecting nothing still returns metadata and references. | <../../docs/thread_view_sync.md> § Queries              |
| P9  | Pending and optimistic commands reconcile after a lost reply.                                                                                                                              | Deployed behaviour                                      |

**P8 is what the deployed design breaks, and it is not hard to satisfy.** The spec makes content
selection the client's; the deployed implementation has no parameter for it, and the page design
put a fixed field set in a shape predicate. Baking "reasoning is lazy, text is eager" into the
protocol is an **optimisation** on the wrong side of the wire.

An earlier draft of this file said P8 **disqualifies** the Electric family. That was wrong. `field`
is a column of the chunk table, so a field selection is a `where` predicate and therefore part of a
shape's identity — but that means **one shape per field**, not no shape at all, and the client
chooses P8-style by choosing which of them to subscribe to:

- A reader always holds the entity shapes, which carry metadata and references. "Selecting nothing
  still returns metadata and references" holds by default.
- It subscribes to the `text` content shape because it always renders text; to `reasoning` only if
  it wants reasoning; to `output` when a disclosure opens, or from the start if it wants it
  streamed. **That is the client choosing, and it composes with streaming** — every content shape is
  live like any other.
- Per-**field** shapes share better than per-selection ones: a reader wanting `{text}` and one
  wanting `{text, reasoning}` share the `text` shape, where `field IN ('text')` and
  `field IN ('text','reasoning')` would be two shapes with overlapping contents.

What it costs is shape count: pages × fields in use, rather than pages. That is an **O1** problem
against `ELECTRIC_MAX_SHAPES`, not a P8 problem.

So no option on the matrix is disqualified by P8. It separates designs that have a parameter for it
from designs that do not, and every design here can grow one.

## Sync semantics

| ID  | Requirement                                                                                                                                | Source                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------- |
| S1  | A body never renders content its **own reference does not name** — no showing a later revision's bytes under an older revision's metadata. | `test_http_admission_ahead_of_replay…` |
| S2  | Segments render in **conversation order** whatever order they arrive in.                                                                   | Implied by P2/P4                       |
| S3  | A reader can distinguish **"caught up"** from "still arriving", per whatever unit it subscribes in.                                        | `view_state` catch-up gate             |
| S4  | A revision to an item the reader **currently holds** always reaches it. No silent staleness.                                               | P3 + P2                                |

## Efficiency

| ID  | Requirement                                                                                                   | Source                          |
| --- | ------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| E1  | Requests on open are **O(1)** in conversation size. Today they are `O(bodies)`, which is the reported defect. | Measured 2026-09-22 (§ README)  |
| E2  | Bytes on open are bounded by **what is rendered**, not by conversation length.                                | P1                              |
| E3  | Streaming adds **≈0 requests** per arriving item.                                                             | Measured: ~0.4 s per round trip |
| E4  | Scrolling back loads **only the new page** — not the window a reader already holds.                           | Owner, 2026-09-22               |
| E5  | Nothing already held is **re-transferred**; revalidation over retransfer.                                     | E2/E4                           |

## Operability

| ID  | Requirement                                                                                               | Source                            |
| --- | --------------------------------------------------------------------------------------------------------- | --------------------------------- |
| O1  | Per-reader server state is **bounded**, and preferably **shared** between readers of one conversation.    | `ELECTRIC_MAX_SHAPES=1024`        |
| O2  | The sync tier scales **horizontally**; staging runs two app replicas.                                     | <../../app/README.md>             |
| O3  | **Debuggable**: what a client is subscribed to, and why it received a given row, is answerable from logs. | Owner, implied by this whole plan |
| O4  | **Few moving parts.** An option that needs a new engine owes an argument that the problem needs one.      | Owner, 2026-09-22                 |

## Desires, not requirements

Weighed, not required. Lettered `D` because option_electric_pages.md uses `W1`…`W9` for the work
items of an earlier draft, and two numbering schemes in one directory is how a citation comes to
mean the wrong thing.

**These do not disqualify anything.** An option that fails a desire owes an argument that what it
wins elsewhere is worth more — not an exit from the comparison.

### D1 — moving the window never re-sends what the client holds

The invariant, in the owner's words: closing the previous watch and opening `O(1)` new ones is
fine — **the new watch must not send the client data it already has.**

The worked case. A client holds segments 100–200 at revision 9932. The reader scrolls up, so the
client now wants 50–150. It should fetch **50–99**, learn of any change to **100–150 since revision
9932**, and drop 151–200. What it must not do is receive 100–150 again.

Note what this is _not_: a cap on subscriptions. An earlier draft of this file read "one watch, and
move it", and that turned an implementation shape into the requirement. The count is free; the
re-transfer is the cost.

**This splits the options cleanly, and not where the earlier version did:**

- **Overlapping windows re-send.** Any design where the viewport is one predicate that changes —
  Electric as deployed, or a delta poll without a `have` parameter — replays the new window whole.
  Electric has no choice about it: a shape's predicate is fixed at creation, so a moved range is a
  different shape and a fresh shape's log starts at `offset=-1`.
- **Non-overlapping partitions do not.** The page partition (§ option_electric_pages) never moves a
  bound, so scrolling up subscribes to a page the client does not hold and re-sends nothing. It pays
  for this in subscription count, which D1 no longer charges for.
- **A stated `have` does not either.** § option_moving_window sends the difference because the
  client says what it holds.

So D1 readmits the page partition, and leaves **P8** as the only hard thing standing against the
Electric family.

One honest edge: a reader that unsubscribes from a page, scrolls back to it and re-subscribes does
re-download it. That is not a D1 violation — the client dropped the data — but it is the cost of
bounding held pages, and a client that keeps them cached avoids it.

- **D2 — no seam between windowed and on-demand content.** Whether a body streams or is fetched
  should be one mechanism with a parameter, not two code paths that behave differently. P8's
  implementation-side twin.
- **D3 — incrementally reachable.** A design we can get to in steps, each shippable and better than
  the last, beats one that has to land whole.
