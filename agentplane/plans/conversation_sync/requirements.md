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

**P8 is the one the deployed design breaks.** The spec makes content selection a client query
parameter; the Electric implementation put the field set in a server-side shape predicate, where the
client cannot express it and the server cannot vary it per reader. Any option that bakes "reasoning
is lazy, text is eager" into the protocol fails P8 — that is an **optimisation**, and it belongs on
the client's side of the wire.

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

### D1 — one watch, moved in place

A reader holds a single subscription over the range it cares about, and moving that range costs only
the difference. Stated by the owner as a protocol, which is why it is worth writing out even as a
desire:

A client holds segments 100–200 at revision 9932. The reader scrolls up, so the client now wants
50–150. It should:

1. fetch **50–99 only** — the part it does not have;
2. learn of any change to **100–150 since revision 9932** — the part it has, which may be stale;
3. **move its existing watch** to 50–150, without re-downloading 100–150 and without opening a
   second one;
4. drop 151–200.

**No Electric design can do this**, and the reason is mechanical rather than aesthetic: a shape's
predicate is fixed when the shape is created, so 50–150 is a different shape from 100–200 and its
log replays from `offset=-1` — the overlap is re-transferred. The page partition
(§ option_electric_pages) avoids that re-transfer the only other way available, by holding `N`
immovable shapes.

So D1 is the sharpest axis between the families, **and it is a preference**. Electric scores badly
on it and stays in contention; how badly it should count is exactly the judgement to make, not one
this document can make for anybody.

- **D2 — no seam between windowed and on-demand content.** Whether a body streams or is fetched
  should be one mechanism with a parameter, not two code paths that behave differently. P8's
  implementation-side twin.
- **D3 — incrementally reachable.** A design we can get to in steps, each shippable and better than
  the last, beats one that has to land whole.
