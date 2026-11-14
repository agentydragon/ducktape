# Storage scenario races a recovering controller

Required CI for source `58c274be30` tested merge `35eb3cce00` and failed at the
one-shot `readyReplicas == 1` assertion before entering the storage body
([invocation e0cdcb09](https://app.buildbuddy.io/invocation/e0cdcb09-e0ad-5a46-88aa-465f85deb0a7)).
The other 20 cases did not run after that exclusive case failed. The controller
had lease PUT/GET timeouts, exited 1 at 23:42:47Z, restarted at 23:42:48Z and
became Ready at 23:42:58Z. Its exit cause remains unknown because previous
stderr was not captured. A separate MCP snapshot TSC restore failure recovered
through its infrastructure retry.

The guard waits up to 30 seconds for the same readiness predicate and saves
controller status and previous logs when a restart is present, including after
successful recovery. It leaves the storage body, storage assertions and suite
timeout unchanged. The change passed focused source-commit CI
([invocation 92dde5b6](https://app.buildbuddy.io/invocation/92dde5b6-dc28-5c52-9a0f-0482fd1d0466));
Nix and pre-commit checks also passed there. This is source-commit evidence; the
separate devel port still needs its own PR checks.

The real-cluster controls passed at source commit `ff39339c92`
([invocation 1dd90380](https://app.buildbuddy.io/invocation/1dd90380-d9a8-4eab-990c-907761aeb854)):
one physical TestRunner, two nonempty JUnit passes, no failures, errors, skips
or retries. The stuck-controller control observed the old predicate false and
the bounded wait failed after 30.149983 seconds as expected. In the recovery
control, the controller was restored only after the storage scenario entered
its readiness wait; it recovered in 10.206639 seconds and all original storage
probes passed, including claim refusal, suspend/resume, volume identity and
cleanup. The native test took 239.212 seconds and the physical test took
202.290760 seconds. Raw cluster artifacts remain private.

The devel PR's exact-head checks are pending. This addresses a test precondition;
it does not establish a production controller crash fix or fleet reliability
improvement. Follow up in [#16099](https://github.com/agentydragon/ducktape/issues/16099)
to inspect previous stderr on another restart and determine whether lease renewal
caused the exit.
