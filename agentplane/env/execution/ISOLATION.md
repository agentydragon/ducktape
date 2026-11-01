# Execution isolation and shared resources

## Protection goals

- The primary goal is to keep a resident agent harness alive when a command exhausts its memory
  budget. Harnesses need a resource boundary separate from their shells and other tool processes.
- Protecting Execution Service and the native executor server from agents' command memory usage
  and accidental signals is also desirable.
- Isolation between tool calls from different agents sharing an environment is low priority. Those
  agents already share writable files; a separate cgroup for every agent's commands is optional.
- Protecting Environment Runtime's and other Agentplane programs' files from agent writes is a
  lower-priority goal.

These goals allow Runtime, Harness Host and Execution Service management code to share a process.

Resource containment and signal permissions are separate protections. Cgroups constrain resource
usage; they do not prevent an agent from signaling a server running under the same UID. Signal
protection needs appropriate UID/permission or PID namespace boundaries.

## Current Codex sharing

The current Codex execution backend is shared at the environment level. This topology is accepted
for now, with the resource and failure coupling described below.

Execution Service owns one `codex exec-server` for the environment. The shared app-server receives
`CODEX_EXEC_SERVER_URL` for that existing server; opening another Codex thread does not start another
server. Central-sampler execution connections to the same environment also use that backend.

In VMs, `codex exec-server` and its commands share one transient service cgroup under
`agentplane-executors.slice`. In Pods, they share the companion container's resource limits and PID
namespace. CPU, memory and task limits apply collectively across Sessions. Native session IDs,
process handles and working directories do not create separate kernel resource domains.

Individual process signal/terminate operations remain available. Fencing or restarting the backend
affects every Session using it and loses their native execution state. Authored workspace files
survive; prior commands are not automatically replayed.

## Current harness and server protection

| Resident process         | Resource boundary from agent commands                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Shared Codex app-server  | Codex commands have a separate transient service cgroup in VMs and a separate container budget in Pods with the isolated executor enabled. |
| Native Codex exec-server | Shares its transient service cgroup or companion container budget with its commands.                                                       |
| Claude harness           | Direct Bash children share the harness's transient service cgroup in VMs and its container budget in Pods.                                 |

The current Codex placement separates the app-server's memory budget from Codex command memory, while
protection of the native exec-server itself remains a gap. Claude's direct execution does not yet
provide the intended harness-versus-command resource boundary in either environment type.

In VMs, the shared Codex app-server runs under `agentplane-workloads.slice`. Each Claude harness
also has its own transient service there, with its Bash descendants. The workload slice's aggregate
budget is shared across those services. Runtime, Harness Host and Execution Service have separate
service cgroups. In Pods, the runner container also contains Claude and its direct tool processes,
so their memory contributes to that container's limit.

The workload/executor slice names describe the current placement and budgets. They do not establish
a uniform per-Session resource boundary across harnesses or prescribe the future environment model.

## Native Codex sandbox controls

Codex `rust-v0.162.1`, the version used for Agentplane's exec-server protocol adapter, accepts an
optional sandbox policy per execution request. Its Linux bubblewrap backend supports filesystem
permissions, network restrictions and, by default, a separate PID namespace. PID isolation can
protect the server from commands signaling it. The trusted startup option
`--linux-sandbox-pid-namespace=inherit` deliberately removes that protection.

Those controls do not allocate separate cgroup resource budgets to the server and its commands.
Preventing command memory usage from killing the server still requires resource containment around
the command processes.

Agentplane currently configures Codex app-server threads with `danger-full-access`, and its Execution
API adapter omits the sandbox policy for process starts. Native sandbox support therefore does not
provide the protections above in the current configuration.

Upstream references: [Linux sandbox](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/linux-sandbox/README.md)
and [execution request](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server-protocol/src/protocol.rs).

## Native Claude controls

Claude Code has a separate Linux tool-memory feature, available since `2.1.233`:
`CLAUDE_CODE_TOOL_MEMORY_LIMIT=4G`, for example, caps tool commands collectively in a memory cgroup
separate from the harness. This requires a writable, delegated cgroup layout. If setup fails,
Claude continues without the cap and records the failure only in its debug log.

Agentplane does not explicitly enable this cap or delegate cgroups to Claude. Ordinary Pod
containers have read-only cgroupfs; the VM native services also lack delegation. Enabling the
variable alone therefore does not establish harness protection. See the existing
[tool-memory research](../../docs/harness_tool_memory_isolation.md) for the sibling-cgroup
layout and shell-wrapper interception options.

Claude's optional Bash sandbox separately restricts filesystem and network access. Its Linux
bubblewrap backend also isolates process IDs, which can protect the harness from accidental
signals. The shell sandbox does not cover built-in file tools, hooks or MCP servers. Agentplane's
Claude launch does not enable it.

Upstream references: [tool memory cap](https://code.claude.com/docs/en/tools-reference#memory-limit-on-linux-and-wsl),
[Bash sandbox](https://code.claude.com/docs/en/sandboxing) and
[sandbox runtime](https://github.com/anthropics/sandbox-runtime).
