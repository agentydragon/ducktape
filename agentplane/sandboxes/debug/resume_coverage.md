# Resume coverage audit

Audited 2026-10-14 PDT against the merged retained-state implementation. The bounded audit asks
whether concurrent resume, interrupted setup, and incompatible retained storage have deterministic
coverage; it does not require repeating the accepted live VM Codex recall scenario.

| Contract                                                                  | Coverage                                                                                                                                                                                                                             |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Concurrent explicit or idle-suspended resumes publish one wake generation | `sandboxes/test_inventory_mutations.py::test_concurrent_resumes_increment_the_wake_generation_once` holds both requests after their reads, forces the resourceVersion collision, and checks one successful patch with unchanged UID. |
| Resume rechecks quiescing after a concurrent controller write             | `sandboxes/test_inventory_mutations.py::test_resume_rechecks_quiescing_after_a_resource_version_conflict` rejects the retry without a second patch.                                                                                  |
| Concurrent queued commands share one Sandbox wake                         | `session_dispatcher/test_delivery.py::test_concurrent_queued_sends_share_one_sandbox_resume` checks one generation, one budget record, and delivery of both commands after the replacement incarnation is Ready.                     |
| Interrupted setup never reruns or starts a harness                        | `runner/test_restart.py::test_runner_shutdown_interrupts_setup_without_rerunning_it` restarts the runner, observes the retained interrupted state, and checks one setup-start record.                                                |
| Dormant sessions remain dormant until selected; history survives resume   | `runner/test_restart.py::test_runner_startup_keeps_unselected_sessions_dormant_and_resume_keeps_history`.                                                                                                                            |
| Changed state layout refuses resume without replacing data                | `sandbox_controller/state_volume_test.go::TestStateVolumeMismatchRetainsPVCAndRefusesResume` retains the same PVC identity and record and starts no Pod.                                                                             |
| Changed workspace layout refuses resume without replacing data            | `sandbox_controller/workspace_test.go::TestWorkspaceChangeRefusesResumeAndRetainsClaimAndHaltedVM` retains the claim and halted VM.                                                                                                  |

The only added test is the direct two-request resourceVersion collision, for both explicit and idle
suspension. The existing queued-send test does not force that exact interleaving. This is a coverage
review, not a claim that every listed suite was rerun for this documentation change; the new test
must pass the PR's Bazel checks before merge.

Live same-state-PVC VM Codex resume, changed workload UID, stable Session identity, journal
continuation and native recall were already accepted in
[storage acceptance](../../acceptance/debug/storage_acceptance.md). The later guest disk-preparation
change has its own fresh-format/remount acceptance. Long-stop credentials, app presentation,
ambiguous delivery across incarnations, and Dispatcher activation remain separate DAG outcomes.

Merged #10574 adds `session_dispatcher/test_delivery.py` coverage joining the real Dispatcher,
PostgreSQL, and two runner incarnations across a lost admission reply. The original command ID,
payload and durable admission cursor survive; only one `CommandAdmitted` is journaled. This is
admission deduplication, not exactly-once external effects: an input whose native acceptance was
never confirmed can replay after replacement.
