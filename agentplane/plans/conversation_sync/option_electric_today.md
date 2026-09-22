# Option: keep what is deployed

The baseline that already exists, recorded so "do nothing" and "fix the worst of it" are on the
matrix rather than assumed away.

An interest is resolved per reader — `(anchor_cursor, tail_from, window_from?, window_before?)` —
and becomes an Electric shape over `conversation_entity`. Each rendered body is a second shape,
reached through a `payload-interest` call that hands the client its extent.

## What it gets right

**P1, P2, P3, P7, P9, S1, S2, S4, O2** all hold today, and `test_thread_browser` covers most of
them. The epoch double-buffer (P7) is genuinely good and worth keeping in any option: the pending
selection syncs in a hidden subtree and swaps only once caught up, so the visible tree never blanks.

## What it gets wrong, in order

- **E1** — the open path is `O(bodies)`: two round trips each, ~60 for a 30-segment tail. This is
  the reported twenty seconds, and it is the reason this plan exists.
- **P5, E4, E5** — paging up re-resolves the interest, which changes the shape's predicate, which is
  a different shape. The reader's whole window is re-snapshotted, and its place is restored
  afterwards by finding `[data-conversation-anchor]` and correcting `scrollTop`.
- **P5 again, via rotation** — a client-side threshold at 60 segments and a server-side
  `ConversationInterestExpiredError` at the same count both rebuild the selection. With a history
  page open the count sits at 59, so streaming trips it every couple of segments and opens a hole in
  the middle of the view (see option_electric_pages.md § flow 4).
- **P8, D1** — the content selection is not the client's, and there is no watch to move: every
  change of window is a new shape.
- **O1** — the bound moves with every appended segment, so no two readers and no two opens of one
  conversation share a shape. The 1024-shape LRU churns behind a cache nobody hits.

## Fixing it in place

Two of these have local fixes that do not need a new design: the `payload-interest` round trip can
go (the extent belongs on the reference), and the per-revision chunk bound can go (bound the shape
to the generation). Those are worth taking whatever else is decided — they are in
option_electric_pages.md § W9 as the partition-independent pieces, and #7592 implements them.

The rest — P5, E4, E5, O1 — are consequences of a bound that moves, and no local fix reaches them.
