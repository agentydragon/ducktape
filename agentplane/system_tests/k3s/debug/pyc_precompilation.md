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
processes, including 2,787 remote executions. The small render test does not justify enabling collection on its own,
and these probes did not run a full k3s suite with collection.

`--@rules_python//python/config_settings:precompile=enabled` and `force_enabled` both fail
during analysis of `//devinfra/lint:pytest_main_checker`, which uses `main_module` and no direct
`srcs`: `rules_python` tries to look up `main_py = None` in its precompiled source map.
This is a separate global-policy blocker, before any RBE action runs
([enabled attempt](https://app.buildbuddy.io/invocation/71eb10b9-d9b3-4b63-9ade-f81c9e806f82),
[forced attempt](https://app.buildbuddy.io/invocation/599521d0-e622-4f60-a86e-474ace28a6bb)).
Target-level collection does not hit it.

Before enabling collection for the four CI suites, compare their actual import/collection
time and total Bazel invocation cost with and without it on matched cache states and RBE worker
classes. The added compile and cache-lookup work must fit within the CI critical path, including
the first run after dependency changes. Keep source files in runfiles for tracebacks and dynamic
imports, and verify subprocess imports in a full suite run.
