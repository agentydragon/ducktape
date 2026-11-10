# PR 12822: repeated Bazel profiler clock crashes

GitHub run [40811672455](https://github.com/agentydragon/ducktape/actions/runs/40811672455)
failed on attempts 1 and 2 before test execution. Both attempts crashed inside
Bazel's profiler at `TimeSeries.toDoubleArray`; attempt 1 also logged an
`Invalid duration` warning. CI used the shared
`ducktape-bazel-ci-v3` runner recycling key.

The `bb-remote` action documents that changing this key starts a fresh
recyclable runner. The workflow moves to `ducktape-bazel-ci-v4` to isolate CI
from the prior runner namespace while keeping recycling enabled. These logs
show a clock regression and profiler failure; they do not establish the
underlying Firecracker or host cause. An earlier occurrence is recorded in
[GitHub run 40787876182](https://github.com/agentydragon/ducktape/actions/runs/40787876182).
