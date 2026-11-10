# Archive PostgreSQL image reuse on RBE

The [archive timeout investigation](https://github.com/agentydragon/ducktape/pull/15537)
found repeated PostgreSQL image imports in four small test actions. On commit
`975f68a29e4139cd66e0010a480d70c100c599c1`, those targets use the existing
digest-keyed PostgreSQL/Ryuk recycling family. This changes only the executor
family: the tests still start fresh PostgreSQL containers and create their own
databases. Remote snapshot sharing and the save policy remain unset.

## Cold and restored actions

Ran the four targets twice with `bbr test --nocache_test_results`; action and
build caches remained enabled. Each row is a fresh physical TestRunner action.
The first [invocation](https://app.buildbuddy.io/invocation/7528fb07-7d5f-4a19-a14b-c4e98913c765)
landed on four cold workers; the [repeat](https://app.buildbuddy.io/invocation/ffbe0e3f-d558-4ef0-985d-eeeb96ebc6d1)
restored three of those VMs and landed the fourth on a different cold worker.
Times are test-action durations, not CI critical-path savings.

| Target                         |    First action | First PostgreSQL import |  Repeat action | Repeat PostgreSQL import |
| ------------------------------ | --------------: | ----------------------: | -------------: | -----------------------: |
| `test_claude_title_projection` |  37.5 s, passed |                   8.0 s | 34.3 s, passed |                     none |
| `test_codex_title_rename`      |  42.3 s, passed |                   8.4 s | 32.6 s, passed |                     none |
| `test_codex_title_retry`       |  43.6 s, passed |                  12.2 s | 32.8 s, passed |                     none |
| `test_initial_import`          | 60.2 s, timeout |                  31.9 s | 50.0 s, passed |                   21.0 s |

The first initial-import action reached PostgreSQL readiness 4.1 seconds after
its import completed, shortly before the deadline. Its timeout does not isolate
the work still underway after readiness. In the repeat, its different worker
also imported Ryuk and PostgreSQL and reached readiness in 2.7 seconds.

For the other three targets, the repeat's `VMMetadata.lastExecutedTask` names
the matching first-run execution; VM IDs, executor hosts and configuration
hashes match. Their repeat setup logs contain no Ryuk or PostgreSQL import and
show fresh PostgreSQL readiness in 3.2–3.4 seconds. The same four pytest cases
passed in those three targets in both runs; initial import passed its one case
in the repeat. The three paired duration differences include other variable
work, so they are not a controlled estimate of fleet-wide savings.

The decoded first-run Action carries `recycle-runner=true`, Firecracker
isolation, and the expected `postgres-images-v1-...` key, with no
`allow-remote-snapshots` property. The test actions' distinct execution IDs and
`doNotCache` metadata confirm fresh executions; resuming a VM alone would not
prove the image was present.

## Hosted CI and limits

The [PR CI attempt](https://github.com/agentydragon/ducktape/actions/runs/42917751198)
tested synthetic merge commit `47e21db0`. Its outer BuildBuddy stream closed
while the task was claimed, leaving the [Bazel invocation](https://app.buildbuddy.io/invocation/5dce3ad4-e653-5143-bcf2-1473c2b33e40)
disconnected. Three test results were reported as passing. The physical
`test_initial_import` execution independently reported `deadline exceeded`;
its cold-VM output tree contains a setup log ending after PostgreSQL import
started, with no completion line. Bazel did not publish its final test result
before the disconnect, so the execution timeout and the stream failure are
separate observations.

This experiment shows useful local image reuse on three matched workers, not a
guarantee that an arbitrary placement will be warm. It neither establishes a
fleet hit rate nor fixes the cold-start timeout. The long cold imports and
remaining startup/test work need their own diagnosis if cold actions are to
meet the existing deadline reliably.
