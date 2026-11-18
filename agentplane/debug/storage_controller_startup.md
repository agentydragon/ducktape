# Environment controller restart during storage CI

The environment controller logged lease PUT/GET timeouts, exited with code 1 at 23:42:47Z,
restarted a second later and became Ready at 23:42:58Z. The storage test failed at its readiness
precondition. The run lacked previous-container stderr, so the cause of the exit is still unknown.
[Original failing invocation](https://app.buildbuddy.io/invocation/e0cdcb09-e0ad-5a46-88aa-465f85deb0a7).

On the next restart, retrieve `storage-<case>-controller-startup.json` from the storage test's
undeclared outputs, including when the scenario passes. The [readiness guard](../system_tests/scenarios/storage.py)
added in [PR #16299](https://github.com/agentydragon/ducktape/pull/16299) records Pod status and
previous-container logs when it sees a restart. Match the terminated container's timestamps to
the last stderr lines and determine whether lease renewal caused the exit.
[Controller recurrence follow-up in #16099](https://github.com/agentydragon/ducktape/issues/16099#issuecomment-6856865600).
