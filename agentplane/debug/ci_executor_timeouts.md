# Agentplane CI timeouts: unresolved executor signal

Observed 2026-10-15. The cause of the reported BuildBuddy timeouts is not fully
established. Across #10560–#10562, multiple action kinds had RBE code-14 or
deadline failures. Several Firecracker TestRunner logs showed a resumed VM with
no `PATCH /drives/workspacefs` before the action deadline. In a completed
comparison for the inventory target, the drive attached 4.26 seconds after
resume and 20 tests ran in 0.27 seconds. This supports workspace-attach stalls
on some workers; it does not explain every timeout or prove the tests were free
of separate waits. The invocation's `bb remote` base commit also does not by
itself identify the exact patched source/action key.

Other failures in those runs were distinct: Notification Service acceptance
could not establish its Dispatcher/Runner Control stream, Runner attach lost
its tunnel, and an app test timed out creating Testcontainers' Ryuk container.
Do not attribute these to one cause without a correlated trace.

For the next occurrence, retain the exact TestRunner action metadata, VM log
tail and test log together. If `workspacefs` attaches promptly, capture a
bounded stack at the test's wait. Do not raise test timeouts or sizes from this
evidence. Related investigation: [#10405](https://github.com/agentydragon/ducktape/issues/10405).
