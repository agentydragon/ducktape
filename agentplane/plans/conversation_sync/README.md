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

- **<requirements.md>** — what any design has to do, with stable IDs (`P1`, `E4`, `W1`…) that the
  options cite. Read this first; it is the thing to argue with.
- **<option_poll.md>** — the app answers HTTP; the client polls. `A0` whole conversation, `A1` delta.
- **<option_app_push.md>** — the same delta, pushed over SSE, reusing machinery the app already has.
- **<option_electric_today.md>** — what is deployed, as a baseline.
- **<option_electric_pages.md>** — TanStack DB + Electric, partitioned into stable pages. The
  furthest worked out, because it is where the thinking started.
- **<prior_art.md>** — how other systems cut this, and which are worth an afternoon.

## Fit matrix

`+` meets it, `~` meets it with work or a caveat, `−` fails it, `?` unknown without investigation.
Cells are judgements from the option files, not measurements.

| Req                          | A0 poll all | A1 poll delta | SSE push | Electric today | Electric pages |
| ---------------------------- | ----------- | ------------- | -------- | -------------- | -------------- |
| P1 tail-first open           | −           | +             | +        | +              | +              |
| P2 live updates              | ~ (1 s)     | +             | +        | +              | +              |
| P3 history can change        | +           | +             | +        | +              | +              |
| P4 scroll back               | + (free)    | +             | +        | ~              | +              |
| **P5 place survives**        | +           | +             | +        | **−**          | +              |
| P6 disconnect resumes        | +           | +             | ~        | **−**          | +              |
| P7 epoch replacement         | +           | +             | +        | +              | +              |
| **P8 client picks content**  | +           | +             | +        | **−**          | **−**          |
| P9 command reconcile         | +           | +             | +        | +              | +              |
| S1 body ≤ its own revision   | +           | +             | +        | ~              | +              |
| S2 ordering                  | +           | +             | +        | +              | +              |
| S3 caught-up signal          | +           | +             | +        | +              | ~              |
| S4 no lost update            | +           | +             | +        | +              | +              |
| **E1 O(1) requests on open** | +           | +             | +        | **−**          | +              |
| E2 bytes bounded             | −           | +             | +        | +              | +              |
| E3 streaming ≈0 requests     | +           | +             | +        | +              | +              |
| E4 scroll loads only new     | n/a         | +             | +        | −              | +              |
| E5 no re-transfer            | −           | +             | +        | −              | +              |
| O1 bounded/shared state      | +           | ~             | −        | −              | ~              |
| O2 horizontal scale          | +           | ~             | ~        | +              | +              |
| O3 debuggable                | +           | +             | +        | ~              | ~              |
| **O4 few moving parts**      | +           | +             | ~        | −              | **−**          |
| **W1 one subscription**      | +           | +             | +        | −              | **−**          |
| W2 no windowed/lazy seam     | +           | +             | +        | −              | −              |
| W3 incrementally reachable   | +           | +             | ~        | n/a            | −              |

### What the matrix says

- **The deployed design is the worst column**, and its failures are not a tuning problem: P5, E4,
  E5 and O1 all follow from a shape bound that moves with every appended segment.
- **The page partition fixes those and cannot fix P8, W1 or W2.** Content selection lives in a shape
  predicate, and a partition is inherently `N` subscriptions. Those are structural, and they are
  exactly the two things the owner flagged independently.
- **The two cheapest options score best on the things that keep going wrong.** A0 and A1 satisfy
  P5, P6, P8, W1 and W2 by construction, because there is no subscription to lose, no partition to
  cross, and the content selection is a query parameter. A1 gives up only `O1`-as-shared-cache,
  which the deployed design does not achieve anyway.
- **A0's only real failures are E2 and E5.** That is a smaller list than it feels like, and it makes
  A0 a serious fallback rather than a joke — particularly under W3.

## What to settle next, in order

1. **Is P8 a requirement or a preference?** It is in <../../docs/thread_view_sync.md> and the
   deployed implementation violates it. If it holds, both Electric columns are disqualified as
   written, and that decides most of the rest.
2. **Check Electric's `subset__where`** (<prior_art.md> § What to check first). If a wide shape can
   be snapshotted narrowly, the Electric option changes shape and may reach W1. Cheapest unknown on
   the board.
3. **Prototype A1's delta query** — `revision_cursor > $since` bounded to a window — and confirm the
   projector never revises an item without advancing it. That is the load-bearing assumption of the
   two cheapest options.
4. **Read Zero and Replicache** for whether either gives P8 + W1 + W2 without us building it.

Until 1 is answered the rest is premature, and #7592 — which implements the page content shape —
stays held.
