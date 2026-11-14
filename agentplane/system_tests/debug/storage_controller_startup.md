# Storage case races a recovering controller

Required CI for source `58c274be30` tested merge `35eb3cce00`, including devel
`6dfbd741f5`. [Invocation e0cdcb09](https://app.buildbuddy.io/invocation/e0cdcb09-e0ad-5a46-88aa-465f85deb0a7)
failed the Pod suite before storage work: its initial `readyReplicas == 1`
assertion observed an unavailable controller. The other 20 cases were not run
after this exclusive case failed; they are not 20 independent regressions.

Private fixture artifacts show the controller's lease PUT and GET timing out,
then exit 1 at 23:42:47Z, restart at 23:42:48Z and Ready at 23:42:58Z. It was
not OOM-killed. No previous controller stderr was captured at that boundary,
so the reason for exit remains unknown. A separate MCP Actions snapshot TSC
restore error recovered through its automatic infrastructure retry.

The candidate waits at most 30 seconds for the same readiness predicate, without
raising any storage-operation or suite timeout. It captures controller status
and previous logs on restarts, even if recovery succeeds. The storage scenario's
file contents, incompatible-claim refusal, suspend/resume, volume identities and
cleanup assertions remain unchanged.

The manual `//agentplane/system_tests/suites:test_storage_startup` control scales
the real controller to zero and confirms the old predicate is false. One case
keeps it unavailable and requires the bounded wait to fail. The other starts the
unchanged storage scenario, restores the controller after that scenario enters
its readiness wait and requires every probe and cleanup to pass. Results are
pending; this is a proposed test-race repair, not a production controller fix or
a measured fleet reliability improvement.

Follow up in [#16099](https://github.com/agentydragon/ducktape/issues/16099): inspect
previous stderr on any recurring controller restart, determine whether leader
lease renewal caused exit, and retain failed physical attempts in reliability
measurements. A successful readiness wait must not be counted as a crash repair.
