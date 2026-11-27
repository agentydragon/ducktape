# Execution isolation and shared resources

For proposed command placement and configurable resource boundaries, see the
[execution placement design](docs/execution_placement_design.md).

## Protection goals

- The primary goal is to keep a resident agent harness alive when a command exhausts its memory
  budget. Harnesses need a resource boundary separate from their shells and other tool processes.
- Protecting Execution Service and the native executor server from agents' command memory usage
  and accidental signals is also desirable.
- Isolation between tool calls from different agents sharing an environment is low priority. Those
  agents already share writable files; a separate cgroup for every agent's commands is optional.
- Protecting Environment Runtime's and other Agentplane programs' files from agent writes is a
  lower-priority goal.

These goals allow Runtime and Execution Service management code to share a process.

Resource containment and signal permissions are separate protections. Cgroups constrain resource
usage; they do not prevent an agent from signaling a server running under the same UID. Signal
protection needs appropriate UID/permission or PID namespace boundaries.

## Shared executor

The shared Codex app-server connects to the environment's executor through `CODEX_EXEC_SERVER_URL`.
Central sampler execution connections use the same backend. Native session IDs, process handles
and working directories do not create separate kernel resource domains.

Pod commands share the companion's resource limits and PID namespace. In VMs, direct command
placement shares the executor service cgroup. The separate systemd placement puts native
commands in their own transient units under an aggregate budget.
[Pod and VM placement](docs/codex_executor.md) documents the topology.

Fencing or restarting affects every Session using that backend and loses its execution state.
Individual process terminate and signal operations remain available. Authored workspace files
survive. Runtime never replays commands.

## Current harness and server protection

| Resident process         | Resource boundary from agent commands                                                                                                   |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| Shared Codex app-server  | A configured executor gives Codex commands a separate budget in both Pods and VMs.                                                      |
| Native execution backend | Shares its resource budget with directly placed commands. Only VM systemd command placement gives commands a separate aggregate budget. |
| Claude harness           | Direct Bash children share the harness's transient service cgroup in VMs and its container budget in Pods.                              |

Where an isolated executor is configured, its budget protects the app-server from command memory pressure.
The backend remains exposed to direct-placement commands. Claude's direct execution does not yet
provide the intended harness-versus-command resource boundary in either environment type.

In VMs, the shared Codex app-server runs under `agentplane-workloads.slice`. Each Claude harness
also has its own transient service there, with its Bash descendants. The workload slice's aggregate
budget is shared across those services. Runtime and Execution Service share one
management service cgroup. In Pods, the runner container also contains Claude and its direct tool processes,
so their memory contributes to that container's limit.

The workload/executor slice names describe the current placement and budgets. They do not establish
a uniform per-Session resource boundary across harnesses or prescribe the future environment model.

## Codex sandbox controls

The native backend's Codex compatibility endpoint rejects requests containing a sandbox policy; it does not
implement Codex's Linux bubblewrap sandbox. Agentplane's app-server uses `danger-full-access`
and its typed Execution API does not request that policy. Processes therefore rely on the
execution container or VM scope and their existing resource limits. In particular, a command can
still exhaust the shared execution budget and kill the backend.

## Native Claude controls

Claude Code has a separate Linux tool-memory feature, available since `2.1.233`:
`CLAUDE_CODE_TOOL_MEMORY_LIMIT=4G`, for example, caps tool commands collectively in a memory cgroup
separate from the harness. This requires a writable, delegated cgroup layout. If setup fails,
Claude continues without the cap and records the failure only in its debug log.

Agentplane does not explicitly enable this cap or delegate cgroups to Claude. Ordinary Pod
containers have read-only cgroupfs; the VM native services also lack delegation. Enabling the
variable alone therefore does not establish harness protection. See the existing
[tool-memory research](../../../docs/harness_tool_memory_isolation.md) for the sibling-cgroup
layout and shell-wrapper interception options.

Claude's optional Bash sandbox separately restricts filesystem and network access. Its Linux
bubblewrap backend also isolates process IDs, which can protect the harness from accidental
signals. The shell sandbox does not cover built-in file tools, hooks or MCP servers. Agentplane's
Claude launch does not enable it.

Upstream references: [tool memory cap](https://code.claude.com/docs/en/tools-reference#memory-limit-on-linux-and-wsl),
[Bash sandbox](https://code.claude.com/docs/en/sandboxing) and
[sandbox runtime](https://github.com/anthropics/sandbox-runtime).
