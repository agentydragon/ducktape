# Native scrolling resurrected a released nested anchor

The archive recording `ai-session-archive-history-recording-2026-10-31T10-08-03-977Z.json`
(SHA-256 `459512aac0f1faba04339c6955478d1ac36c0cbb3ce9439ba04be52dcaf233ca`)
retains 1,000 layout snapshots and reports 531 dropped layouts, with no dropped
marks. The retained capture starts with a tool group already expanded; it does not
include the expansion click itself.

Within the retained layouts, 128 paired `scroll-before`/`scroll-after` corrections
have reason `restore-committed-anchor`. Every pair has `following: false`.
At relative time 70,127.6 ms, upward scrolling reaches 10,262.4 px and restoration
writes 10,264.8 px. Subsequent frames similarly undo upward motion. The first
corrections occur with unchanged virtual extent, so new content growth is not
required. This is anchor restoration fighting scrolling, rather than tail-follow
remaining enabled.

`HistoryViewport.acceptMovement()` converts an accepted native scroll from a pinned
nested target to row-relative coordinates: it clears `target`, subtracts the old
`inset` from `offset`, then sets `inset` to zero. Commit 891a05cd1e (#13280) added
`targetKey` and remount lookup in `reconcile()`, but left this transition unchanged.
The stale key lets reconciliation reacquire the nested target and use the newly
row-relative offset against it. The old nested inset is effectively added to the
scroll position again, often clamping it to the bottom. A later anchor rebase can
repeat the cycle on the next wheel event.

Clearing `targetKey` with `target` releases both representations of the pin. The
stable entity identity still maps to its current row, and the normal post-commit
anchor rebase can choose the reader's newly visible semantic landmark. Nested
anchor recovery across prepends and DOM remounts remains available while the
reader is stationary.

The shared viewport regression expands a group, scrolls upward repeatedly through
a nested semantic landmark, and delivers `scrollend` between movements. Before the
fix it expects 460 px after the second movement but observes 700 px (the maximum):
BuildBuddy invocation `dd4b3be4-411d-4ca0-a893-6e2c4ab02537`. The other 11 viewport
tests pass before the fix, demonstrating the coverage gap: the existing deep
clicked-disclosure test has no keyed nested landmark, and the nested-anchor test
checks prepend/remount recovery without subsequent native scrolling.
