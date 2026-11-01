# Execution isolation and shared resources

The current Codex execution backend is shared at the environment level. This topology is accepted
for now, with the resource and failure coupling described below.

## Shared Codex execution

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

## VM harness placement

The shared Codex app-server runs in its own transient service under `agentplane-workloads.slice`.
Its threads send execution requests to the shared backend described above. Claude instead runs one
native harness process per Session, each in its own transient service under the workload slice;
ordinary Bash children inherit that harness's cgroup. The workload slice's aggregate budget is still
shared across those services.

The workload/executor slice names describe the current placement and budgets. They do not establish
a uniform per-Session resource boundary across harnesses or prescribe the future environment model.
