# Thread sync — what is left

One Electric shape per thread, windowed by subset snapshots, is deployed:
<../../docs/thread_view_sync.md>. What any implementation owes the browser, with the IDs cited
below, is <../../docs/thread_sync_requirements.md>. This plan holds the work still open on the
Electric design, and the path to trying a second implementation beside it.

The Electric work goes as far as Electric goes without working against it. Where it cannot do what
following an agent wants, the work stops and the mismatch goes in
<../../docs/thread_sync_electric_limits.md>; those mismatches are what a second implementation
would be for.

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
6. **Completing a message re-sends its text.** `item_completed` always writes the final text as a
   new generation (`fold.py`), so its reference moves and a reader who followed the stream
   downloads the whole text once more. Keeping the generation when the completed text is what was
   streamed removes that, for every implementation. It also leaves the streamed generation's
   chunks and manifests unreferenced, which is most of what **D5** would compact.
7. **Compacting completed bodies (D5).** Electric's behaviour is pinned
   (`test_electric_chunk_compaction.py`). Rewriting chunk 0 to the whole text and deleting the
   rest in one transaction needs no client change, since the store applies only inserts. Every
   follower of the field still receives the compacted text once, twice under `replica=full`; that
   cost is Electric's (<../../docs/thread_sync_electric_limits.md>). Dropping `replica=full` from
   the chunk shapes halves it, since nothing reads a chunk update's or delete's values. **S1** for
   intermediate references needs the replaced chunks' lengths, which `thread_payload_chunk` has
   no column for; without them a compacted body answers only its final reference.
8. **Measure it on `agentplane-testing`.**
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
is the moving window. Electric's `−` on P10 and `~` on E6 are items 1 and 2 above; its `~` on E5 is
<../../docs/thread_sync_electric_limits.md> § What does not.
