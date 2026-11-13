# Explicit resume after idle suspension

PR #16244 CI invocation [344edf77-c942-5ad2-85f0-b9558807607a](https://app.buildbuddy.io/invocation/344edf77-c942-5ad2-85f0-b9558807607a) failed the operator idle-resume Pod case. The other 20 Pod cases and all four App bridge shards passed.

The operator ResumeEnvironment RPC ran at 20:19:09.987–20:19:10.007 UTC on 2026-11-13. Its descendants contain four Kubernetes GET requests and no PATCH. The readiness wait then expired after 300 seconds, still showing the original quiesced Pod UID, observed generation 1 and suspended-at wake generation 0. The artifacts do not record the ResumeEnvironment response or its input Sandbox snapshot; they establish that this call did not advance desired state.

The scenario waited for Suspended, Idle and no diagnostic Pod. That does not prove the controller has finished stopping: Pod deletion and the controller status write are separate operations. The [Sandbox controller](../../sandbox_controller/internal/controller/controller.go) can delete the Pod in `converge` while `updateStatus` still sees its old workload and leaves the stop stage empty. The [lifecycle manager](../../runner_gateway/environments/lifecycle/manager.go) requires `Stopped` before treating an idle suspension as needing a wake; an otherwise running desired state returns ALREADY_RUNNING. This is a source-supported explanation of the observed no-write result, rather than a recorded intermediate snapshot.

The scenario now waits for the controller-owned `Stopped` stage too, and verifies that its explicit resume returns REQUESTED for the next wake generation before waiting for readiness. This preserves the existing lifecycle policy, authorization, optimistic fencing and prohibition on replaying uncertain writes.
