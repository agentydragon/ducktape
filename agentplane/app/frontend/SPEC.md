# Thread history scrolling

`VirtualizedHistory` (`projected_session.tsx`) renders one thread's transcript: an unbounded,
variable-height list of runs, lifecycle events, and streaming content, paginated from the
electric-backed store. This page states what it guarantees to the reader, independent of how the
guarantee is met — today, a hand-rolled scroll-anchor layer over `@tanstack/react-virtual`.
agentydragon/ducktape#7841 evaluated `react-virtuoso` as a replacement and rejected it; a further
spike is evaluating `content-visibility` plus native `overflow-anchor` in its place. Either way,
a candidate is judged against this contract, not against a smaller diff or a smaller bundle.

## Guarantees

- **Opening a thread** — including switching from another thread — scrolls to the thread's tail;
  the most recent message is what the reader sees first.
- **Opening a thread loads generously up front** — several pages, not just enough to fill the
  screen — so scrolling through recent history reads as an ordinary lazy-loaded scroll rather than
  a page-by-page stop-and-wait. A thread shorter than that loads in full immediately.
- **Reaching within one screen of the top of what is loaded** asks for the page before it — before
  the reader can actually see the top, not only once they reach the very edge.
- **An appended message, reader at the bottom**: the view follows it into view. "At the bottom"
  tolerates a small amount of slack — a reader a few pixels off the exact bottom (scroll momentum,
  subpixel rounding) still counts as following.
- **An appended message, reader scrolled up**: nothing moves. The reader keeps reading exactly
  where they were.
- **A message actively streaming, reader following, it is the last message**: the view tracks its
  growth so the growing edge stays visible — the message's own start must never scroll off the top
  of the screen while it is still generating.
- **A message actively streaming or otherwise resizing anywhere else** — off-screen above or below
  the viewport, or while the reader is not following — must not move the reader's scroll position,
  even by a pixel. This is the guarantee the hand-rolled implementation has repeatedly gotten wrong
  (agentydragon/ducktape#7784, agentydragon/ducktape#7829), and the one a candidate replacement has
  to actually satisfy, not merely avoid triggering.
- **A deliberate scroll** (wheel, drag, keyboard, touch) away from the bottom disengages following
  immediately, without lag or fighting the input.
- **Returning to the bottom** re-engages following, with the same tolerance as above.
- **Reaching the top loads older messages**, and whatever was visible immediately before the load
  is still visible, in the same position, immediately after — no blank flash, no snap-then-correct,
  no wrong position even for a single frame. Holds regardless of the heights of the newly-inserted
  rows, and whether or not the thread was scrollable at all before the load.
- **A window/viewport resize** (browser resize, mobile address-bar collapse, orientation change, an
  on-screen keyboard opening): following stays pinned to the bottom through the resize; not
  following keeps the reader's current row in place and shifts only off-screen content.
- **Text selection, copy, and find-in-page** work across the entire thread, not only whichever rows
  happen to be mounted right now.
- None of the above ever produces a visible flash of blank space or a scrollbar that visibly jumps
  and then corrects.

## Verification

Chromium only — no guarantee here extends to Safari/WebKit, and no candidate needs an
`overflow-anchor` fallback for it.

Existing coverage, all Playwright against a real backend:

| Guarantee                                                                                                                                                                                  | Test                                                                                                                                                                             |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Opening a thread / switching threads starts at the tail                                                                                                                                    | `test_switching_threads_starts_at_each_threads_tail` (`test_thread_browser.py`)                                                                                                  |
| Append-follow; disengage on manual scroll, re-engage at the bottom                                                                                                                         | `test_thread_follows_bottom_until_reader_scrolls_up` (`test_thread_browser.py`; `desktop`/`phone` × `normal`/`raw`)                                                              |
| Reaching the top loads older rows without moving the reader; the tail growing past a page while the reader is scrolled back doesn't move them either; opening loads several pages up front | `test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place` (`test_thread_window_browser.py`)                                                            |
| A thread shorter than the eager initial load shows in full, with no scrollbar and no further request                                                                                       | `test_a_thread_shorter_than_the_eager_load_shows_in_full_without_a_scroll` (`test_thread_window_browser.py`)                                                                     |
| Resume/reload preserves shape and position                                                                                                                                                 | `test_a_long_offline_gap_resumes_the_same_shape_without_losing_the_draft`, `test_terminal_shape_error_keeps_rows_until_a_refresh_replaces_the_window` (`test_thread_browser.py`) |
| Lazy pagination elsewhere in the transcript UI                                                                                                                                             | `test_chronological_debug_is_lazy_paged_and_keeps_the_thread` (`test_thread_browser.py`)                                                                                         |

No test yet isolates the streaming/idle-resize guarantee in general — only the specific shape
`test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place` exercises. A
candidate spike adds a regression test for the general case before it can claim to satisfy it.

A candidate implementation is evaluated the way agentydragon/ducktape#7841 evaluated
`react-virtuoso`: each variant run 3× against the tests above plus
`//agentplane/app/frontend:visual`, `vitest_test`, and `sw_test`, against a control run the same
way on unmodified `devel`. A guarantee counts as met only at 3/3, not on a majority — and a test
setup that avoids exercising the guarantee (nothing mounted above the viewport, no rows present to
grow) doesn't count as meeting it.

Performance: no regression against today's baseline at 500 and 2,000 rows, measured with a real
profiler (a Playwright trace or Chrome's performance timeline) — not ad hoc timers. 2,000 rows is
a placeholder past any thread length observed so far; revisit against real staging data if it
turns out to matter.

Report, for any candidate: lines changed in `projected_session.tsx`, ref/state count before and
after, and the bundled JS size delta — the same shape agentydragon/ducktape#7841 reported for
`react-virtuoso`. These inform the decision but don't override it.

**Decision rule**: reject a candidate unless it satisfies every guarantee above at the 3/3 bar,
including the streaming/idle-resize guarantee that motivated this investigation. A smaller diff or
a smaller bundle does not offset a guarantee that regresses — and a custom correction layer
fighting a library's own internal one (agentydragon/ducktape#7841's `it2`) is a regression, not a
mitigation.
