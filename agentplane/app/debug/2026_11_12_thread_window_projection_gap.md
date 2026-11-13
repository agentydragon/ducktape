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

The historical trace also separates the fast-loop warning from the entity shape. At
browser monotonic time 19453.981 ms, the client warns; the next reset request, at
19455.3 ms, is **`chunks/text`**, with `offset=-1`. Entity requests keep handle
`108823093-1794085978036639` and offset `34595184_22`. After the held older-page
POST, an entity GET at 20012.3 ms completes with 200 and an up-to-date control
message, and its next live SSE request begins at 20065.0 ms. Thus this warning
does not show that the entity stream reset or skipped its position. The pinned
client's fast-loop guard and snapshot pause behavior explain why the warning was
a plausible lead; only the payload shape's reset is visible here.

The failed target's `conversation-http2-ingress/container.log` begins with a later
scenario at 21:14:29 UTC, whereas this failure's gap begins around 21:13:01 UTC.
The fixture reuses that output path, so the retained ingress file cannot establish
what the failing scenario's proxy sent. The Electric log for the failing case
records startup and shape creation without request bodies or per-update delivery;
the Go app logs contain migration messages only. Neither supplies the missing
live-response body.

The controlled [resume-gap probe](https://app.buildbuddy.io/invocation/6f0b886f-b5a1-46c1-af6e-c15a76940769)
on effective source `e5d9161610` held the entity live GET that resumes after the
older-page subset, appended all 35 new items, waited until History folded through
334, then released the request. It passed freshly on RBE in 67.393 seconds,
including the original one-shape, reader-position and draft assertions. Its one
`TestRunner` execution was uncached (`strategy=remote`, one run and zero cached
runs). This rejects that reconnect/backlog gap alone under the tested schedule;
it does not reproduce or repair the historical failure. The temporary routing
change was removed.

If a future failure's direct view and fresh Electric snapshot both reach the
expected cursor, the remaining boundary needs the entity SSE contents received by
Chromium. The failure diagnostic uses
[`Network.streamResourceContent`](https://chromedevtools.github.io/devtools-protocol/tot/Network/#method-streamResourceContent)
to inspect the still-open response buffer and save only numeric view revision
cursors, change count, byte count/digest, malformed-frame and incomplete trailing
byte counts, and request/shape position in
`thread-window-entity-stream.json`. Entity text/state stays in memory. Chromium's
buffers are bounded to 1 MiB per response and 2 MiB total, within the existing
ten-second diagnostic budget. The original assertion remains the test failure if
capture fails.

An entity view update at the expected cursor in this artifact establishes that
Chromium received that update, directing investigation into client
filtering/application. Capture observes only one response's retained bytes.
Absence cannot exclude client handling: the update may have arrived on an earlier
response, been evicted, or appeared in a malformed or incomplete frame. Correlate
the request position, decoding counters and snapshot with the trace before
localizing a delivery gap. An unavailable response is missing evidence.
Browser-login setup timeouts remain a separate failure.

The [fresh baseline](https://app.buildbuddy.io/invocation/b9b4fba4-5d1c-4d80-9b43-b388d82fbaaa)
on `e5d9161610` passed its one uncached remote test run in 61.534 seconds. A
[forced diagnostic run](https://app.buildbuddy.io/invocation/ab7f3509-5208-492b-bd66-100ea1ab5096)
then changed only the final expected DOM cursor to 335. That original assertion
remained the failure; committed, folded, direct view, fresh Electric snapshot and
browser cursors were all actually 334. The new metadata artifact was 244 bytes:
Chromium retained 32,258 entity-stream bytes containing 40 changes and view
revisions `[281, 281, 334, 334]`. This validates capture of the open response and
its relevant revisions without saving its payload. The impossible assertion was
restored. The forced run used effective source `e5d9161610` plus the diagnostic
patch; the remote runner fetched newer base `d85ebfd828` and applied the inverse
committed difference as part of mirroring the worktree.

The [ordinary focused run with the CDP fixture enabled](https://app.buildbuddy.io/invocation/de2f1fe3-cac7-4dc9-bdbb-c24ae84b739c)
passed at code commit `0c3306453e`, with one uncached remote test execution in
61.100 seconds. That verifies the final fixture's normal lifetime separately from
the deliberately failing diagnostic probe. PR #15913's final-head CI is separate
from these focused executions.

On 2026-11-13 UTC, a [five-run repetition](https://app.buildbuddy.io/invocation/17344c27-a61c-429e-8195-3f5613e47c75)
of only `test_a_growing_thread_stays_one_shape_and_scrolling_back_keeps_the_reader_s_place`
passed on `devel` commit `93f2ba8eeff36265d6e7887836b615c39b4ad985`.
The invocation used `--noremote_accept_cached --runs_per_test=5` with a pytest `-k`
filter. The target summary records five runs and five passes; all five result
events have `strategy=remote`, and their test logs each report one passed and five
deselected tests. The five completed TestRunner execution IDs are distinct:

- `/uploads/ab1d769a-c8c5-4727-aedc-2f2822c447c7/blobs/ffb672d8278194dfd7626ea5861c57af24256fac60c0744c7c110bc86a1de338/841`
- `/uploads/7ca14dc4-a78e-47e3-84b6-162ede28b841/blobs/574f6380cfc2cc5163e0465e158e365b4aeca6d61779cc8902bfec6d15a98bd0/841`
- `/uploads/94b885c3-8bbe-42f6-ad7e-8a13ae68f3c8/blobs/0dd9377eb4a8604722edc7b919204baa04c36f63bd043d18f4cbd0671a39fef6/841`
- `/uploads/88cb195f-a187-4ac3-8bb6-579a3c27414b/blobs/da7fa2261c31d82ed2b5471d0a23825cd46b5f6fa77201d8a67a91f4c2238edb/841`
- `/uploads/d470dd5c-e472-4d4a-96a7-f8fe7d594997/blobs/8b91e5384f383220e12eda441753a10a49c4610323b22c4e0cb3f5e356fa6baf/841`

The Bazel invocation completed successfully. No projection failure occurred, so the new
failure artifacts did not fire and this sample supplies no new delivery-boundary
evidence or runtime repair.

The canonical-repository BuildBuddy target-history index queried over the preceding
168 hours returned 760 `PASSED`, 15 `FAILED_TO_BUILD`, and one `FAILED` entry; the
failure is the 2026-11-07 invocation above. This index includes cached outcomes
and excludes some PR/manual runs, and its build failures are not test verdicts.
These counts therefore do not measure the scenario's physical failure rate. The
next discriminating observation is still a naturally failing run with the direct
view, fresh Electric snapshot, Chromium entity-stream metadata, and browser cursor
captured together. Only then can we distinguish replication, stream delivery, and
client application before changing the runtime path.
