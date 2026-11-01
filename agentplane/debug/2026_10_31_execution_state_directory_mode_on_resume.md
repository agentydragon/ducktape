# Execution state-directory mode after Pod resume

BuildBuddy invocation `9e1effd3-cc85-55dc-b54b-993241dc4c4e` failed
`//agentplane/system_tests/suites:test_mcp` in
`test_pod_central_sampler_exec_resumes_after_sandbox_suspend_resume`. After
resume, the replacement Pod was Ready, but Runner Gateway stayed
`ConnectionPending` waiting for its first state message. The runner log showed
repeated `execution state directory must be private (0700)` errors.

Runtime configures the standalone Execution Service state under
`/state/execution`. Gateway's first `WatchState` calls Runtime's
`AdoptIncarnation`; Runtime starts Execution before it publishes the first
state response. `service.lock` rejected the existing persistent directory
because it had group or other permissions. The Pod uses `fsGroup: 1000`; the
volume's exact mode was not captured, but fsGroup handling on the remount is
the likely source of the widened mode.

`service.lock` now opens the directory and owner file without following
symlinks, restores mode `0700` through the directory file descriptor, and then
takes the owner lock. `//agentplane/env/execution/service:service_test` passed
in BuildBuddy invocation `f4026c82-8442-4ece-aa08-55abba5c81da`, including
mode restoration and symlink rejection. The focused
`//agentplane/system_tests/suites:test_mcp` rerun passed in 309.5 seconds in
BuildBuddy invocation `817f6875-3637-44e0-86cf-e695a6718cd8`.
