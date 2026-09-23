# Thread sync — what is left

One Electric shape per thread, windowed by subset snapshots, is deployed:
<../../docs/thread_view_sync.md>. What any implementation owes the browser, with the IDs cited
below, is <../../docs/thread_sync_requirements.md>. This plan holds the work still open on the
Electric design, and the path to trying a second implementation beside it.

## Open on the Electric design

1. **Eviction (P10).** The store keeps every row and body it has loaded until the thread closes.
   The rule has to keep **P5**: evict rows outside the virtualizer's range and a margin, holding the
   scroll anchor while the list above it shrinks, and release their bodies' pair subsets. The store
   already ignores live updates to rows it does not hold, so evicting a row is dropping it, and
   scrolling back re-reads it as a subset. Done when retained heap and row counts stabilize over
   repeated scroll, load and evict cycles while the thread grows (<../../docs/thread_view_sync.md>
   § Acceptance evidence).
2. **A thread with no fold yet re-reads its scope on a one-second timer (E6).** The scope read
   should wait on `ThreadUpdates.changes` until the fold exists, and the client re-issue it on
   return.
3. **The live log over SSE.** Long polling already meets E6; SSE saves a request per change batch.
4. **Pending commands past the newest 200.** The pending subset returns the newest 200 with no
   older page and no count, and the pending panel has no height cap, so on a phone it can squeeze
   the history view to nothing. Porting means a subset form such as
   `… pending = true AND entity_index < $1`, a load-older for it, and the cap. The design doc's
   "keyset page by admission cursor" describes the page that does not exist yet.
5. **Electric's server memory** under history growth, a restart and a stalled reader is still an
   adoption gate (<../../docs/thread_view_sync.md> § Server memory ownership). Probes exist on the
   parked spike PR #7490 (`shape_history_memory_test`, `shape_stalled_reader_test`,
   `shape_capacity_test`); they need rewriting against `testing/electric_service.py` and the thread
   tables.
6. **Compacting completed bodies (D5).** Compaction keeps the body's identity — owner, generation
   and every reference to it — so the entity row does not change. What reaches a reader holding the
   body is the storage change itself:
   - a delete for each chunk it replaces, which must not withdraw text the reader shows;
   - the compacted row, which the field's live shape pushes to every reader following the field,
     including readers that do not hold the body: one re-send of the text. Keeping compacted rows
     out of the live shape avoids it, at the cost of a second read path (**D2**); compacting late
     makes the re-send rare either way.

   **S1:** the compacted row must answer a reference as far as it spans, as the chunks did. If it
   keeps the length of each chunk it replaces, it answers every revision's reference; without those
   lengths only the final reference resolves, and one naming an intermediate revision gets `410`.
   Nothing needs an old revision's body outside debugging, and the event log keeps it. First, pin
   Electric's behaviour: whether a delete carries the row's text, and whether a compaction
   transaction's changes arrive together.

7. **Measure it on `agentplane-testing`.**
   - Open to first text for a 30-row tail, cold and warm, timed per stage.
   - A PING turn under 3 s.
   - The live log's traffic for a reader scrolled away from an active tail (**E5**).
   - Shapes against `ELECTRIC_MAX_SHAPES`: one entity shape per thread, plus one per payload field
     in use.

## A second implementation

<seams.md> is where one plugs in, so several can live on `devel` at once and a deployment picks
one. Whether to build one is for the measurements above to decide: if Electric meets them, a second
implementation is an experiment rather than a replacement. The candidates:

- <option_window_poll.md> — the client long-polls a range of positions and fetches bodies by
  reference. The cheapest to build.
- <option_moving_window.md> — one watch over a range the client moves, paying only the difference.
- <option_app_push.md> — the same delta, pushed over SSE.

<prior_art/README.md> scores other systems against the requirements; Zero is the one to consider if
a new engine becomes acceptable (**O4**).

Before either windowed candidate trusts `since`, a test must pin that every mutation advances an
entity's `revision_cursor`, a body change included, and that a thread's revisions become visible in
commit order.

### Fit

`+` meets it, `~` meets it with work or a caveat, `−` fails it. Cells are judgements from the option
files, not measurements. Rows where every column is `+` are left out.

| Req                   | Electric (deployed) | Window poll | Moving window | SSE push |
| --------------------- | ------------------- | ----------- | ------------- | -------- |
| P6 disconnect resumes | +                   | +           | +             | ~        |
| P10 bounded tab state | −                   | +           | +             | +        |
| E4 scroll loads new   | +                   | −           | +             | +        |
| E5 no re-transfer     | ~                   | −           | +             | +        |
| E6 no timer polling   | ~                   | +           | +             | +        |
| O1 bounded/shared     | +                   | +           | +             | −        |
| O2 horizontal scale   | ~                   | +           | +             | ~        |
| O4 few moving parts   | ~                   | +           | +             | ~        |
| D1 no overlap re-sent | +                   | −           | +             | +        |
| D3 incremental        | +                   | +           | +             | ~        |

The window poll's `−` cells are one omission — the client never says what it holds — and adding it
is the moving window. Electric's `−` on P10 and `~` on E6 are items 1 and 2 above.
