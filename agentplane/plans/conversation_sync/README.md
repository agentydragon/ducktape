# Conversation sync — choosing a design

Opening a Thread takes tens of seconds to paint. The first diagnosis blamed Electric shape creation;
measurement says otherwise, and the difference reframed the whole problem. This directory holds the
options and the grounds for choosing between them.

**Nothing here is decided.** The deployed design is one of the options and is not favoured by being
deployed.

## The measurement, which grounds every option

Against deployed `agentplane-testing`, on threads whose shapes had never been created:

| Stage                                     | thread A   | thread B   |
| ----------------------------------------- | ---------- | ---------- |
| `/sync/interest`                          | 0.54 s     | 0.37 s     |
| `/sync/entities` (offset=now + snapshot)  | 0.89 s     | 0.81 s     |
| 3 bodies (`payload-interest` + `-chunks`) | 2.90 s     | 2.53 s     |
| **total**                                 | **4.33 s** | **3.72 s** |

A cold entity shape took 0.37–0.62 s and a warm one 0.36–0.53 s — **indistinguishable**, so what is
measured either way is a round trip, and shape creation was never the cost. The cost is **request
count**: ~0.4 s each, two per rendered body, ~60 on the open path for the specified 30-segment tail.
That is the reported twenty seconds — sixty ordinary requests, not one slow shape.

(Absolutes come through an agent proxy from outside the cluster, so a browser's round trip is
smaller. The `O(bodies)` → `O(1)` conclusion does not depend on them.)

## How this is organised

- **<requirements.md>** — what any design has to do, with stable IDs (`P1`, `E4`, `D2`…) that the
  options cite. Read this first; it is the thing to argue with.
- **<option_poll.md>** — the app answers HTTP; the client polls. `A0` whole conversation, `A1` delta.
- **<option_moving_window.md>** — one watch over a range the client moves, paying only the
  difference. The protocol P10 describes.
- **<option_app_push.md>** — the same delta, pushed over SSE, reusing machinery the app already has.
- **<option_electric_today.md>** — what is deployed, as a baseline.
- **<option_electric_pages.md>** — TanStack DB + Electric, partitioned into stable pages. The
  furthest worked out, because it is where the thinking started.
- **<prior_art.md>** — how other systems cut this, and which are worth an afternoon.

## Fit matrix

`+` meets it, `~` meets it with work or a caveat, `−` fails it, `?` unknown without investigation.
Cells are judgements from the option files, not measurements.

| Req                          | A0 poll all | A1 poll delta | Moving window | SSE push | Electric today | Electric pages |
| ---------------------------- | ----------- | ------------- | ------------- | -------- | -------------- | -------------- |
| P1 tail-first open           | −           | +             | +             | +        | +              | +              |
| P2 live updates              | ~ (1 s)     | +             | +             | +        | +              | +              |
| P3 history can change        | +           | +             | +             | +        | +              | +              |
| P4 scroll back               | + (free)    | +             | +             | +        | ~              | +              |
| **P5 place survives**        | +           | +             | +             | +        | **−**          | +              |
| P6 disconnect resumes        | +           | +             | +             | ~        | **−**          | +              |
| P7 epoch replacement         | +           | +             | +             | +        | +              | +              |
| **P8 client picks content**  | +           | +             | +             | +        | **−**          | **−**          |
| P9 command reconcile         | +           | +             | +             | +        | +              | +              |
| **P10 one moving watch**     | +           | ~             | **+**         | +        | **−**          | **−**          |
| S1 body ≤ its own revision   | +           | +             | +             | +        | ~              | +              |
| S2 ordering                  | +           | +             | +             | +        | +              | +              |
| S3 caught-up signal          | +           | +             | +             | +        | +              | ~              |
| S4 no lost update            | +           | +             | +             | +        | +              | +              |
| **E1 O(1) requests on open** | +           | +             | +             | +        | **−**          | +              |
| E2 bytes bounded             | −           | +             | +             | +        | +              | +              |
| E3 streaming ≈0 requests     | +           | +             | +             | +        | +              | +              |
| **E4 scroll loads only new** | n/a         | −             | **+**         | +        | −              | +              |
| E5 no re-transfer            | −           | ~             | +             | +        | −              | +              |
| O1 bounded/shared state      | +           | +             | +             | −        | −              | ~              |
| O2 horizontal scale          | +           | +             | +             | ~        | +              | +              |
| O3 debuggable                | +           | +             | +             | +        | ~              | ~              |
| **O4 few moving parts**      | +           | +             | +             | ~        | −              | **−**          |
| D1 no windowed/lazy seam     | +           | +             | +             | +        | −              | −              |
| D2 incrementally reachable   | +           | +             | +             | ~        | n/a            | −              |

### What the matrix says

- **P10 eliminates both Electric columns**, not on preference but on mechanism: a shape's predicate
  is fixed at creation, so a moved window is a different shape whose log replays from `offset=-1`.
  The page partition sidesteps the re-transfer by holding `N` immovable shapes, which is the thing
  P10 rules out.
- **The moving window is the only column that is all `+`.** That is not a claim that it is right —
  it is the option written _from_ these requirements, so it ought to score well, and the honest
  reading is that the requirements have not yet been stress-tested against it. Its costs are real
  and are in its own file: a delta query the app owes, a client trusted about what it holds, and a
  round trip per change batch.
- **The deployed design is the worst column**, and its failures are not a tuning problem: P5, E4,
  E5 and O1 all follow from a bound that moves with every appended segment.
- **A0's only real failures are E2 and E5.** A smaller list than "poll everything every second"
  sounds like, which makes it a serious fallback rather than a joke — particularly under D2, and
  particularly as the thing to ship if the deployed design has to go before its replacement is
  ready.
- **A1 is strictly worse than the moving window** and differs by one idea: `have`. Without it a
  reader that scrolls up re-downloads its whole new window. Keep A1 on the matrix only as the step
  before, not as a destination.

## What to settle next, in order

1. **Confirm P10 and P8 are binding.** Both are now stated; between them they disqualify every
   Electric design as written. If either is softer than it looks, say so before the rest is built on
   it.
2. **Prototype the delta query** — `segment_index` in range, whole for the backfill and
   `revision_cursor > $since` for the overlap — and **pin with a test that every mutation advances
   an entity's `revision_cursor`, including a body change.** That single assumption carries the
   moving window, A1 and the SSE option alike.
3. **Add `segment_index` to the fold.** Needed by every option that pages, including the Electric
   one, and a cursor cannot substitute: how many segments a cursor range covers depends on how
   densely a turn packs them.
4. **Read Zero and Replicache** — Replicache especially, whose pull protocol is this option
   specified properly, and worth copying rather than reinventing.

`subset__where` (<prior_art.md>) drops down the list: it could only help an Electric option reach
P10, and a narrow snapshot of a wide shape still leaves the live log carrying the whole
conversation.

#7592 — the page content shape — stays held, and under P10 it is unlikely to be what lands. The
extent on `PayloadRef` (S1) and `segment_index` are the parts of that work worth keeping whatever
wins.
