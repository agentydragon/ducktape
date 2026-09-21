# Agentplane conversation acceptance

This tracks the combined draft implementation against
<../agentplane/docs/thread_view_sync.md>. A passing focused invocation applies to its
recorded commit, not automatically to every later stack head. No merge, deployment,
instance reset, or disabling of raw capture is part of this work.

## Evidence and remaining work

| Requirement                                                        | Evidence                                                                                                                                                                                                                                                                                                 | Remaining work                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One ordered conversation; old items may change independently       | `test_conversation_projection`, including parallel tools and batch boundaries; pure fold and test passed at `1c5a00d57c`, [950208b4](https://app.buildbuddy.io/invocation/950208b4-f9e1-4ca4-a30c-feafd1271dd5).                                                                                         | Recheck the assembled head after final changes.                                                                                                                                                                                                                                                                                                                                                                                                                |
| Transactional entities, immutable content revisions and checkpoint | Real PostgreSQL trajectory suite and bridge suite passed at storage `7933a15d0`, [68847e7c](https://app.buildbuddy.io/invocation/68847e7c-3465-4aee-8b72-d66181c10b90).                                                                                                                                  | SIGKILL before/after commit and overlap replay passed at `055660af80`, [e2acd3ce](https://app.buildbuddy.io/invocation/e2acd3ce-bd11-41a7-96fe-39d0ec85de2d); final assembled rerun remains. This subprocess test uses the archive SSE consumer, not Electric.                                                                                                                                                                                                 |
| Tail and history queries avoid full replay                         | Real Electric/app test: 95 items, tail 30 and keyset history windows, updates inside/outside selected windows, evicted-window revisit and explicit stale-interest refresh. Latest full sync pass at `030d7214a6`, [142ceaae](https://app.buildbuddy.io/invocation/142ceaae-9a9b-4f05-b1e7-ac9821fadf3b). | Actual large-window browser retention and viewport behavior.                                                                                                                                                                                                                                                                                                                                                                                                   |
| Exact pinned revisions and independent selective bodies            | Same real sync test checks immutable old text after later updates and complete explicit selection exceeding 2 MiB; browser scenario checks omitted reasoning/tool bodies and whole selected output.                                                                                                      | Final assembled browser rerun and cache-retention measurements.                                                                                                                                                                                                                                                                                                                                                                                                |
| Streaming message text and tool input, including older items       | Built SPA/HTTP2 browser scenario passed at `745bcf34dd`, [a06221c1](https://app.buildbuddy.io/invocation/a06221c1-0b99-4de1-98fe-28d1df0ae7ec). This invocation also contains a separate failing certificate-fixture case.                                                                               | Final assembled rerun.                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Reconnect and multiple app replicas                                | Real sync test resumes replica-one handles/offsets through replica two. Browser offline/reconnect scenario passed at `9090999396`, [9deb01cd](https://app.buildbuddy.io/invocation/9deb01cd-e621-4c70-98ad-330351730650).                                                                                | Window/draft/disclosure state through reconnect and scope refresh.                                                                                                                                                                                                                                                                                                                                                                                             |
| Command recovery without duplicate submission                      | Four built-SPA lost-response/reconnect cases passed at `bd433217cd`, [ca970cb1](https://app.buildbuddy.io/invocation/ca970cb1-fb6b-45ca-a987-db9658622389).                                                                                                                                              | Rerun after replacing outcome reconciliation with a bounded command subscription.                                                                                                                                                                                                                                                                                                                                                                              |
| Settled command failures remain visible                            | Backend selected-command shape passed real two-replica sync at `030d7214a6`, [142ceaae](https://app.buildbuddy.io/invocation/142ceaae-9a9b-4f05-b1e7-ac9821fadf3b), including admission/failure in one transaction and excluded command IDs.                                                             | Failed/noop after 40 later items, reload, explicit dismissal and another reload pass at `1a18727404`, [a6027c77](https://app.buildbuddy.io/invocation/a6027c77-2f1d-4336-a97b-227e555b4386). Final assembled rerun remains.                                                                                                                                                                                                                                    |
| Fluent lazy debug access                                           | Exact associated native envelope displayed only after expansion; built-SPA case passed [242e8d66](https://app.buildbuddy.io/invocation/242e8d66-606c-4135-9c95-1717eb06a1a0). Authenticated debug API tests and PR #7555 checks passed at `808ec7b558`.                                                  | Final assembled rerun; verify disclosure eviction/revisit.                                                                                                                                                                                                                                                                                                                                                                                                     |
| Explicit rejected source/epoch handling                            | Both malformed-source browser cases passed at `72431f8916`, [66aa8fa2](https://app.buildbuddy.io/invocation/66aa8fa2-c442-4740-9995-ea1993bedd2d): retain verified prefix, show rejected versus verified cursor, disable controls. Other cases in that invocation failed on scrolling.                   | Final assembled rerun and stale-scope browser coverage.                                                                                                                                                                                                                                                                                                                                                                                                        |
| Bounded server fold working set                                    | Real record path after 100/10,000 raw-frame entries, captured actual query plans and Python allocations; storage `7933a15d0`.                                                                                                                                                                            | After the partial pending index, 100/2k/20k-item profiles passed at `2d9ef51197`, [99f4ad93](https://app.buildbuddy.io/invocation/99f4ad93-dba9-45d8-94db-50bd6b6ee848). At 2k/20k, touched-row lookups use the primary key with 3–6 buffer hits; Python peaks are 476,370/475,979 bytes. The command-heavy tail query passed at `75f92056bc`: one partial-index search and three buffer hits after 10,000 settled commands; see the query-plan section below. |
| Slow readers do not accumulate proxy buffers                       | ASGI backpressure and cancellation/receive/send disconnect tests pass at `b37065c305`, [f7531114](https://app.buildbuddy.io/invocation/f7531114-946a-46ff-ada5-ebc63842eabe); reads stop at the current chunk and upstream closes on disconnect.                                                         | Total process resource measurements are separate from this deterministic unit proof.                                                                                                                                                                                                                                                                                                                                                                           |
| Electric shape expiry and recovery                                 | Electric 1.8.1, max two shapes, three full shapes: stale handle returns 409/must-refetch and exact fresh snapshot succeeds; redacted probe [d5f734b8](https://app.buildbuddy.io/invocation/d5f734b8-ceff-4dfb-854b-4de05c9498be).                                                                        | Fixed 30-row shape through 100→10k→100k rows and two expiry/reopen cycles passed [111334db](https://app.buildbuddy.io/invocation/111334db-bc7c-46a8-82b8-ff91a7b9ffde). Selected updates transferred 471 bytes and no excluded rows; sampled RSS was 313,424→318,604 KiB. Descriptive single-run evidence; browser cache retention remains separate.                                                                                                           |
| Bounded WAL retention during Electric outage                       | Dedicated real PostgreSQL 18/Electric 1.8.1 test in draft #7560.                                                                                                                                                                                                                                         | Forced invalidation and automatic recovery passed at `b6bf06600d`, [b4be438c](https://app.buildbuddy.io/invocation/b4be438c-5bf4-4a74-af06-95a1c92404f8): healthy new slot, old handle 409/must-refetch, fresh snapshot includes outage data. The unsupported startup-probe extension was removed in `60d0cef936`; recheck the final stack.                                                                                                                    |
| Bounded runner journal/recovery                                    | Independent #7535 at `efe5195565` has green CI, bounded journal paging/checkpoint recovery and serialized SQLite access/cancellation cleanup.                                                                                                                                                            | Runner CI [b39a03b3](https://app.buildbuddy.io/invocation/b39a03b3-ba4e-575f-96d9-795a8dcde3f1) passed 43 tests. Reopen + 128-row page Python allocation peaks were 356,198/343,817 bytes for 512/32,768 stored events. This excludes native SQLite/harness memory. Separate native-process measurements are recorded in draft #7562.                                                                                                                          |
| Actual frontend cutover and single server-state owner              | Application routes use the projected TanStack DB view.                                                                                                                                                                                                                                                   | Obsolete full-history session/reducer removed in `f0a968286e`; final replacement browser/visual coverage remains.                                                                                                                                                                                                                                                                                                                                              |
| Reviewable complete PR stack                                       | Independent runner #7535 and batching #7546; pure fold #7523; storage #7540; lazy debug #7555; Electric/frontend #7537; assembled acceptance #7551; WAL #7560.                                                                                                                                           | Refresh stack bases to remove unrelated ancestry from review diffs; final current-head CI, rendered-artifact review and requirement audit.                                                                                                                                                                                                                                                                                                                     |

## Chronological debug and command evidence

At `7b77cad72a`, the debug API and projector tests plus changed library checks passed
[2ec6ca31](https://app.buildbuddy.io/invocation/2ec6ca31-962e-4616-93a5-5f4b40b06818).
The archive endpoint returns whole original observations in count-bounded,
exclusive-keyset pages, including unlinked native packets, stderr and checkpoints.
Every settled command associates its outcome with the stable admission entity;
effects may also associate with a lifecycle/input entity. The tests cover failed,
noop and effect settlements, batch partitions, authentication, cursor precision,
forward/backward paging and an untruncated stderr body over 2 MiB.

The on-demand frontend drawer is implemented at `ee46509b9d`; it keeps one raw page,
mounts JSON only for expanded observations, aborts page requests on disposal, and
links from semantic evidence to its original chronological context. Real desktop
and phone browser cases passed at `647d5ed476`
[5163ace0](https://app.buildbuddy.io/invocation/5163ace0-52d0-4115-9be7-055f00b2c347):
no archive request before opening, exact original records, bounded replacement
pages, backward/forward/latest/context navigation, draft and evidence-disclosure
preservation, and cancellation when closing during an in-flight real response.
Desktop and phone PNGs from the preceding successful paging run at `1265c9a818`
[2271c3b1](https://app.buildbuddy.io/invocation/2271c3b1-cafb-444d-afba-9d8eef986b93)
were downloaded and inspected; the drawer and its close control fit both viewports.
After consolidating debug request ownership, the same browser cases and all 115
frontend unit tests passed at `3aa75798db`
[382c516a](https://app.buildbuddy.io/invocation/382c516a-2442-47db-8d3b-4128d206b0ac).
The latter include exact before/after query serialization above JavaScript's integer
precision limit.

At `478f3352f0`, evidence and nested raw-frame disclosures use the shared bounded
disclosure store. Their loaded pages are keyed by source, projection epoch, entity
and observation so a replacement scope cannot reuse a mounted page from the old
scope. The real browser streaming/lazy-content scenario and all 116 frontend unit
tests passed [e900bca3](https://app.buildbuddy.io/invocation/e900bca3-8dde-46b0-80b2-6b632f895274).
The browser verifies closing evidence removes its raw body and reopening restores
the selected original frame; its PNG was downloaded and inspected. The unit test
checks eviction after 129 disclosure choices and isolation of a replacement source.
Actual history eviction/revisit and source-reset browser coverage remain separate.

## Navigation between threads

At `406735fec3`, the real browser navigated between two stored 80-item conversations
after selecting older history in the first:
[cf04b70e](https://app.buildbuddy.io/invocation/cf04b70e-ae88-470f-8f85-c19498aec244).
Both route transitions requested a fresh tail without the previous thread's
`before_cursor`, displayed the correct name and rendered its newest item. The view
is keyed by thread ID; older-page selection and asynchronous header state cannot
carry into another thread. The resulting PNG was downloaded and inspected.

The first run's five-second body assertion expired during initial shape creation;
its trace shows successful payload responses and the expected rendered tail just
afterward. The test now waits for the selected metadata row before awaiting its
lazy body. This test verifies navigation state isolation, not a cold-load latency
target.

## Native harness measurements

Independent draft #7562 adds a controlled-upstream experiment above runner #7535.
All six real Claude/Codex cases (1/10/100 prior turns, followed by native restart)
passed at `813573b1c0`
[4edd2d8a](https://app.buildbuddy.io/invocation/4edd2d8a-306c-41f6-919c-11889949e058).
Exact prior context reached the model again. Conversation text in the resumed request
grew from 6194 to 415345 bytes for both harnesses. At 100 turns, sampled native RSS
was 263340 KiB for Claude and 127752 KiB for Codex before restart; these finite samples
do not establish constant native memory or long-run latency.

The native persistence measurements exclude symlink targets. At 100 turns, Claude's
native directory held 876141 logical bytes and Codex's held 97415342 bytes, including
78972792 bytes across 5416 `.tmp` files. Codex's eventual cleanup policy was not
established. These are native execution/retention limits separate from Agentplane's
bounded journal and conversation projection. Full artifacts and scope limitations
are documented in #7562's `debug/agentplane_native_resources.md`.

## Tail queries after settled commands

At `75f92056bc`, the production tail-bound query was exercised after 10,000 settled
commands through actual ingestion:
[dfc9c92b](https://app.buildbuddy.io/invocation/dfc9c92b-521e-4cf3-8536-f47d2a8f7c94).
The downloaded EXPLAIN artifact shows a backward scan of the partial segment cursor
index: one index search, three shared-buffer hits, and no row filtering. This closes
the specific risk of scanning command rows to find the last conversation segment.
The full 33-test trajectory suite subsequently passed at `1f259b30c2`
[c897a7ca](https://app.buildbuddy.io/invocation/c897a7ca-fe1c-4909-a23c-3b8b82cfdfc8).

## Authenticated proxy boundary

At `85db5c8798`, all five production sync routes passed missing, incorrect, and
forged-header credential checks before any Electric request:
[aafbcdf0](https://app.buildbuddy.io/invocation/aafbcdf0-ce16-4b49-933f-03c0c280ed96).
The test creates the actual application with an Electric proxy whose upstream
transport fails if invoked.

## Latest browser regression results

At `78336e3879`, all five focused cases passed
[3e7e5fb7](https://app.buildbuddy.io/invocation/3e7e5fb7-3ddd-48f3-b502-59f6d431a62f):

- Desktop follow-bottom, preserving the visible reading anchor, and returning to
  the bottom while content expands before a queued scroll event.
- Failed-turn phone layout with exact lazy native-frame disclosure.
- Streamed admission after a lost HTTP reply, including reload.
- Admission hidden from synchronization while reloading.
- Same-document Electric reconnect with an unconfirmed command.

The full 21-case real-service browser suite runs across four shards at `ab52b83f84`,
[2017cf5c](https://app.buildbuddy.io/invocation/2017cf5c-0af1-4d58-8d32-c3172f616945);
it finished with 19 passed and two scroll failures (desktop-normal after expansion
above the reader, phone-raw after new output). The failures persisted after a focused
pass, so scroll acceptance remains open. A follow-up adds failure-only visible-row
geometry artifacts and the frontend worker is correcting competing scroll anchoring.
At `5e059313e8`, the follow-up four-variant scroll run
[60123eec](https://app.buildbuddy.io/invocation/60123eec-ea8b-4ea2-aaee-391b49d1c839)
failed all four variants after new output while reading. Geometry artifacts show the
expected cursor 38 moved out of view in three cases, and by about 600 pixels in the
fourth. The latest anchoring changes therefore do not establish a working solution.

The larger history-window and scope-refresh probe remains separate. An empty initial snapshot in that probe was traced to fixture precedence:
the app used conftest's database while Electric used another database. It does not
establish a product cold-start defect.

At `ad25ece551`, scroll run
[dec9e254](https://app.buildbuddy.io/invocation/dec9e254-2862-4e65-b352-77b849f1a292)
passed desktop-normal but failed desktop-raw and both phone variants at the first
reading-anchor check. Disabling competing virtualizer size adjustments alone does not
resolve the drift. The worker is tracing saved anchors against actual DOM geometry.

At `ba15d83115`, the input-intent capture candidate (`a1556e1f7b` plus `d1dc7ce03b`)
failed all four variants at that same first reading-anchor boundary:
[564517b4](https://app.buildbuddy.io/invocation/564517b4-45d0-492e-b2df-53424e01f1b9).
All expected rows remained mounted. Desktop cursor 36 moved from offsets -116.59/-42
to about -303; phone cursor 38 moved from -56.47/-292.875 to -341.875. This is anchor
drift rather than eviction and requires an instrumented trace of capture/restoration;
the input-intent heuristic is not accepted as a working solution.

Screenshots of expanded/reloaded/reconnected content, exact native-frame disclosure,
terminal failed/noop cards, failed-turn desktop/phone layouts, and resumed scrolling
have been downloaded and inspected. The intentionally expanded message in the scroll
fixture retains its synthetic height. Additional visual changes require fresh evidence.

## WAL recovery fixture correction

At `4fafc4b429`, the fixture's changed library checks and the real WAL-recovery test
passed [a4c53347](https://app.buildbuddy.io/invocation/a4c53347-b6a7-4cee-a28b-c8a5ede30de9).
This also corrects the readiness URL callable and validates Docker's bytes log result,
both caught by full PR CI. The WAL PR still needs the final frontend acceptance parent.

Earlier recovery runs continued probing the host port assigned before Docker restarted
the Electric container. Captured Docker state at `519c665e6b` showed a healthy service
and active PostgreSQL replication on port 32771, while the fixture still used 32770.
The corrected fixture resolves the published port again and reopens its HTTP client.
At `b6bf06600d`, recovery passed with persisted Electric storage, a forced lost slot,
and data committed during the outage. The old stream handle returned
`409` with `must-refetch`; a new snapshot included that data. No manual storage reset
was needed. This establishes the service/protocol recovery path; browser subscription
recovery and final deployment configuration are verified separately.
