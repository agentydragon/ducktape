# Grader supervisor test timeout on PR #12262 (2026-10-27)

`//props/orchestration:test_grader_supervisor` timed out in GitHub Actions run
`40573772348`, attempts 1 and 2, against merge commit
`23b49199fb90ed7c52daaeb908962a321a842eaa`. The test source was unchanged
by the PR. The two attempts have distinct failure paths; neither implicates
grader reconciliation.

## Attempt 1: workspace delay and import budget

BuildBuddy invocation `5ee65889-282f-598e-b84a-8d4e5c11e16a` first had a
Firecracker startup failure (`/uploads/2ca84397-b2cf-46c5-9134-7c2b0c7a2d3b/blobs/d7ea69a29a6b244cba2a34b382b51e5f9dd59d88ede53036e599214a8eb2a0f6/498`):
the VM API socket did not appear before its deadline. A later execution of the
same action, `/uploads/fa502e18-a1fa-4164-93df-335fd2545d34/blobs/d7ea69a29a6b244cba2a34b382b51e5f9dd59d88ede53036e599214a8eb2a0f6/498`,
ran from 20:07:31.001 to 20:08:34.647 UTC and hit its execution deadline.
Its VM log (`c760a2c397da4cd660e1de55fc84dbdec5ebc8d1012dd88b33bd42ee447cb27d/8210`)
shows snapshot resume at 20:07:31.637 and the first
`PATCH /drives/workspacefs` at 20:07:49.122: **17.485 seconds passed before
the workspace was attached**. Pytest started at 20:08:01.413. Its XML records
all 19 cases passing in 25.917 seconds; each case took at most 0.255 seconds,
and the last test span ended at 20:08:27.329. The action remained alive until
the 20:08:34.647 deadline. It consumed 42.67 CPU seconds and about 710 MB peak.
The log/XML artifacts are `6522e4876fdf10df471c2d5ea7e70c1125ca194e0bb7002125d7bf222b136c59/2134`
and `46c8ddf23e4ab37fd570913f95ab41d06d99221a7d75ff4faaf47ed91a7b93ad/2704`.

A separate later execution of the same action in this invocation,
`/uploads/35aa68cf-89c9-4ec8-b2ec-06731fad31f5/blobs/d7ea69a29a6b244cba2a34b382b51e5f9dd59d88ede53036e599214a8eb2a0f6/498`,
timed out from 20:14:09.486 to 20:15:15.467. Its VM log
(`47d4b613538c6f05a231b75062fefa329cc7c25cc5dbff4ad067bdc5a07f44df/7827`)
shows resume at 20:14:15.224 and workspace attachment at 20:14:33.674,
an 18.450-second gap. The earlier all-pass XML does **not** prove this later
execution reached or completed pytest.

## Attempt 2: no workspace attachment

BuildBuddy invocation `00aab7f1-5818-5789-beb6-2b0538e6bf1b` executed the
same test action as
`/uploads/7dca444c-1c0c-4964-b0f0-8e1e7ab86ffb/blobs/d7ea69a29a6b244cba2a34b382b51e5f9dd59d88ede53036e599214a8eb2a0f6/498`.
It ran from 20:19:02.929 to the deadline at 20:20:03.213. Its `test.log` is
empty and the test XML is only a Bazel timeout wrapper. The VM log
(`18a7b8a92ec13ff292271c2f83800bfcf1ae8dc8f3a6c5ec888d65fc737561f9/4718`)
shows snapshot resume at 20:19:04.351, then **no**
`PATCH /drives/workspacefs` through the deadline. The last guest kernel lines
report `vdb` capacity changing to zero. This action never received the test
workspace, so Python startup and the test could not begin. BuildBuddy omitted
CPU/memory/PSI data for this action; no application stack is available.
The executor was `sjc-prod-r2204m05vm4kwn`, runner
`04649b79-0f3d-4242-a1ae-1368b4dd4847`, VM
`708c78df-dc09-4207-b1d8-9058876ed6c0`, using snapshot
`b649d525-ce52-4d3d-8501-6fcc85876e32`.

This matches the Firecracker workspace attachment symptom previously recorded
in [the July/October BuildBuddy investigation](../../../devinfra/debug/buildbuddy_firecracker_workspace_attach_stalls_2026_07.md).
BuildBuddy's current public source (commit
[`a04caa7`](https://github.com/buildbuddy-io/buildbuddy/commit/a04caa741548187c77dc991c8d8384c6788e24a9))
calls `TaskFinished` and `TreeFetcher.Wait()` at task end; `TransferDuration`
can therefore include the action's lifetime. The 60–69 seconds reported as
`fileDownloadDurationUsec` here **cannot** independently establish 60–69
seconds of blocked file downloads. The deployed BuildBuddy version is unknown.

The client-visible trace ends between Firecracker resume and drive attachment.
Current public `createAndAttachWorkspace` builds the workspace image, waits for
guest exec readiness, then calls `UpdateGuestDrive`; these records do not
separate workspace-image construction, CAS/storage fetch, guest readiness, or
executor contention. A BuildBuddy escalation should correlate the action and
worker IDs above with host-side timings for those three steps and with CAS
latency, then compare the 17–18-second attachments and the no-attachment
attempt against a passing execution of the same action. No external report was
sent during this investigation.

## Repository-side improvement

The fixture import profile shows that `props/conftest.py` eagerly imported e2e
support for this unit suite even though it uses none of its fixtures. Registering
those fixtures in `props/agents/conftest.py` removes that import burden from
orchestration tests. The four fixtures in `props/testing/fixtures/e2e.py` had
no consumers and were removed. This reduces normal startup cost. It cannot
repair the attempt-2 failure before workspace attachment.
