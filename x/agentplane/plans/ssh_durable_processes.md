# Durable SSH-backed processes

Status: **deferred** (`SSHDURABLE` in the task DAG). One-shot commands run through the independent
`ssh-mcp` server (<../../ssh_mcp_server/README.md>) behind the Agentplane MCP Executor; this plan is
the follow-up for processes that must survive an SSH disconnect and be inspected or controlled later.

## Design

Add a small `agentplane-execd` host component to `rugged` and `wyrm2` for
processes that must survive SSH disconnects and be inspected or controlled later. It should be a
root-owned systemd service that delegates process lifetime to the system systemd manager, avoiding
any dependency on user lingering.

The SSH connection remains authenticated as the configured target user. The remote stdio client
(`agentplane-execctl`, whether shipped separately or as a mode of the same binary) sends structured
requests to the root daemon over a local Unix socket. The protocol must not contain a requested user:
the daemon obtains the caller's UID from kernel Unix-socket peer credentials and uses that identity
for every operation. A target user mismatch is therefore impossible to create through request data.

The root daemon exposes only mechanical operations for the caller's own execution namespace:

- `start(execution_id, command, timeout_seconds)` creates a system transient unit named from the
  server-issued Execution ID and runs it with `User=`/`Group=` derived from the SSH peer;
- `status(execution_id)` reads that derived unit's state;
- `read_output(execution_id, cursor, max_bytes)` reads only that unit's bounded journal output;
- `signal(execution_id, signal)` signals the unit's cgroup, never a caller-supplied PID;
- `terminate(execution_id)` applies the reviewed termination sequence and cleanup.

The daemon must reject arbitrary unit names, PIDs, UIDs, systemd properties, forwarding, PTYs, and
unbounded journal queries. `KillMode=control-group` ensures signals reach child processes. The
Action Service remains the authority for Action schemas, approval, caller control rights, durable
Execution state, leases, and reconciliation; the daemon is only a systemd adapter.

The durable process identity is the derived systemd unit name, not an SSH connection and not a PID.
If `start` loses its SSH connection after the remote operation may have begun, Agentplane marks the
Execution `execution_unknown` and later queries the same unit to reconcile it. It never repeats
`start`. An existing unit with the same Execution ID must make a duplicate start fail safely.

This follow-up adds code-owned Actions such as `start`, `status`, `read_output`, `signal`, and
`terminate`; their names and schemas are not supplied by the host ConfigMap. The host component is
deployed through the existing NixOS/Ansible machinery and is initially scoped to `rugged` and
`wyrm2`. Do not add stdin streaming or PTYs until durable process identity, output cursors, signal
authorization, and unknown-outcome reconciliation are proven.

## Acceptance evidence

1. A `start` Action returns a durable Execution while the systemd unit continues after the SSH
   connection closes.
2. `status` finds the same unit after executor and daemon restarts without using a persisted PID.
3. `read_output` returns bounded, resumable output from only the matching unit.
4. `signal(SIGINT)` reaches the original unit's process group and cannot address another user's unit,
   arbitrary systemd units, or a caller-supplied PID.
5. A lost `start` connection yields `execution_unknown`; later status reconciliation resolves the
   original Execution without launching a second unit.
6. Repeating `start` with the same Execution ID does not run the command twice.
7. The stdio protocol contains no requested-user field, and tests prove the daemon uses kernel peer
   credentials as the execution identity.
