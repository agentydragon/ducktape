# BuildBuddy Firecracker workspace-attach stalls (July and October 2026)

## Status

Open infrastructure anomaly, seen again on 2026-10-20 and 2026-10-27 (see below). The immediate
Calendar test exposure was removed by making its router tests independent of the full console/Postgres
fixture; do not raise the test timeout as a mitigation, and a re-run only hides it.

## Symptom and scope

`//haku/console/tools:test_google_calendar` normally took about 33 seconds with
the old full-console fixture. Two remote TestRunner actions instead spent most
of their 60-second deadlines between resuming a recycled Firecracker VM and
attaching its `workspacefs` drive. The test then received too little runtime and
timed out.

A history scan covering June 15 through July 15 found 198 unique TestRunner
executions: 194 succeeded and 4 timed out. The VM logs prove the workspace-drive
stall for 2 of the 4 timeouts; the other 2 were not attributed by this
investigation.

## Reproducible evidence

### July 14 timeout

- Invocation: `2e1882dd-5914-42da-9cfe-6bcb4c55cc33`
- Action: `44fd2de5cb12635b01575dba7d6ac85c337cb36a67288176a32968caa5f318d7/489`
- Execution: `/uploads/7d4532d0-c5f4-4b3c-b3fd-4877fe6e57f2/blobs/44fd2de5cb12635b01575dba7d6ac85c337cb36a67288176a32968caa5f318d7/489`
- VM log CAS: `94cba22cf25e740cccd8d92f3ae36f9a0b07ab05469f6e8fb00fd04f43900097/7802`
- Remote-execution queue (`queuedTimestamp` to `workerStartTimestamp`):
  65 ms (`14:04:16.659633Z` to `14:04:16.724948Z`)
- Firecracker finished the resume request at `14:04:18.821715Z`, but did not
  receive `PATCH /drives/workspacefs` until `14:05:13.871077Z`: a 55.05-second
  post-resume gap.

### July 15 timeout

- Invocation: `b7a2cca6-3dda-4684-a9e5-547bac160b30`
- Action: `cb8fd95562b5754a0ab09618b0c6bb5e4aded6f41483381f2e30893ad253425a/489`
- Execution: `/uploads/74a3293e-7a1e-4bb5-b4b1-a4a7353b8cf5/blobs/cb8fd95562b5754a0ab09618b0c6bb5e4aded6f41483381f2e30893ad253425a/489`
- VM log CAS: `bc441193ff756a394daa474c16dc01c7f8a58524d61c90da393ca27b84e65d62/5678`
- Remote-execution queue (`queuedTimestamp` to `workerStartTimestamp`):
  61 ms (`02:37:52.057256Z` to `02:37:52.118622Z`)
- Firecracker finished the resume request at `02:37:58.885039Z`, but did not
  receive `PATCH /drives/workspacefs` until `02:38:41.445826Z`: a 42.56-second
  post-resume gap.

The same Calendar action later passed when the equivalent resume-to-workspace
attachment interval was about 3.2 seconds. That contrast, plus the action
content being unchanged, argues against a deterministic application deadlock.

## October 20 recurrence

A `small` test that normally runs in 5 to 8 seconds, `//cluster/validation:test_mailbox_smtp_ingress`
(it reads two files), timed out at 60 s with an empty `test.log` on a PR that touches nothing it
reads. The same action passed on the re-run, so nothing in the repository differs between the two.

- Invocation: `988a5a1c-f017-5655-b803-5d6f1acd89fe`
- Execution: `/uploads/2b36de3b-73ad-486b-9b43-0d76d57df336/blobs/e880b12b44b635ac4c2836f3247632e81a55c3eddd755f0c497e12b3e5c4b1cf/498`
  on executor `sjc-prod-r2206m08vm4onh`, VM `fae01fa6-72ef-4d34-af21-30d45fd571c4` (snapshot resume,
  runner task number 1)
- VM log CAS: `19b22834c04ead053c969c3b11f18f7edabd62b9710b971fdefa20ae667da042/4713`
- Queue: 59 ms (`04:20:51.260Z` to `04:20:51.319Z`). Execution ran `04:20:55.32Z` to `04:21:55.33Z`
  (60.004 s), then `deadline exceeded`.
- Firecracker resumed the vCPUs at `04:20:57.56Z` and logged no further API request, in particular
  no `PATCH /drives/workspacefs`, through the kill 58 s later. The guest's last kernel line is
  "vdb: detected capacity change from 3914640 to 0".
- `fileDownloadDurationUsec` is 60.7 s for 4 files and 880 KB.

Unlike July this was not one VM. Of the 54 executions in the invocation, 40 recorded at least 20 s
of file download (up to 111 s), most of them started within the same two seconds
(`04:20:50Z` to `04:20:52Z`), and many downloaded no files at all. Actions with longer deadlines
absorbed the delay (mypy and `tsc` actions took 83 to 133 s); this test's 60 s deadline is
shorter than the stall.

Nothing here is fixable from the repository: the test is already minimal and the data it declares
is not what stalls. The remaining step is the one under Next diagnostic.

## October 27 recurrence

`//props/orchestration:test_grader_supervisor` timed out in both attempts of GitHub run `40573772348` on PR #12262,
which did not change the test. Neither failure implicates grader reconciliation.

- Attempt 1, invocation `5ee65889-282f-598e-b84a-8d4e5c11e16a`, late attachment: executions
  `fa502e18-a1fa-4164-93df-335fd2545d34` and `35aa68cf-89c9-4ec8-b2ec-06731fad31f5` got `PATCH /drives/workspacefs`
  17.485 s and 18.450 s after resume. The first ran `20:07:31.001Z` to `20:08:34.647Z` and returned
  `deadline exceeded`: resume `20:07:31.637Z`, attach `20:07:49.122Z`, pytest start `20:08:01.413Z`, all 19 cases passed
  in 25.917 s (none over 0.255 s), last test ended `20:08:27.329Z`. Its nominal 60 s budget ended about
  `20:08:31.001Z`, and the executor finalization after it cannot all be attributed to Python shutdown. It used
  42.67 CPU s and about 710 MB peak. Whether the second reached pytest is unknown. Before them, execution
  `2ca84397-b2cf-46c5-9134-7c2b0c7a2d3b` failed to start: Firecracker did not create its API socket before its
  deadline.
- Attempt 2, invocation `00aab7f1-5818-5789-beb6-2b0538e6bf1b`, no attachment: execution
  `7dca444c-1c0c-4964-b0f0-8e1e7ab86ffb` resumed at `20:19:04.351Z` and got no `PATCH /drives/workspacefs` through its
  deadline at `20:20:03.213Z`. Its `test.log` is empty, the last guest kernel lines report `vdb` capacity changing to
  zero, and BuildBuddy omitted CPU, memory and PSI data, so no application stack exists. Executor
  `sjc-prod-r2204m05vm4kwn`, runner `04649b79-0f3d-4242-a1ae-1368b4dd4847`, VM `708c78df-dc09-4207-b1d8-9058876ed6c0`,
  snapshot `b649d525-ce52-4d3d-8501-6fcc85876e32`.

In BuildBuddy's public source at commit
[`a04caa7`](https://github.com/buildbuddy-io/buildbuddy/commit/a04caa741548187c77dc991c8d8384c6788e24a9) (the deployed
version is unknown), `createAndAttachWorkspace` builds the workspace image, waits for guest exec readiness, then calls
`UpdateGuestDrive`. `TaskFinished` calls `TreeFetcher.Wait()` at task end, so `TransferDuration` can include the
action's lifetime; the 60 to 69 s `fileDownloadDurationUsec` of these executions, like the figures in the other
sections, therefore cannot by itself establish blocked file downloads.

Keeping the root `props/conftest.py` to configuration and tracing (<props/testing/AGENTS.md>) trims this test's startup
but cannot repair a failure before the workspace attaches.

## Runner-queue hypothesis

The data does not support ordinary waiting in BuildBuddy's runner queue as the
source of the 42--55 second delays. In both failures, BuildBuddy assigned a
worker about 60 ms after recording the queued timestamp. The executor then
started Firecracker, loaded the snapshot, and resumed the vCPUs. The long silent
interval is inside that already-assigned execution, after VM resume and before
the executor asks Firecracker to attach `workspacefs`.

A follow-up `bbr test //haku/console/tools:test_gmail` did print `Waiting for
available remote runner...` before outer invocation
`7220a137-bed8-43c4-a93c-4815a147d332` began its runner logs. That confirms the
outer remote-runner pool can queue work. It happens before repository sync and
before the inner Bazel invocation (`f562b4aa-88e8-4c43-ba47-e26d1583a5cf`), so
it does not consume a TestRunner action's 60-second execution deadline and is
separate from the post-resume gaps recorded above.

There is some additional worker-side setup before the first Firecracker log
(about 2.1 seconds on July 14 and 6.7 seconds on July 15), but it is also too
small to explain the timeout. BuildBuddy's client-visible metadata does not
break the post-resume interval into workspace-image construction, CAS/storage
fetch, local executor contention, or another internal runner operation, so this
investigation cannot identify which of those is the root cause. The unusually
large `fileDownloadDurationUsec` values (62.0 and 72.4 seconds) are consistent
with the delay being accounted to input/workspace preparation, but are not
sufficient to distinguish those mechanisms.

## Commands used

```bash
bbapi execution <invocation-id>
bb execution get <execution-id> --output=json
bb download <vm-log-cas-digest>
```

The execution record supplies queue and worker timestamps; the VM log supplies
the Firecracker resume and `PATCH /drives/workspacefs` timestamps.

## Next diagnostic

If this recurs, preserve the action execution ID and `vm_log_tail.txt`, then ask
BuildBuddy to correlate the gap with executor-side workspacefs/CAS metrics for
the recorded executor host and runner ID. In particular, compare a stalled and
successful execution of the same action around workspace image creation/fetch,
guest exec readiness and the drive-attach call. Those server/executor details are beyond the
visibility exposed by the execution record, so more client-side test logging
would not answer the remaining question.
