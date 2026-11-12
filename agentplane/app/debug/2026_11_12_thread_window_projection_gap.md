# Thread window stops at an older projection cursor

The 2026-11-07 CI failure of `//agentplane/app:test_thread_window_browser` on
[BuildBuddy](https://app.buildbuddy.io/invocation/b48eb4c8-d3bc-54cb-be56-3b66759cc62c)
occurred in `test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place`.
After the test appended items 130–164, the browser's `data-projection-cursor` remained 264
for 30 seconds, while it expected 334.

The Playwright trace's History summary responses show `committedCursor=334` at 21:13:01.127 UTC
and `foldedThrough=334` at 21:13:01.286 UTC. Thus the append, archive, and fold completed;
the gap lies between the folded view row and the browser's displayed view. The page had
opened an Electric entity shape and loaded older pages through POST subsets. Its last live
entity request was still open when the assertion failed. The trace has an Electric client
fast-retry-loop warning near the older-page requests, but it does not include the body of
that open SSE response. The warning is a lead, not proof that the stream skipped the update.
Runner Gateway `UNAVAILABLE` warnings also appear; they do not explain the already-folded
cursor.

The failure artifacts do not contain a fresh Electric snapshot or a direct read of the
folded view row at failure time. If this assertion fails again, the test has ten seconds
to gather both alongside the browser cursor into an undeclared output artifact; a failed
probe leaves the original browser assertion as the test failure. A view row at 334 with an Electric snapshot at 264 would
place the problem in Electric replication or shape serving. If both are at 334, the live
stream or browser application is the remaining boundary. A later fresh run must establish
that before changing delivery or recovery behavior.

One uncached run and five more uncached runs of this scenario passed on 2026-11-12
([single run](https://app.buildbuddy.io/invocation/c251a836-285f-4dd7-a09c-7d061f6e20ba),
[five runs](https://app.buildbuddy.io/invocation/b1c349a1-d221-4302-a802-607fe55d10b3)).
Those passes confirm the diagnostic path does not disturb the normal scenario; they do not
exercise its failure branch or establish a runtime repair.

A temporary impossible cursor expectation exercised the failure branch on RBE. The first
probe exposed the app's same-origin POST requirement; after the test sent its Origin header,
the [forced run](https://app.buildbuddy.io/invocation/9bcbc8c1-3a90-46eb-a82e-b5df48920fd0)
reported committed, folded, direct view, browser, and fresh Electric snapshot cursors all
at 334. The impossible expectation was restored before this change was committed.
The [bounded-artifact run](https://app.buildbuddy.io/invocation/38d07614-2f31-4904-b582-14e5aceee18e)
then confirmed that the original Playwright assertion remained the failure and
`thread-window-projection-gap.json` was published as an undeclared test output.
