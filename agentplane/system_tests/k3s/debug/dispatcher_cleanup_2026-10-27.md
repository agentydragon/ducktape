# Dispatcher cleanup failure, October 26 CI run

The failed `//agentplane/system_tests:test_dispatcher` result in [BuildBuddy invocation
7d08c8f3](https://app.buildbuddy.io/invocation/7d08c8f3-2186-5057-9b1f-08aa7a98a740)
was the wake scenario's cleanup wait for `Ready.reason=Stopped`. Its retained
`test.outputs/dispatcher-wake.json` says `passed: true` and records runner admission
before cleanup. The test log's `pod/systest-wake-32198300` diagnostics show
`start Codex executor: fork/exec /bin/codex: no such file or directory`, followed
by companion restart backoff and the runner's `Codex executor startup: context
deadline exceeded`. The `k3s` target at that source revision selected the
`model_free` runner image, which has no Codex binary. The same-source retry
[786f259e](https://app.buildbuddy.io/invocation/786f259e-a8b7-5444-b806-a23f7ebf07eb)
passed its admission and cleanup assertions, but did not prove a Codex
executor could start with that image.

[PR #12167](https://github.com/agentydragon/ducktape/pull/12167) already
changed this target to the harness image and made the companion refuse an
unstartable claim without entering restart backoff. A fresh unfiltered
`test_dispatcher` run passed all three scenarios on the corrected source
([BuildBuddy invocation 19325b64](https://app.buildbuddy.io/invocation/19325b64-2793-4f6e-8ab1-2211efd4cc38)).
The separate cleanup change in this directory preserves a scenario exception
if cleanup also fails; the cited failed attempt had no earlier scenario
exception to preserve.
