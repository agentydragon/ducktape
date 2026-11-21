# Dispatcher cleanup timeout

The original CI invocation `6195e2a1-c114-51d5-b40d-dea24592409a` failed in
`test_deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends`. The
test's evidence file recorded `passed: true`, then `finally` cleanup timed out waiting for
`Ready.reason=Stopped` after the Sandbox reported `operatingMode=Suspended`. The Pod remained
running with one restart. Wake and admission assertions passed; stop and quiesce did not finish
before their deadline. The original artifacts do not establish why.

The focused uncached rerun, invocation `96eea856-53a1-42c5-b8c2-1ed7651be428`, passed in 169.880
seconds according to BuildBuddy `GetTarget` and did not reproduce the timeout, so it produced no
cleanup snapshot. The `bbr` wrapper separately returned 403 while fetching its outer invocation
(`336c9bc4-1177-4a82-b4ac-cde4e9120706`); this did not change the authoritative target result.

A failed Dispatcher scenario or Sandbox cleanup writes
`dispatcher-cleanup-<sandbox>-<capture id>/diagnostics.json` to undeclared outputs
([`capture_cleanup_diagnostics`](../system_tests/driver/sandbox_diagnostics.py)). It holds the Sandbox
and Pod, current and previous Environment Runtime and executor logs, recent `environments`
Deployment logs, container image state and the archived runner data. Each `kubectl` command has a
30-second deadline; completed results remain in the file when another command times out. If the
failure recurs, use that snapshot to establish why the suspended Sandbox and running Pod did not
reach the stopped state before diagnosing or changing lifecycle behavior.
