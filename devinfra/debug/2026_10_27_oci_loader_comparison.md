# Python OCI loader versus `rules_oci` `oci_load` (2026-10-27)

## Decision

Keep the Python loader's digest/daemon-ID guard and per-tag lock for now. A warm
guard hit avoided a repeated `docker load` in this experiment, but the common
PostgreSQL session fixture loaded each image only once in its observed test
action. The measured importers overlapped once Docker layers were warm; there
is no current evidence that Python's tar implementation is materially faster
than pinned `rules_oci`. A small follow-up could retain the guard and lock
while delegating the import to `oci_load`, but this report does not justify a
production migration or a claim of full-CI time savings.

This is a bounded comparison on base `6d29199a2f24cadf0d08df589d64da6d65c44652`.
It changes no loader policy. Go's current tag-presence shortcut remains in
[PR #12237](https://github.com/agentydragon/ducktape/pull/12237); commit
`11fc6ac91d81ef39385775c7b6ed75a5c4527901` added a TODO to verify its
pin and consider sharing loading with Python.

## What was compared

`util/oci.py` reads the first OCI manifest digest from `index.json`. A hit needs
both a per-tag marker containing that digest and the Docker ID recorded at the
last load to match the daemon's current tag ID. A miss takes a `flock` under
`tempfile.gettempdir()`, rechecks, writes a Docker-format tar stream using
Python `tarfile`, then records the digest and daemon ID. This catches a changed
Bazel pin, prune, or external retag. `third_party/containers/BUILD.bazel`
already declares `oci_load` targets for PostgreSQL 18 and Ryuk 0.14.0.
Pinned `rules_oci` 2.3.0's `oci/private/load.sh.tpl` uses `bsdtar` and an
mtree in process substitution, streaming into `docker load --input`; it does
not create a large cached tar artifact, inspect the tag, or lock. Both paths
construct a Docker-format tar at runtime. The Python source and fixture have
stale wording about loading "via crane" and immutable upstream tags; neither
describes the present import path or pin policy.

The reproducible manual targets are
`//devinfra/debug/oci_loader_benchmark:compare` and `:compare_concurrent`.
Run with `bbr test //devinfra/debug/oci_loader_benchmark:compare
--nocache_test_results --test_output=all` (likewise for `:compare_concurrent`).
Set `--test_env=FIRST_IMPORTER=python` for the Python-first order; the default
is `rules` first.
The first target alternates importer order over three pairs of initially
absent, benchmark-owned tags in one Docker daemon. Its Python driver uses the
production `load_oci_image`. The guarded `oci_load` variant uses the same
`_already_loaded`, `_one_loader_at_a_time`, and marker write policy, changing
only the import command. Bash `time` reports full command wall and client
process-tree user/system CPU; it does not measure long-lived `dockerd` CPU.
Both targets assert their benchmark-owned tags are absent at the start and
fail rather than prune a reused daemon, so a rerun is never silently called a
fresh-tag trial.
Each Python command starts a new interpreter, whereas the real pytest fixture
calls the function in an existing process. The first no-op Python invocation
took 1.149s and 0.872s in the two actions; later warm guarded calls took about
0.1s. Startup is therefore variable and cannot be subtracted as one constant.

The two passing comparison invocations are
[Python first](https://app.buildbuddy.io/invocation/881d30c3-41a1-4680-a825-e040eaab1951)
and [rules_oci first](https://app.buildbuddy.io/invocation/4c230353-0560-4a20-991c-b1c2d5f8dbe0).
Their raw BuildBuddy test logs are copied as
[first_python.test.log](oci_loader_benchmark/first_python.test.log) and
[first_rules.test.log](oci_loader_benchmark/first_rules.test.log). Both actions
reported Docker 28.1.0 with `overlay2` at `/var/lib/docker`, empty
`DOCKER_HOST`, and `TEST_TMPDIR=/workspace/_tmp/7b00ef401257a97257fb5f26b09367e4`.
The actual benchmark `TestRunner` action platform contained
`workload-isolation-type=firecracker`, `init-dockerd=true`, and
`recycle-runner=true`, with RBE image digest
`sha256:478978590586fa9688ed0b5d8b02c7c8534ed80824b72d27e1c51e9fe53df207`.
The actions ran on different worker IDs and both found the same benchmark tags
absent at start. This proves no observable tag reuse between these two actions;
it does not establish whether arbitrary concurrent test actions share a daemon,
`/tmp`, marker files, or a lock. The outer `bbr` runner configuration alone is
not evidence of the test action's filesystem topology.

| Condition, full command elapsed                      |                        Python first action |                    `oci_load` first action |
| ---------------------------------------------------- | -----------------------------------------: | -----------------------------------------: |
| First observed import, tag absent                    | Python 28.427s; `oci_load` after it 0.755s | `oci_load` 18.591s; Python after it 0.588s |
| Later fresh tags after first import, Python          |                               0.822–0.939s |                               0.384–0.543s |
| Later fresh tags after first import, bare `oci_load` |                               0.663–1.086s |                               0.419–0.495s |
| Repeated warm tag, Python guard                      |                               0.121–0.142s |                               0.100–0.127s |
| Repeated warm tag, bare `oci_load`                   |                               0.585–0.668s |                               0.311–0.393s |
| Repeated warm tag, guarded `oci_load`                |                               0.111–0.122s |                               0.102–0.107s |

The first import in each action dominates; the initially absent tag does not
prove an empty Docker layer store. Once either importer had run, new tags used
already imported layers. The two first-import numbers are from different
actions and are not a controlled cold-store comparison. In each action, later
fresh-tag importer timings overlap. A speed advantage for either tar builder
is not established. Bare `oci_load` repeats work on every warm call; the
equivalent guard makes its warm behavior match Python's. The guarded
`oci_load` first call after bare loading was deliberately labeled "warm tag,
missing marker" and reloaded, as the policy requires; only its second call
is a guard hit.

The first comparison TestRunner spent 0.112s queued, 16.449s fetching inputs,
51.053s executing the script, and 1.875s uploading outputs, per BuildBuddy
execution metadata. The top-level `bbr` invocation took 105.243s. Those are
different clocks; the script's command timings do not include remote runner
allocation, Bazel analysis, or input fetch. BuildBuddy reported 118,108 bytes
downloaded for that action, which is its reported CAS transfer, not the Docker
tar byte count; Docker import bytes were not measured.

## Fixture and contention boundary

An uncached exact-base `bbr test //util/testing:test_postgres --nocache_test_results`
[passed in 48.9s](https://app.buildbuddy.io/invocation/000ea7f1-1438-49ed-8e2f-6069c41d66ed).
Its uploaded `container_setup.log` recorded Ryuk import 1.1s,
PostgreSQL import 25.7s, and PostgreSQL readiness after container start 6.0s.
The raw phase log is [postgres_container_setup.log](oci_loader_benchmark/postgres_container_setup.log).
This is one full setup observation; it does not compare the end-to-end fixture
against `oci_load`. The PostgreSQL image had no observed warm guard hit in that
action. The session-scoped fixture normally calls the loader once for each
image per pytest action, so the warm-hit microbenchmark should not be applied
to every PostgreSQL test as a saving.

The August [Docker timeout investigation](2026_08_14_docker_test_timeouts.md)
records a plausible concurrent-load herd and the rationale for a per-tag lock.
It is historical evidence, not a current controlled measurement of shared
RBE daemon state or a current importer-speed result. The manual
`:compare_concurrent` target bounds four same-action callers under one daemon;
that tests lock behavior only within that action. Its one passing
[invocation](https://app.buildbuddy.io/invocation/711d2038-d7f7-4192-aa00-da8ee4689754)
first loaded a benchmark-owned PostgreSQL tag in 34.499s, then launched each
four-caller group against a different absent tag with layers already used:

| Four same-action callers | Group wall time | Observed imports                                                             |
| ------------------------ | --------------: | ---------------------------------------------------------------------------- |
| Python guarded loader    |          5.105s | Python caller logs were empty; source guard/lock enforces one loader per tag |
| Bare `oci_load`          |          4.725s | All four caller logs say `Loaded image`                                      |
| Guarded `oci_load`       |          1.591s | One caller log says `Loaded image`; three are empty                          |

The [raw test log](oci_loader_benchmark/four_callers.test.log) records wall and
client process-tree CPU. Its twelve individual caller logs remain attached to
the BuildBuddy invocation. This is one ordered run, so the 5.105s versus
1.591s guarded-importer difference may reflect process startup, daemon state,
and contention; it does not establish that the tar backend should change.
The four bare callers did redundant imports, while the equivalent guard
avoided three of them. A cross-action CI reliability claim still needs
evidence that those actions actually share the Docker daemon and lock
filesystem. No daemon was pruned, and no other job's tags or containers were
removed.

## Next small change

Keep the current guard and lock. If maintenance cost of the manual tar builder
becomes material, make one narrow PR that invokes the existing `oci_load`
script from inside the same guarded policy, keeping the pin/prune/retag checks.
Validate it with an owned-tag test and an uncached PostgreSQL fixture test.
Do not replace the guard with a tag-presence check or claim a CI speedup from
this comparison. Separately, reconcile the historical lock comment with the
actual test action topology once cross-action sharing is measured.
