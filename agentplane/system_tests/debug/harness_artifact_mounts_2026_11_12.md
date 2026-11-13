# Direct harness artifact mounting

This manual experiment tracks [#16030](https://github.com/agentydragon/ducktape/issues/16030).
Required suites retain ordinary OCI packaging. The candidate removes only the Claude/Codex archive
layers from the fixture environment-tools image, preserving its other tools, command links and UID.
It declares the pinned Claude executable and Codex file tree directly as Bazel test inputs.

K3s runs directly on the RBE action's node, so its Pod workloads can bind those declared input files
through read-only hostPath volumes. Every file is mounted at its ordinary `/opt/harnesses/…` path
in both Environment Runtime and the isolated Codex executor. Binding each resolved file avoids
absolute runfiles/file-cache symlinks dangling inside the Pod root. This is a fixture boundary;
it does not provide a shared production filesystem or a transport into VM guests.

The manual targets `//agentplane/system_tests/suites:test_harness_artifacts` and
`//agentplane/system_tests/suites:test_harness_packaging` run the same real Claude/Codex scenario.
Each creates two fresh Sandboxes with ordinary home/state volumes and checks the fresh writable
home, executable paths and versions. The mounted arm also checks the native mount flags are read-only.
Each harness executes two real Session turns against the scripted model and checks standing
instructions reach only the intended Session. The packaged arm boots the ordinary image and runs
the same turns, retaining a real image packaging/boot check in this experiment.

## Measurement boundary

The candidate's RBE inputs contain raw artifact files, while the control contains their OCI layers.
Compare actual input-root identities, bytes downloaded and input-fetch time alongside containerd
imports, stack setup, real turn time, cleanup, CPU and peak memory. Declaring a source file is not a
free cache hit. A paired invocation can establish feasibility and expose physical costs; different
workers, warmth and competing load prevent causal speedup attribution.

Required CI coverage is unchanged. Adoption requires a matched counterbalanced full selected
Pod/MCP Actions/VM graph, including a required packaged-image boot path, and preservation of source
pins, permissions, readonly mounts and fresh mutable home/state across later Sandboxes. Retain
separate VM guest packaging. Revisit on the next relevant backend head or within seven days after
a manual feasibility pass; close #16030 only after that graph and required-gate/resource comparison
supports a concrete policy decision.

## Observed feasibility

The [packaged control](https://app.buildbuddy.io/invocation/fb705349-840e-4a05-925c-59bb66304ad6)
passed both harnesses. Its mounted peer failed at the first Sandbox's real readiness boundary.
The retained runtime error was `fork/exec /bin/codex: no such file or directory`: the probe used
`Runfiles.Path.relative_to`, which re-rooted the relative destination beneath the runfiles root.
The declared source files existed and were executable, but Codex was mounted at the wrong path.
This was a probe construction bug, not evidence that direct mounting is infeasible. The first
correction then failed mypy before executing a test because the inferred source tuples mixed
Runfiles.Path and pathlib.Path. Converting both input paths to ordinary Path objects fixed that.
A preflight assertion now requires the exact `/opt/harnesses/codex/bin/codex` destination.

[Final mounted acceptance 845e3e0f](https://app.buildbuddy.io/invocation/845e3e0f-c4ee-4082-8858-b01e2e5037dc)
passed with Claude Code 2.1.252 and Codex 0.166.0. Both Sandboxes reported 44 harness-file mounts
with native `ro` mount flags, executable paths and fresh writable homes. Each harness completed
instructed and plain real Session turns against the scripted model. Normal fixture cleanup completed;
its infrastructure baseline was cold. The test source SHA256 is
`c4d58607cb4a297a8c733283e9cf8a67f42b15c0536629d247fa0791641d660c`, verified inside actual input root
`97d4e28e31a6c251ca155d24777272e5e267dfa1e41416b7b00effff81193035/165`.

| Observation                      | Packaged control | Mounted candidate |
| -------------------------------- | ---------------: | ----------------: |
| TestRunner process               |         152.835s |          128.472s |
| Stack setup                      |          79.540s |           59.097s |
| Environment-tools import         |          22.005s |            7.997s |
| First / second Sandbox readiness |  7.272s / 6.351s |   5.645s / 6.539s |
| Fixture cleanup                  |          26.553s |           26.605s |
| Native input-fetch wall          |           1.356s |            0.515s |
| Actual downloaded bytes          |    1,941,164,914 |       768,203,437 |
| Observed CPU                     |         386.375s |          291.065s |
| Peak memory                      |  4,878,757,888 B |   4,772,241,408 B |

These observations are feasibility evidence, not a speedup estimate. Control ran on worker
`b099453a-65e8-4674-a523-641db2f9e648` at 03:13:46–03:16:19 UTC; corrected candidate ran on
`150dea52-2b18-465e-90af-dd2ec83c7039` at 03:26:54–03:29:03 UTC on 13 November. Both used
managed Firecracker and requested 4 CU. Different workers, retained content and cache exposure
confound the comparison. Native summed file-download durations extended across execution and
cannot be read as elapsed input-fetch time; later lazy artifact reads are included in process cost.
The setup/import times overlap and are not additive. The failed peer's 300-second readiness wedge
is excluded from this passing comparison and retained in the linked invocation.

No production policy changes follow from this probe alone. Its tested boundary is now feasible;
next compare counterbalanced complete selected graphs with required image packaging/boot coverage
and native source/input identities, resource costs and actual required-gate feedback. Keep #16030
open. No full selected graph or fleet saving has been measured for direct mounts.

Exact declared input accounting reinforces that limit: the packaged OCI layout contributes
914,498,274 bytes in 13 unique CAS files. The mounted layout contributes 243,931,041 bytes in
11 files, plus 670,520,189 bytes of raw harnesses in 44 files: 914,451,230 bytes combined.
That is effectively the same input volume; roughly 47 KB of packaging metadata disappears.
The candidate moves the unpacking boundary and can avoid duplicate containerd storage, but it
does not meaningfully reduce declared input bytes. Actual download differences above reflect
cache exposure. The 44 hostPath volumes are mounted by both runtime and isolated executor;
full-graph trials must account for their per-Sandbox setup/resource cost as well.
