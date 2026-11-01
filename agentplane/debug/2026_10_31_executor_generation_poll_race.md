# Executor generation recovery test race

BuildBuddy invocation `ba012287-726e-538e-9d0f-09f88b743e6f` failed
`//agentplane/env/e2e:test_executor_container` while waiting for the companion to
report an unowned executor generation after the detached process exited.

The companion fences workload processes and publishes a fresh idle generation.
Environment Runtime can claim that generation as soon as Execution Service
restarts, so a 100 ms test poll can miss the transient unowned state. The test
now waits for a changed, owned generation, which proves recovery without
requiring that intermediate state to remain observable.

The focused BuildBuddy rerun passed in invocation
`5e24c086-83a3-400f-a5a9-f3f513f1be4e`. The PR CI rerun is the remaining check.
