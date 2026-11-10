# Remote runner patch application stalls

On 2026-11-02, bounded native-operation CLI tests through `bbr` stalled before
Bazel started. The runner log stopped during `git apply --verbose` on a patch
that added neutral fixtures to `e2e/native_operation_input_test.rs`. The first
attempt stopped while checking that file; the next checked all files and
stopped after applying preceding documentation files. API execution status
remained `EXECUTING`; `bb view` showed the same last output as the observer.
The current diff's local reverse `git apply --check` completed in 0.03 seconds.
These observations do not establish the remote cause.

| Attempt                                           | Runner invocation                      | Requested Bazel invocation             |
| ------------------------------------------------- | -------------------------------------- | -------------------------------------- |
| Initial, superseded by completed consumer changes | `3c0d3cfd-b04c-48d0-b511-da4a654e94e3` | `98cb2be3-3f50-4ff4-a512-216113070cbc` |
| Completed consumer snapshot                       | `f8eab7e9-48cb-4169-b9d7-7c0d4ce96a8d` | `858e00e5-44af-4961-8d00-22d26870915d` |

Both test-only runner invocations were explicitly cancelled once. The bounded
validation switched to `nix develop --command bazelisk test`, preserving normal
RBE and remote-cache settings. A future investigation should inspect the
runner's patch-application process and filesystem state while the stall is
live, before attributing it to the patch or Bazel.
