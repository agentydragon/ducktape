# Live test status after Bazel retries

## Observed retry

The `Test + Build + lint` step for [PR #15725](https://github.com/agentydragon/ducktape/pull/15725)
passed in [workflow run 43142492035, attempt 1](https://github.com/agentydragon/ducktape/actions/runs/43142492035).
Its original invocation,
[`145c211e-9a1e-547d-9c1b-5ac87c867819`](https://app.buildbuddy.io/invocation/145c211e-9a1e-547d-9c1b-5ac87c867819),
completed with a missing-CAS-input (`REMOTE_CACHE_EVICTED`) failure. The Bazel log then reported
`Found transient remote cache error, retrying the build...`; retry invocation
[`ef1d795c-7afc-4d10-88c2-da047235ff1f`](https://app.buildbuddy.io/invocation/ef1d795c-7afc-4d10-88c2-da047235ff1f)
and the overall step succeeded.

The live-status finalizer looked up the deterministic original invocation ID,
saw its terminal failure, and left the extra GitHub status red even though the
whole CI step passed. The finalizer can trust the successful CI step for this
status while linking to the workflow run; it cannot claim that the original
invocation passed.

## Remaining publication evidence issue

`bazel-ci.outputs.test_invocation_id` is assigned from the deterministic ID
before the Bazel step. Release and image-push workflows consume that output as
test evidence, so they can still receive the failed original invocation after
an internal retry succeeds. Fixing the live status does not change that output.
The successful retry's actual invocation ID must be captured through structured
CI evidence and propagated to those publication jobs.
