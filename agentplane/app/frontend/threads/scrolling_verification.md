# Verifying thread history scrolling

The reader-facing contract is [`../SPEC.md`](../SPEC.md). This document records
implementation context and how to assess changes against that contract.

`VirtualizedHistory` in `projected_session.tsx` currently uses a scroll-anchor layer over
`@tanstack/react-virtual` and reads paginated rows from the Electric-backed store.
Issue #7841 evaluated `react-virtuoso` and rejected it; another spike is evaluating
`content-visibility` plus native `overflow-anchor`. A candidate is judged against the
contract, not against a smaller diff or bundle. Prior scroll regressions: #7784 and #7829.

## Existing coverage

Existing coverage, all Playwright against a real backend:

| Guarantee                                                                                                                                                                                  | Test                                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Opening a thread / switching threads starts at the tail                                                                                                                                    | `test_switching_threads_starts_at_each_threads_tail` (`test_thread_browser.py`)                                                                                                                                                         |
| Append-follow; disengage on manual scroll, re-engage at the bottom                                                                                                                         | `test_thread_follows_bottom_until_reader_scrolls_up` (`test_thread_browser.py`; `desktop`/`phone` × `normal`/`raw`)                                                                                                                     |
| Opening or closing a run, a call, or an output past its height cap keeps the clicked row in place: mid-thread, with output arriving, and for a reader following the tail                   | `test_opening_a_call_and_its_output_leaves_the_clicked_line_where_it_was` (`desktop`/`phone` × `mid-thread`/`following`), `test_opening_a_call_while_output_streams_in_leaves_the_clicked_line_where_it_was` (`test_thread_browser.py`) |
| Reaching the top loads older rows without moving the reader; the tail growing past a page while the reader is scrolled back doesn't move them either; opening loads several pages up front | `test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place` (`test_thread_window_browser.py`)                                                                                                                   |
| A thread shorter than the eager initial load shows in full, with no scrollbar and no further request                                                                                       | `test_a_thread_shorter_than_the_eager_load_shows_in_full_without_a_scroll` (`test_thread_window_browser.py`)                                                                                                                            |
| Resume/reload preserves position and draft                                                                                                                                                 | `test_a_long_offline_gap_resumes_the_same_shape_without_losing_the_draft`, `test_terminal_shape_error_keeps_rows_until_a_refresh_replaces_the_window` (`test_thread_browser.py`)                                                        |
| Lazy pagination elsewhere in the transcript UI                                                                                                                                             | `test_chronological_debug_is_lazy_paged_and_keeps_the_thread` (`test_thread_browser.py`)                                                                                                                                                |

No test yet isolates the streaming/idle-resize guarantee in general — only the specific case
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
