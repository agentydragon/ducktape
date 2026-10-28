# Runner configuration

Pods and VMs start the same executable with the same configuration contract:

```sh
/run/agentplane/runner/bin/agentplane-runner \
  --config /run/agentplane/config/runner.json \
  --launch-file /run/agentplane/sandbox/launch.json
```

Both arguments are required. Unknown flags, positional arguments, unknown JSON fields and unsupported
versions fail before Runner opens state. The executable name does not select a mode. The file schema
is [`configuration.py`](configuration.py); [`configuration.go`](configuration.go) validates and
composes it into the runtime configuration. The [protocol contract](SPEC.md) covers Session behavior.

## Ownership and composition

1. The template writes `runner.json`: endpoints, paths, providers, environment declarations,
   platform instructions and isolation policy. `runner_sandbox.py` writes Pod configuration;
   `runner_vm.py` writes VM configuration using the same schema.
2. The image supplies only explicitly inherited environment names. Provider `token_env` fields
   name Runner variables containing credential placeholders; tokens are not serialized in the file.
3. The Sandbox Controller supplies `launch.json`: namespace, name, UID and optional bootstrap source.
   Deployment policy and platform instructions cannot be supplied by this file.
4. `SessionSpec` supplies harness, absolute working directory, model, optional reasoning effort,
   task instructions and Session setup. A Session cannot raise template resource limits.

The loader resolves `runner_environment` against the process environment, overlays its explicit
values, and resolves `harness_environment` against that result. In each declaration `inherit` is an
allowlist of names, and `values` is a map that takes precedence over inherited values. Missing
inherited names are omitted with a warning. Undeclared process variables never enter harnesses.
All validation completes before Runner environment changes or state ownership begins.

## File fields

`runner.json` is a strict JSON object with `version: 2`.

| Field                           | Meaning                                                                                                                     |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `session_reconcile_concurrency` | Optional positive count of concurrent Session COSI reconciliations; defaults to `16` and does not limit Session admission.  |
| `state_dir`                     | Required absolute persistent state path; deployed as `/state`.                                                              |
| `initialization_cwd`            | Required absolute bootstrap working directory.                                                                              |
| `workspace_root`                | Optional absolute directory constraining Session working directories. Omission permits requested absolute paths directly.   |
| `control`, `ingress`            | Required objects containing `target` (`host:port`) and `bearer`. They select the Runner Gateway and History ingress routes. |
| `platform_instructions`         | Required deployment-wide Session guidance, rendered from `platform_instructions.j2`.                                        |
| `runner_environment`            | Optional `inherit` names and `values` map applied to Runner itself.                                                         |
| `harness_environment`           | Required `inherit` names and `values` map defining the native process environment.                                          |
| `claude`, `codex`               | Optional provider objects: absolute `binary`, HTTP(S) `base_url`, and `token_env`. An omitted provider is disabled.         |
| `environment`                   | Required tagged Pod or VM policy, including the optional executor.                                                          |
| `debug_checkpoint`              | Optional test-only object containing nonempty `name` and `command_id`.                                                      |

`environment` has exactly one discriminator, `kind`:

- `kind: "pod"`: optional `executor` with loopback `url` and absolute ownership `socket`.
  Kubernetes owns the container resource limits.
- `kind: "vm"`: required `resources`, non-root `agent_uid`/`agent_gid`, absolute `block_devices`,
  optional `console_autologin` (false when omitted), and optional `executor` with loopback `url`.
  Systemd owns containment; both native service slices use the VM identity and block devices.

The VM `resources` object contains `guest_memory_bytes`, `guest_vcpu_count`, `os_reserve_bytes`,
`service_memory_high_bytes`, `service_memory_max_bytes`, `service_cpu_quota_micros`,
`service_pids_max`, and `aggregate`/`process` harness budgets. Its optional `executor` budget must be supplied together with
the VM executor endpoint. Each budget contains `memory_max_bytes`, `cpu_quota_micros` per fixed
100,000 µs period, `pids_max`, `read_bps`, `write_bps`, `read_iops` and `write_iops`. Runner derives
cgroup limits from these budgets; the document contains each budget once. Process memory/CPU/PID
ceilings must fit the aggregate; harness and executor caps must fit the enclosing service.

Omitting `environment.executor` selects local tools. An executor requires Codex, and its endpoint
must be `ws://127.0.0.1:<nonzero port>`. `CODEX_EXEC_SERVER_URL` belongs to Runner and is rejected in
the harness environment. Lifecycle, recovery and scope are documented in
[Codex executor isolation](docs/codex_executor.md).

## Shared layout

The delivery mechanism differs: a Pod mounts ConfigMaps, while a VM mounts read-only ConfigMap disks.
The paths consumed by Runner and tools are identical.

| Path                                             | Owner and contents                                  |
| ------------------------------------------------ | --------------------------------------------------- |
| `/run/agentplane/config/runner.json`             | Template-owned Runner configuration.                |
| `/run/agentplane/sandbox/launch.json`            | Controller-owned Sandbox identity and bootstrap.    |
| `/run/agentplane/config/kubeconfig`              | Sandbox workload identity through the egress proxy. |
| `/run/agentplane/trust/ca-certificates.crt`      | Egress proxy CA bundle.                             |
| `/run/agentplane/trust/ca-certificates.p12`      | Java proxy trust store.                             |
| `/run/agentplane/runner/bin/agentplane-runner`   | Shared executable on the read-only Runner disk.     |
| `/run/agentplane/runner/bin/agentplane-executor` | PID-1 companion for isolated Pod Codex execution.   |
| `/state`                                         | Durable Session journals and native persistence.    |
| `/state/workspaces`                              | Sandbox-owned workspace storage.                    |

The Pod also mounts the CA at the system trust location for clients which do not read CA environment
variables. Its runner config is a per-template ConfigMap, including specializations such as the
BuildBuddy-enabled tools image. The companion mounts workspace/home/tmp and tool configuration;
it does not mount Runner configuration, provider token variables or projected sidecar tokens.

Home persistence is explicit template policy. Pod configuration uses `/home/runner`, shared through
an `emptyDir` in isolated mode. VM configuration uses the existing `/state/workspaces/.home` on the
workspace disk. This contract change neither moves nor deletes home/state data. Image installation
paths also remain explicit: `/bin/{claude,codex}` in Pods and `/run/current-system/sw/bin/{claude,codex}`
in the NixOS guest. VM native processes use uid/gid 107, matching KubeVirt's virtiofs owner; Pod
processes use uid/gid 1000.

## VM boot and resource policy

VM boot policy lives in `runner.json`'s `environment` variant. `GuestResources` and both environment
variants are defined in [`configuration.py`](configuration.py). The strict decoder rejects fields
belonging to another environment; there is no separate boot document or executor discriminator.

One typed `Environment.vm_resources` value supplies KubeVirt RAM/vCPU sizing and the VM variant's
resource policy, including boot service limits and harness/executor budgets. `vm.nix` mounts the disks
and installs the shared systemd units; it does not supply an alternate Runner configuration loader.

Before Runner starts, systemd invokes the executable's private `--vm-resource-properties` mode
with `runner.json`. The preflight validates resource relationships and live `/proc/meminfo` before
printing `MemoryHigh`, `MemoryMax`, `MemorySwapMax=0`, `CPUQuota` and `TasksMax`. The maximum must be
below live `MemTotal`, and nominal sizing must leave the configured OS reserve. The helper applies
these properties to `agentplane.slice`, enclosing Runner, workloads and executor. The Runner unit uses
`OOMPolicy=continue`. Aggregate and native process cgroups use `memory.oom.group=0`, so a cgroup OOM
selects individual victims. The kernel may select the harness or executor and may kill more processes
if pressure persists. Native leader exit still fences all descendants. Runner logs OOM counters and available peak memory before releasing a native service.
See [supervision](docs/supervision.md) for the unit hierarchy and startup fence.

`/run/agentplane/config/runner.env` supplies the VM systemd unit's Runner-only OpenTelemetry
settings. Pod templates set the corresponding container environment directly. The Go guest loader
has been removed; the common file explicitly supplies proxy, trust, tool paths and identities.

## Provider, Session and telemetry settings

Deployed providers read `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY`, set to the workload credential
placeholder. The egress proxy substitutes credentials and the authenticated LLM ingress routes
requests. Claude uses the ingress origin; Codex uses its `/v1` API base. Claude retains its resolved
context budget for resume; Codex obtains metadata from the ingress catalog.

Runner composes platform instructions, the Session's notification destination and caller task in
that order. They are persisted and must match when reopened. Sandbox bootstrap and Session setup
remain distinct one-time operations. Claude has one process per Session; Codex Sessions are threads
in one shared app-server, with model/effort/cwd/instructions selected per thread.

OpenTelemetry uses the standard `OTEL_*` environment contract, including SDK disable and sampler
settings. Templates set service name, deployment attributes and the local OTLP/gRPC route. Telemetry
variables are Runner-only unless explicitly included in the harness allowlist. Instrumentation
records protocol/lifecycle metadata without prompts, model content, bodies, credentials or headers.
