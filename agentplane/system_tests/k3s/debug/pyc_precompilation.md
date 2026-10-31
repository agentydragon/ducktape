# Python bytecode precompilation on RBE

On 2026-10-30, `rules_python` 2.4.3 with the repository's hermetic Python 3.14 toolchain
successfully precompiled and ran `//agentplane/system_tests/k3s:{test_guest_logs,test_render}`
on RBE when each test temporarily set `pyc_collection = "include_pyc"`. The October 21
standard-library lookup failure did not recur. The probes used the normal `bbr test` path;
the remote runner checked out `devel` at `3ccae0647205afca4764cf5a9ea0b67b40f95f93` and applied
the single-target BUILD change. The setting was removed after measurement.

| Probe                                                                                                                        | RBE result | Build elapsed | Test elapsed |                       Bazel processes |
| ---------------------------------------------------------------------------------------------------------------------------- | ---------- | ------------: | -----------: | ------------------------------------: |
| [`test_guest_logs` with bytecode](https://app.buildbuddy.io/invocation/43ef10ed-e8a6-4144-aa83-1b7c3842989d)                 | passed     |       115.7 s |        6.2 s |                          2,422 remote |
| [`test_render` with bytecode, first run](https://app.buildbuddy.io/invocation/372a2a15-9412-4c1c-b2fd-039e40e243bc)          | passed     |       145.5 s |       13.1 s | 2,787 remote, 2,423 remote-cache hits |
| [`test_render` with bytecode, warm cache](https://app.buildbuddy.io/invocation/5128e7e0-dd5b-42e9-a196-1e263c9e0e4a)         | passed     |        21.6 s |       14.4 s |     1 remote, 5,209 remote-cache hits |
| [`test_render` without bytecode, paired baseline](https://app.buildbuddy.io/invocation/3cb6f972-320b-49b7-b94d-c53088db8d74) | passed     |        28.0 s |       25.7 s |        1 remote, 32 action-cache hits |

The warm and baseline probes both used `--nocache_test_results --test_env=PYTHONVERBOSE=1`.
CPython's import trace reported 2,053 code objects loaded from `.pyc` and 235 generated at
runtime with collection; without collection, it reported zero loaded from `.pyc` and 2,288
generated at runtime. This proves the collected bytecode is consumed. The remaining runtime
generation includes standard-library modules, which the Bazel precompile action does not cover.
The two test durations are from different RBE workers; the 11.3 s apparent gain is not a
controlled estimate of suite or CI feedback savings. The warmed Bazel invocation was 6.4 s
shorter in this pair. The first collection invocation took 145.5 s and evaluated 5,220 Bazel
processes, including 2,787 remote executions.

## Full `test_pod` suite comparison

On 2026-10-31, one full `//agentplane/system_tests/suites:test_pod` run with collection was
compared with a baseline on the same source revision (`3608ca9`) and RBE worker. The isolated
[candidate commit](https://github.com/agentydragon/ducktape/commit/af6d622166b3dda8e898ef5aa2fb1977166e47c9)
sets `pyc_collection = "include_pyc"` only for this test target. Both runs used
`--nocache_test_results --test_env=PYTHONPROFILEIMPORTTIME=1 --test_output=errors`; both passed
all 13 tests.

| Run           | Invocation                                                                            | Test XML duration | Bazel elapsed | Critical path | Worker CPU |
| ------------- | ------------------------------------------------------------------------------------- | ----------------: | ------------: | ------------: | ---------: |
| Baseline      | [6c57acdd](https://app.buildbuddy.io/invocation/6c57acdd-d5eb-469a-8a5b-9054a94fabc4) |          422.59 s |     441.356 s |      438.93 s |   747.75 s |
| `include_pyc` | [af205ab9](https://app.buildbuddy.io/invocation/af205ab9-002d-4938-83ea-8202ddf41c3e) |          433.17 s |     452.629 s |      449.20 s |   752.72 s |

The test XML duration was 10.58 s longer with collection; total Bazel elapsed was 11.27 s longer.
This is one paired run, so it shows no end-to-end speedup but does not establish a stable
regression. `PYTHONPROFILEIMPORTTIME` recorded 1,907 import rows in both runs. Summed import
self-time was 6.638 s without collection and 5.417 s with it; the longest cumulative import path
was 3.768 s and 2.418 s, respectively. That import-path reduction did not appear in the measured
suite critical path.

The candidate profile contained 122 processed `PyCompile` actions. A separate build-only check
forced the full target's `PyCompile` actions past the action cache with Bazel 9.2's
`--modify_execution_info='^PyCompile$=+no-cache'`; remote execution and the ordinary remote cache
configuration remained enabled. Invocation [a6394df1](https://app.buildbuddy.io/invocation/a6394df1-cb58-4dbb-ada4-20dcb6765985)
completed 2,651 remote executions. Its Bazel profile contained 2,651 `PyCompile` action-processing
events with 5,578.392 s summed action span (1.938 s median, 6.17 s maximum), overlapping across
workers; the build elapsed 117.120 s with a 6.18 s critical path. A sampled `.pyc` execution
reported `skipCacheLookup=true`, `doNotCache=true`, and “Served from cache: No”; its worker ran it
for 1.661 s and recorded 1.046 s CPU. The invocation summary does not expose aggregate remote
worker CPU, so the sample is not a suite-wide CPU estimate. The option change discarded Bazel's
analysis cache (39 packages loaded, 73,488 targets configured), so the 117.120 s build includes
analysis setup as well as compilation. The downloaded `test_pod.py` source and executable digests
matched the candidate full-suite run (`3fffd24661384297e8dbb299606c2f4325235e30da7ba1c6d3e67f63e86c73e1`
and `d2e0858e37e510bcb59b9e5b9b7f07966629701ad3c8ab22708143170a1065d2`).

This one target has no demonstrated end-to-end benefit, and the forced uncached build shows that
collection can add substantial dependency compilation work. Keep `pyc_collection` unchanged by
default; this experiment does not justify enabling it for the other CI suites.

`--@rules_python//python/config_settings:precompile=enabled` and `force_enabled` both fail
during analysis of `//devinfra/lint:pytest_main_checker`, which uses `main_module` and no direct
`srcs`: `rules_python` tries to look up `main_py = None` in its precompiled source map.
This is a separate global-policy blocker, before any RBE action runs
([enabled attempt](https://app.buildbuddy.io/invocation/71eb10b9-d9b3-4b63-9ade-f81c9e806f82),
[forced attempt](https://app.buildbuddy.io/invocation/599521d0-e622-4f60-a86e-474ace28a6bb)).
Target-level collection does not hit it.

Keep source files in runfiles for tracebacks and dynamic imports; these measurements do not assess
subprocess-only imports or the other CI suites.
