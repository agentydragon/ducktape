# Agentplane environment services

The environment is split by runtime ownership. Environment Runtime owns Sandbox identity, bootstrap,
external control, and coordinated lifecycle. Harness Host owns native Sessions and journals. The
Execution Service owns typed process and filesystem execution. The Runner Gateway remains a
control-plane service outside `env/`.

## Layout and entrypoints

`//agentplane/env/runtime/cmd:agentplane-environment-runtime` runs the environment owner,
Harness Host and Execution API in one process. Runtime owns bootstrap and public control; Host
owns Sessions, harnesses and journals; Execution owns the native exec-server and execution adapter.
`//agentplane/env/runtime/execution/cmd:agentplane-execution-service` remains independently runnable and
provides the Pod execution companion.

Shared process machinery is in `env/runtime/supervision`; one-shot operation bookkeeping is in
`env/oneoff`, shell execution in `env/scripts`, and Go deployment parsing in `env/configuration`.
Host owns workspace containment and helpers in `env/runtime/harnesshost/workspace`. The `env/e2e/` contains cross-service integration tests; image and VM packaging lives in `env/images/`.

Runtime constructs Host from the same `--config` and `--launch-file` and coordinates its lifecycle
and Session calls directly in the process. Production Pod Runtime is PID1 and Host shares its reaper.
In VMs, native harnesses and executors use separate systemd service cgroups in the workload and
executor slices, bound to the Runtime service lifetime. Runtime retains the environment owner inode; Host locks
`<state_dir>/harness-host/.agentplane-harness-host-owner`. Session state stays under `<state_dir>/sessions`
and the shared Codex home stays under `<state_dir>/codex`.

The lifecycle coordinator binds every intent/report to the same process boots and generation.
Host drains admitted work and native processes before reporting `native_stopped`. Runtime fences
Execution before acknowledging `execution_fenced`; only then may Host seal journals and report exact
History acknowledgements. Losing the Host lifetime closes native admission and drains Host without
creating a seal. An abnormal Pod Runtime exit requires the outer container fence before replacement.
See [Host contract](runtime/harnesshost/SPEC.md) and [lifecycle](runtime/docs/lifecycle.md).

The Runtime and execution companion executables ship with the `agentplane` CLI in `//agentplane/env/images:disk_image`.
Native Claude/Codex executables, workspace tools and CA certificates come from the runtime image.
