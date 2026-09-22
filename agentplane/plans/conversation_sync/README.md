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
  difference. The protocol D1 describes.
- **<option_app_push.md>** — the same delta, pushed over SSE, reusing machinery the app already has.
- **<option_electric_today.md>** — what is deployed, how it fails, and the seven reader flows traced
  through it.
- **<option_electric_pages.md>** — TanStack DB + Electric, repartitioned into stable pages.
- **<electric_primitives.md>** — what Electric actually offers, for both of the above.
- **<prior_art.md>** — how other systems cut this, and which are worth an afternoon.

## Fit matrix

`+` meets it, `~` meets it with work or a caveat, `−` fails it, `?` unknown without investigation.
Cells are judgements from the option files, not measurements.

| Req                          | A0 poll all | A1 poll delta | Moving window | SSE push | Electric today | Electric pages  |
| ---------------------------- | ----------- | ------------- | ------------- | -------- | -------------- | --------------- |
| P1 tail-first open           | −           | +             | +             | +        | +              | +               |
| P2 live updates              | ~ (1 s)     | +             | +             | +        | +              | +               |
| P3 history can change        | +           | +             | +             | +        | +              | +               |
| P4 scroll back               | + (free)    | +             | +             | +        | ~              | +               |
| **P5 place survives**        | +           | +             | +             | +        | **−**          | +               |
| P6 disconnect resumes        | +           | +             | +             | ~        | **−**          | +               |
| P7 rebuild swaps in          | +           | +             | +             | +        | +              | +               |
| **P8 client picks content**  | +           | +             | +             | +        | **−**          | ~ (shape/field) |
| P9 command reconcile         | +           | +             | +             | +        | +              | +               |
| S1 body ≤ its own revision   | +           | +             | +             | +        | ~              | +               |
| S2 ordering                  | +           | +             | +             | +        | +              | +               |
| S3 caught-up signal          | +           | +             | +             | +        | +              | ~               |
| S4 no lost update            | +           | +             | +             | +        | +              | +               |
| **E1 O(1) requests on open** | +           | +             | +             | +        | **−**          | +               |
| E2 bytes bounded             | −           | +             | +             | +        | +              | +               |
| E3 streaming ≈0 requests     | +           | +             | +             | +        | +              | +               |
| **E4 scroll loads only new** | n/a         | −             | **+**         | +        | −              | +               |
| E5 no re-transfer            | −           | ~             | +             | +        | −              | +               |
| O1 bounded/shared state      | +           | +             | +             | −        | −              | ~               |
| O2 horizontal scale          | +           | +             | +             | ~        | +              | +               |
| O3 debuggable                | +           | +             | +             | +        | ~              | ~               |
| **O4 few moving parts**      | +           | +             | +             | ~        | −              | **−**           |
| **D1 no overlap re-sent**    | −           | −             | **+**         | +        | **−**          | **+**           |
| D2 no windowed/lazy seam     | +           | +             | +             | +        | −              | −               |
| D3 incrementally reachable   | +           | +             | +             | ~        | n/a            | −               |

### What the matrix says

- **D1 does not split the options by family.** It asks that moving the window never re-send what
  the client holds, and says nothing about subscription count. Two very different designs pass —
  the page partition, because non-overlapping partitions never overlap by construction, and the
  moving window, because the client states what it has. Two fail for the same reason: a viewport
  that is one changing predicate replays itself, which is Electric-as-deployed and a delta poll
  without `have`.
- **A0 fails D1 hardest**, which is easy to miss because it has no window to move: it re-sends the
  whole conversation on every poll, held or not.
- **Nothing here is disqualified.** No requirement rules out a family; the differences are in what
  each costs to satisfy. P8 in particular is reachable with Electric, by giving each payload field
  its own content shape and letting the client subscribe to the ones it wants — at the price of
  shape count, which lands on **O1** rather than on P8.
- **The case against the Electric family is cumulative, not structural.** Shape count, the page
  boundary and its landing pad, a catch-up signal that becomes composite, and no per-card readiness
  signal. Each is affordable alone; the question is whether the pile is worth what shapes buy,
  which is a cache shared between readers **of the same conversation**.
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

## Measurement gates

What any chosen design has to be held to.

- Open-to-first-text for a 30-segment tail, cold and warm, with upstream shape-creation duration
  reported separately from transfer. `agentplane/acceptance/test_conversation_latency.py` is the
  instrument, and it only exercises what the deployment is running.
- Distinct Electric shape handles created during one turn: zero new shapes per completing item, and
  zero per arriving segment while a reader holds a history page. The second is flow 4, and the
  shape-creation log (landed in #7589) reports a handle per request, so counting distinct handles over
  a session answers both.
- **Shape count against `ELECTRIC_MAX_SHAPES=1024`**, before any Electric partition is built rather
  than after: pages × fields in use × concurrent conversations, given that shapes are shared between
  readers of one conversation.
- Browser coverage for the two flows nothing asserts end to end: scroll up and then stream (no hole
  appears, and the reader's place does not move), and a page's rows surviving an unsubscribe of a
  page above it.
- Small-file create/fsync latency on `seaweedfs-ovh` versus `local-path-ovh-ssd` from the Electric
  pod's node, before spending W1's PVC change.

## What to settle next, in order

1. **Decide whether shapes are worth their complexity**, now that neither D1 nor P8 rules them
   out. What they buy is a cache shared between readers of one conversation; what they cost is
   everything in option_electric_pages.md that is not about conversations. With few concurrent
   readers per conversation, that trade looks bad — but it is a judgement, not a derivation.
2. **Decide whether P7 is worth its machinery.** Nothing at runtime mints a new projection epoch:
   it is a deploy-time constant whose mismatch raises rather than reprojects, and no rebuild path is
   implemented. Every option scores `+` on it, so it discriminates nothing — what it does is carry a
   hidden-subtree double buffer that a design without selection rebuilds would not otherwise need.
   See <requirements.md> § P7.
3. **Prototype the delta query** — `segment_index` in range, whole for the backfill and
   `revision_cursor > $since` for the overlap — and **pin with a test that every mutation advances
   an entity's `revision_cursor`, including a body change.** That single assumption carries the
   moving window, A1 and the SSE option alike.
4. **Add `segment_index` to the fold.** Needed by every option that pages, including the Electric
   one, and a cursor cannot substitute: how many segments a cursor range covers depends on how
   densely a turn packs them.
5. **Read Zero and Replicache** — Replicache especially, whose pull protocol is this option
   specified properly, and worth copying rather than reinventing.

`subset__where` (<prior_art.md>) drops down the list: it could only help an Electric option reach
D1, and a narrow snapshot of a wide shape still leaves the live log carrying the whole
conversation — but it is cheap, and it would improve the Electric option on P1 and O1 regardless.

#7592 — the page content shape — stays held. The extent on `PayloadRef` (S1) and `segment_index`
are the parts of that work worth keeping whatever wins.
