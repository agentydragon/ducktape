# Interleaved workspace GC: synthetic cache profile

Profiled the redesigned read-only `repo-cache` command with Heaptrack on Linux, using the
Bazel-built Python 3.14 binary, eight workers, no size measurement, and a synthetic output
root with one hash bucket. Each entry was an empty UUID directory with a recorded-inputs
file; no output bases referred to it. Both runs completed and classified every entry as
prunable. The normal session-environment output bases were also discovered. No deletion
was requested.

| Entries | Heaptrack peak RSS | Heaptrack peak heap |
| ------: | -----------------: | ------------------: |
|   1,000 |           84.80 MB |            23.88 MB |
|  10,000 |          151.89 MB |            19.93 MB |

Peak RSS includes Heaptrack overhead. These are single samples, not a production workload or
an estimate of the maximum supported inventory. The heap figures are not monotonic; allocator
and instrumentation behavior prevent treating this pair as a precise per-entry cost. The
source currently creates an inspection task for every entry in a discovered bucket, so memory
can grow with that bucket's size even though a semaphore limits active operations. No new
scheduler or hard inventory limit was introduced based on these samples.

The `all --no-prs` dry run against the real checkout and output-user root displayed live
progress at an interactive terminal: a captured refresh showed 792 ready, 8 active, and
28 waiting operations while worktree and base inspections were in progress. A separate
three-minute bounded run did not finish, so this does not establish that the full scan
completes within that interval. The initial terminal capture inherited `TERM=dumb`, for
which Rich does not render an interactive live display; the observed refresh used
`TERM=xterm-256color`.

Raw captures and the generated synthetic cache were kept under the session scratch directory
`/tmp/agents/2026_11_10/workspace-gc-interleave/profile/`; they are ephemeral. Heaptrack was
attached to the Python interpreter and Bazel stage-two bootstrap directly: profiling the
Bazel shell launcher alone recorded no Python allocations. The 10,000-entry profile collected
412,761 allocation calls; profiler runtime overhead makes its elapsed time unsuitable as a
normal execution benchmark.
