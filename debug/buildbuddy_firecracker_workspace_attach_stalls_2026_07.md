# BuildBuddy Firecracker workspace-attach stalls (July 2026)

## Status

Open infrastructure anomaly. The immediate Calendar test exposure was removed by
making its router tests independent of the full console/Postgres fixture; do not
raise the test timeout as a mitigation.

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
successful execution of the same action around workspace image creation/fetch
and the drive-attach call. Those server/executor details are beyond the
visibility exposed by the execution record, so more client-side test logging
would not answer the remaining question.
