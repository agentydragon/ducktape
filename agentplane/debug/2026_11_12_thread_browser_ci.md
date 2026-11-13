# Thread browser CI failures (12 November 2026)

Two failure classes in `//agentplane/app:test_thread_browser` need separate follow-up.

## Projection and raw-frame assertions

The failed PR #15443 shard 13 trace ends with `data-projection-cursor="43"` and a
`Test tail seed` paragraph in the thread history. Its `get_by_text(...).to_have_count(1)`
assertion had already exhausted its five-second default. The test appended 39 events
after its initial projection without waiting for the final cursor. This trace shows
late projection/rendering at the failed assertion; it does not establish why those
events took that long. The current test now waits for the appended event's projection
cursor before checking the paragraph.

In PR #15570 shard 7, the browser requested `native-frames` for observation 5 and
received HTTP 200 with the expected `test-text-delta` `EventEntry` in about 25 ms.
The final Playwright snapshot includes that text in `.ui-code-block`, but the
five-second locator assertion had found no code block. `CodeBlock` uses an
`IntersectionObserver` and shows a placeholder until the editor nears the viewport.
The current test waits for the frame surface and scrolls its stable observation row
into view before asserting and parsing the frame. The trace does not prove whether
the delayed editor was solely due to viewport position.

These changes retain the checks that a runner event reaches the browser and that
the displayed raw frame equals the stored native event. A focused RBE pass is only
a candidate repair until recurrence under comparable CI load is observed.

## OIDC setup timeout remains open

PRs #15534 (shard 14) and #15718 (shard 14) each failed before the browser test
body, in `app_process()` while `AppProcess.authenticate()` awaited the response
headers for `/auth/login`. `httpx.ReadTimeout` identifies the client wait, not
which server or OIDC exchange step stalled. The #15534 app logs show database
migrations completed but no request-level record; there is no browser trace because
the page fixture had not started. These failures are not covered by the projection
and raw-frame assertion changes.

The next discriminating observation is a request ID spanning the test client's
`/auth/login` call, the Go app's login handler and OIDC provider endpoints, with
phase timestamps and the server response or disconnect. Capture it in the failing
RBE shard's undeclared outputs; then determine whether the stall is before app
handler admission, inside the OIDC exchange, or while returning headers. Do not
change the HTTP timeout or add a blind retry without that evidence.
