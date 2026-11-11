# Archive PostgreSQL image-load timeouts on RBE

Inspected source: PR #15300 head `42e4d5046942e614e080a44f7a6574fc97b6dcc2` and
current `devel` `5d7f024455f0ff84a43c382acac6930bf734e95f`. The archive test and
fixture files below were identical at both revisions. The original investigation
changed no test timeout or size; its measurements alone did not establish a
fixture or runtime fix. Later follow-ups are recorded in the conclusion.

## CI failure and exact-label history

GitHub [run 42892897908, attempt 2](https://github.com/agentydragon/ducktape/actions/runs/42892897908/attempts/2)
tested PR head `42e4d504`; its [BuildBuddy invocation](https://app.buildbuddy.io/invocation/de796e9e-9efb-5bab-b540-c7f56df5baa0)
reported four 60-second timeouts in PostgreSQL-backed archive tests:

| Target                                                | Last flushed setup step                                                                                                         |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `//x/ai_session_archive:test_initial_import`          | Postgres image load began at 21:02:26.179 UTC and was still in progress when the action ended at 21:03:00.197.                  |
| `//x/ai_session_archive:test_claude_title_projection` | Postgres image load took 31.6 s; `container.start()` began at 21:02:46.713 and did not reach readiness before the action ended. |
| `//x/ai_session_archive:test_codex_title_rename`      | Postgres image load began at 21:02:28.010 UTC and was still in progress when the action ended at 21:02:53.739.                  |
| `//x/ai_session_archive:test_codex_title_retry`       | Postgres image load began at 21:02:17.276 UTC and was still in progress when the action ended at 21:02:51.367.                  |

These actions did not reach their test bodies. `test_browser`, the Grafana
container test and the Agentplane system-test suite passed in that invocation.

`bbapi target history` with the canonical repository URL and a 720-hour window
returned no rows for these four exact labels. The pre-RC full run at
`6472c00d08c37beeb5c7b9ea1a00f9239ea2226e` did not show a new test result for
these labels, so it does not establish an uncached pass.
An older run had `test_codex_title_rename` pass in 54 s, but that different
revision does not establish the current cold-start behavior.

A separate uncached two-target [devel invocation](https://app.buildbuddy.io/invocation/a5069555-355c-4f67-82c0-ab03c7248707)
reported `test_codex_title_rename` timed out after 60.199 s and
`test_initial_import` passed in 34.509 s. Its test log used Python 3.14.4;
the exact-head run below used Python 3.14.10. BuildBuddy records this invocation
on `devel` at commit `0cdd8f8b14cae7b8e859fba79fd6a65d9372fad7`, with
`STABLE_BUILD_COMMIT=0cdd8f8b-dirty`. The indexed invocation export, events and
build log do not expose a Git-sync/patch record, so they do not establish that
its source tree exactly matched the `6472c00d` pre-RC full-run commit. Treat
this as an independent devel observation for the rename timeout, not an exact
base-tree comparison or evidence about the initial-import timeout.

## Controlled exact-head run

Ran all four targets sequentially with `--jobs=1`, `--nocache_test_results`,
streamed pytest output and the CI RBE image in
[invocation `d4838c38`](https://app.buildbuddy.io/invocation/d4838c38-035f-43ce-ac42-9c50d2a9b1b4).
Each test action ran on a different worker, so the per-tag `flock` in
[`oci.py`](../../oci/oci.py) could not share a Docker image across these actions.
The lock and loaded-image marker are machine-local; they only avoid duplicate
loads on the same worker.

The controlled run reproduced the setup failure in three targets. For
`test_initial_import`, Ryuk loaded in 1.2 s, Postgres image import took 38.1 s,
then `container.start()` began about 7 s before the 60-second action deadline;
no ready message was flushed. `test_claude_title_projection` and
`test_codex_title_rename` also timed out before Postgres image loading finished.
`test_codex_title_retry` passed in 51.7 s: its separate worker imported Postgres
in 17.0 s and started it in 3.6 s; pytest reported 42.9 s including fixture
setup. This pass shows substantial worker-to-worker variation, not that the
other cold starts are healthy.

The fixture in [`postgres_fixtures.py`](../../../util/testing/postgres_fixtures.py)
loads pinned Ryuk and Postgres layouts before calling `PostgresContainer.start()`.
The flushed log identifies active image import on the timed-out actions. On the
initial-import action, the deadline also left only a few seconds to observe
container startup, so these logs do not isolate the remaining startup time from
the import time. They support cold image loading as a direct contributor, not a
complete explanation for every worker's delay.

## Grouping probe and limits

To test whether removing repeated cold imports would fit the existing deadline,
a temporary BUILD-only probe grouped the three timed-out test targets into one
`py_test` with the original source files. The RBE action
[collected all four pytest cases](https://app.buildbuddy.io/invocation/c71f50e9-ad97-4e1c-ae04-f19c08498d9f)
but timed out at 60.4 s after both Claude projection cases passed and Codex
rename started; initial import was not reached. That worker imported Postgres
in 13.2 s and reached readiness in 4.7 s. The grouped action therefore failed
even with a comparatively fast cold setup. The four test functions retain
per-test database isolation, but the combined action does not fit the existing
small-test deadline in this probe. The BUILD experiment was discarded; this is
not a proposed change.

The same CI attempt also had separate unresolved failures in
`//agentplane/system_tests/vm:test_tcg_vmi_shutdown_policy` and
`//agentplane/app/frontend:visual_policies`; this investigation does not diagnose
or resolve them.

## Conclusion

The repeatable evidence is that cold RBE actions repeatedly import the same
Postgres image, and some workers spend most of the 60-second test window in
image loading or container startup. Grouping all three failing targets removes
two image loads but then runs too many cases within the existing deadline. The
grouping probe did not establish that every cold start exceeds the deadline or
that the test bodies themselves need more execution time. It also does not
measure the effects of the later image-reuse change below.

Since this investigation, the repository policy has been clarified: tests that
start a PostgreSQL container must be at least `medium`. [PR #15602](https://github.com/agentydragon/ducktape/pull/15602)
applied that policy to all 19 archive targets using the shared fixture. This is
a policy requirement; it does not change the historical measurements or mean
those measurements proved that every cold start needs a longer timeout.

[PR #15572](https://github.com/agentydragon/ducktape/pull/15572) extended the
archive's PostgreSQL/Ryuk image-reuse setup to all 19 targets using the shared
fixture. Each test still gets a fresh PostgreSQL container and database, and
cold image imports remain variable across workers. The follow-up does not
establish that cold imports are solved, so the original timings and their
limits above still apply.

The VM and visual-shard timeouts need separate diagnoses.
