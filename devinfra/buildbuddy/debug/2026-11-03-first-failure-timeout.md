# Empty first-failure result for a timed-out test

On 2026-11-03, `bbapi invocation first-failure
954e48b4-b2c9-5515-98b0-7afacde044ab` returned `{"label":""}` after the
invocation completed unsuccessfully. The command was repeated with the same result.

The [native CI job](https://github.com/agentydragon/ducktape/actions/runs/41728124196/job/126238142186)
failed its `Test + Build + lint` step. `bb view` for the invocation reported
228 passing tests and one remote failure:
`//devinfra/js/debundle/skills/shared/examples:isolated_edit_test`, `TIMEOUT`,
64.5 seconds. `bbapi artifact list --failed` also reported that target's
`test.log`, `test.xml`, and `test.warnings` with status `TIMEOUT`. Its test log
ended after pytest printed the test filename, without identifying a blocked step.

An empty first-failure label therefore cannot establish that no test failed.
The invocation's final outcome and native test summary remain necessary evidence.

`failedTest` in `bbapi/cmd_invocation_watch.go` requests only the
`common.Status_FAILED` target group. A timeout may be indexed in another group;
this is a source-level lead, not a verified explanation of the server response.
Next action: inspect the invocation's target groups, include all non-passing test
statuses in the lookup, and verify that this invocation identifies the timed-out
target. Review the shared `invocation watch --fail-on-test-failure` caller too.

No test size, timeout, source behavior, or lookup behavior changed in this
investigation. The timeout's runtime cause remains open.
