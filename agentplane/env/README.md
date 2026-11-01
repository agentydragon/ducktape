# Agentplane environment services

The environment is split by runtime ownership. Environment Runtime owns Sandbox identity, bootstrap,
external control, and coordinated lifecycle. Harness Host owns native Sessions and journals. The
Execution Service owns typed process and filesystem execution. The Runner Gateway remains a
control-plane service outside `env/`.

## Layout and entrypoints

| Owner               | Executable target                                             | Main packages                                                                  |
| ------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Environment Runtime | `//agentplane/env/runtime/cmd:agentplane-environment-runtime` | environment owner/state, private lifecycle protocol, bootstrap, public control |
| Harness Host        | `//agentplane/env/harnesshost/cmd:agentplane-harness-host`    | Session, Claude/Codex adapters, app-server, retained records, journal, ingress |
| Execution Service   | `//agentplane/env/execution/cmd:agentplane-execution-service` | execution adapter, native exec-server, companion                               |

Shared process machinery is in `env/runtime/supervision`; one-shot operation bookkeeping is in
`env/oneoff`, shell execution in `env/scripts`, and Go deployment parsing in `env/configuration`.
Host owns workspace containment and helpers in `env/harnesshost/workspace`. The `env/e2e/` contains cross-service integration tests; image and VM packaging lives in `env/images/`.

Runtime and Host read the same `--config` and `--launch-file`. Runtime launches the Host with
private lifecycle and Session socket paths and an explicit `--supervision subprocess|systemd`.
Production Pod Runtime is PID1; its Host child uses subprocess supervision. VMs use distinct Runtime,
Host and Execution systemd scopes. Runtime retains the original environment owner inode; Host locks
`<state_dir>/harness-host/.agentplane-harness-host-owner`. Session state stays under `<state_dir>/sessions`
and the shared Codex home stays under `<state_dir>/codex`.

The private lifecycle protocol binds every intent/report to the same process boots and generation.
Host drains admitted work and native processes before reporting `native_stopped`. Runtime fences
Execution before acknowledging `execution_fenced`; only then may Host seal journals and report exact
History acknowledgements. Losing the control stream closes native admission and drains Host without
creating a seal. An abnormal Pod Host exit requires the outer container fence before replacement.
See [Host contract](../env/harnesshost/SPEC.md) and [lifecycle](docs/lifecycle.md).

The three executables ship with the `agentplane` CLI in `//agentplane/env/images:disk_image`.
Native Claude/Codex executables, workspace tools and CA certificates come from the runtime image.
