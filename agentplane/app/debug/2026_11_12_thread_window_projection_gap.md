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

## Natural recurrence with direct reads, 2026-11-13 UTC

The [01:59 UTC mainline invocation](https://app.buildbuddy.io/invocation/c210960a-e5ab-5a68-9ab2-cf847b5b0052)
on source `0a13d8cabfa551075af470e6660200708643a7bf` reproduced the same
cursor-334 assertion. Its target reports one failed, five passed cases. The
retained diagnostic now establishes committed 334, folded 334, direct view 334,
and fresh Electric subset cursor 334, while the DOM remains 264. Thus the append,
folded row and fresh Electric read completed in this occurrence. They do not
prove what the existing live request delivered.

The CDP artifact contains zero buffered bytes for response `7258.94`, handle
`13800933-1794535177397698`, offset `34614888_124`. This was the last response
that received SSE headers, **not the last attempted request**: the trace shows
it opened at monotonic 22169.453 ms, before pagination paused it. A later entity
GET completed with only an up-to-date control at 22907.444 ms. The final live
entity GET began at 22937.268 ms and received no headers before the assertion's
30-second deadline. An empty older response is therefore not proof that the
server sent an empty current response or that the client discarded cursor 334.

The final request began while the fixture removed its temporary request route:
`setNetworkInterceptionPatterns([])` started at 22934.970 ms. That ordering makes
browser interception/request admission a testable candidate, alongside server
request admission and streaming. It is not a demonstrated Playwright defect.
Completed POST and non-live GET responses negotiated HTTP/2 with TLS/server-port
metadata. Pending/SSE trace entries instead say HTTP/1.1 without TLS/server-port
metadata; these incomplete entries do not establish a negotiated protocol or
six-socket admission limit. The final request's send/wait/receive timings are
all -1, so it does not prove on-wire or upstream admission. The fast-retry warning
again belongs to a payload shape, not an observed entity-stream reset.

The advertised ingress log describes port 45213 and an earlier shape handle;
the failed page uses port 34817. Multiple fixtures wrote the same
`conversation-http2-ingress/container.log` path, so that file cannot determine
whether the failed page's final GET reached nginx, the Go app or Electric. The
fixture now retains each ingress and static-frontend log under a unique
`<role>-<port>-<uuid>/container.log` path. The port joins it to the browser trace;
the UUID prevents collisions even if the operating system later reuses a port.
This repairs evidence retention without changing request routing or delivery.

A [five-run focused repetition](https://app.buildbuddy.io/invocation/007cd06a-44d3-4f33-814a-07f44a3796c8)
on effective source `4415ac2544` passed five fresh remote runs (zero cached),
with five distinct TestRunner execution IDs. The unchanged real streaming
scenario and its one-shape, reading-anchor and draft assertions all remained.
A [focused retention validation](https://app.buildbuddy.io/invocation/72b0f431-9644-4130-93da-3e7e2b422be6)
on the same source plus this fixture change passed one fresh remote run in
50.415 s and published
`conversation-http2-ingress-43521-c51d455f3e484513b25136e4d3a5e2b8/container.log`.
No cursor failure occurred in these six physical attempts; neither is a delivery
repair or a post-fix recurrence measurement.

[Issue #16033](https://github.com/agentydragon/ducktape/issues/16033) remains open.
At the next natural recurrence, join the final attempted entity GET to its
port-specific ingress log before choosing a repair. If admitted upstream,
compare app/session admission and Electric delivery; if absent, retain Chromium
request timing/interception evidence. Review the next seven days of target
outcomes with cache and physical-execution counts kept separate. No scheduled
monitor or failure-rate reduction is claimed.

## Keep interception active across the resumed entity request

The fresh failure's final entity GET begins 2.298 ms after removal of the
page's final route. Repository history contains an independently observed instance
of this exact failure shape: [PR #10094](https://github.com/agentydragon/ducktape/pull/10094),
commit `667ebf6c2d4`, retained one-shot routes because a request beginning 3 ms after
Chromium `Fetch.disable` stayed paused forever and never reached nginx or the app.
The existing `test_thread_browser.route_once` documents and implements that
lifetime rule. That earlier investigation is historical evidence, not a new
measurement of this cursor failure.

`_holding_older_pages` violated that rule by unregistering its final route as
soon as pagination finished, exactly when Electric resumed its live GET. It now
releases the hold and leaves the route registered until page cleanup. Later
requests continue to the actual network immediately. This keeps the real
Electric stream, reconnect behavior, one-shape assertion, reading position,
draft and cursor334 assertion. No timeout, retry, server or browser dependency
changes. This is a candidate repair for the fresh failure's interception race;
post-change passes alone cannot prove the historical cursor failure is eliminated.

The [ingress retention PR #16045](https://github.com/agentydragon/ducktape/pull/16045)
records the direct334/freshElectric334/DOM264 boundary and preserves each fixture's
logs for the next recurrence. It is independent of this route-lifetime change.
[Issue #16033](https://github.com/agentydragon/ducktape/issues/16033) stays open for
seven days of post-change physical exposure and the next natural failure's final
request/proxy comparison. In particular, an arriving cursor334 update with an
unchanged DOM would reject this candidate as a complete explanation.

The initial [ten-run candidate](https://app.buildbuddy.io/invocation/bda85d87-6eda-4425-aa29-f1251167693e)
passed the cursor334, anchor and draft assertions in all ten attempts; nine
whole cases passed. The remaining case subsequently failed when `_older_page`
attempted UTF-8 decoding of a binary OTLP POST to `/telemetry/v1/traces` while
counting all recorded requests. The helper now checks the entity endpoint before
reading `post_data`. A
[focused regression](https://app.buildbuddy.io/invocation/812209ce-3a6d-467b-a3d7-70c17adab038)
proves a telemetry POST's decoding property is never read while a JSON entity
pagination POST still matches. The
[final ten-run real streaming candidate](https://app.buildbuddy.io/invocation/a853f6ab-0978-426d-bdd2-7a35a97abe56)
passed ten remote runs with zero cached results and ten distinct TestRunner IDs.
These use `4415ac2544` plus the scoped test changes. They validate the candidate
under ten fresh schedules, not elimination of a rarely recurring natural fault.
