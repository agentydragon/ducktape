# SSH Executor for Agentplane

Status: **implementation in progress**, replacing the earlier `HOSTEXEC`/hostexecd adapter
direction. Layer 1 is a separate bearer-protected SSH MCP server; the existing Agentplane MCP
Executor connects to it. The backend is the independent `ssh-mcp` service in
<../../ssh_mcp_server/README.md>, also consumed by Haku Console; it is not an Agentplane
component. This does not alter the existing Haku Console hostexec implementation.

## Outcome

An approved Agentplane Action can run one non-interactive command as a configured Unix user on a
configured machine over ordinary SSH, using a private key held by Kubernetes. The Action Service
records the existing Action/Execution lifecycle and a bounded terminal result. A lost connection
never causes a blind retry: the result is `execution_unknown` when Agentplane cannot establish
whether the remote command ran or completed.

The executor is a transport and credential-selection layer. It does **not** decide which commands
are allowed. The existing decider/Decision layer remains responsible for human or policy approval
of the complete Action, including the command, machine, user, and any key-selection input.

## Layer 1: one-shot SSH MCP backend (P0 behavior)

- Add a standalone SSH MCP server pod and connect it through Agentplane's existing MCP Executor
  binding. The SSH server owns SSH keys and OpenSSH execution; the Action Service remains the
  durable Action/Decision authority and holds only the shared backend bearer.
- Define and register the executor's `list_targets` and `exec` Actions, including their input
  schemas and descriptions, in executor code. Configuration selects the executor and supplies
  deployment data; it does not define or override the Action catalog contract.
- Execute through the OpenSSH client rather than inventing a remote command protocol.
- Provide a read-only introspection Action such as `list_targets` so an Agent can discover which
  machine/user pairs have registered SSH credentials before requesting execution. This is an
  Action, not a projection of hidden executor configuration, and therefore remains auditable and
  subject to the existing decider/human approval path.
- Support non-interactive command execution only; no PTY, shell session, forwarding, or interactive
  stdin in the first slice.
- Let `exec` request a per-Execution timeout, defaulting to the configured maximum and never
  exceeding it. The timeout is part of the approved Action arguments and is enforced by the SSH
  executor as an execution bound, not as a command-policy decision.
- Capture bounded stdout and stderr, with explicit connect, execution, and output limits.
- Preserve Action Service exactly-once dispatch and lease semantics: one claimed Execution may cause
  one SSH invocation, and a lost lease or disconnected executor must not retry it.
- Return safe, bounded terminal errors for connection/auth/exit failures. If the SSH process or
  broker disappears after a command may have started, return or retain `execution_unknown`.
- Record which configured SSH key binding, host, Unix user, and command outcome were used as
  redacted provenance. Never persist or project private-key material.

`list_targets` returns only the stable target availability needed for selection, for example
`host` and `user` (and, if needed for explicit selection, a reviewed non-secret key identifier).
It must not return Secret names, mounted paths, private-key contents, fingerprints, filesystem
metadata, or other credential-bearing configuration. It must not probe the remote hosts merely to
answer the inventory question: registration in the reviewed ConfigMap is the source of availability.

The first implementation should exercise the real SSH process seam with a local test SSH server or
an equivalent deterministic fixture. A test that only mocks the entire SSH client is insufficient.

This layer intentionally does not require a host daemon. The SSH MCP server opens one SSH connection
for the MCP call, runs the command, collects the bounded terminal result, and closes the connection.
Durable process control is a separate follow-up below.

## Kubernetes configuration

Private keys are Kubernetes Secrets. A reviewed ConfigMap YAML maps each key to the machine and
Unix user for which it may be used. The mapping is routing metadata, not a command allowlist.
Conceptually:

```yaml
keys:
  - id: wyrm2-coder
    secret:
      name: ssh-mcp-keys
      key: wyrm2-coder
    host: wyrm2.example
    user: coder
  - id: rugged-coder
    secret:
      name: ssh-mcp-keys
      key: rugged-coder
    host: rugged.example
    user: coder
```

The exact schema follows the deployment's existing ESO/Secret-controller conventions. The SSH MCP
pod receives the mounted private-key files; the Action Service receives only the shared bearer file.
Keys never enter PostgreSQL, Action payloads, logs, or transcripts. The implementation validates at
startup that every configured `(host, user)` tuple is unique; missing individual key files leave
targets present but unavailable.

The first deployment may use long-lived keys, as explicitly accepted for this slice. Key rotation
is a deployment concern: update the Secret and roll/reload the Action Service so no stale key
material remains in the running process. Terraform/GitOps/SOPS or an external-secrets controller
may own rotation and distribution; do not make Agentplane a second secret-management authority.

`known_hosts` is configuration-owned and must be mounted alongside the keys. Strict host-key
checking is required. Unknown or changed host keys fail closed; the executor must not accept a
caller-supplied host-key policy.

## Request and selection contract

The executor receives an already-approved immutable `ExecutionRequest`. Its action arguments identify
the target machine and Unix user, and may identify one configured key mapping when more than one key
is valid for that pair. The executor performs only the mechanical lookup and SSH invocation:

- reject a missing or ambiguous host/user/key mapping;
- reject a key whose configured host/user does not match the request;
- use the exact approved command arguments without applying a second command policy;
- resolve the key file and `known_hosts` from process configuration;
- invoke OpenSSH with forwarding and PTY disabled.

The decider must bind approval to the complete target and command. This prevents a caller from
reusing an approval for `wyrm2/coder` against `rugged/root`, while keeping command authorization out
of the SSH layer as requested.

The SSH executor owns the stable Action names and schemas: `list_targets` is the inventory read and
`exec` accepts the target tuple, command, and optional bounded timeout. The reviewed settings file contains only the
executor binding and SSH target/transport configuration; it cannot add arbitrary SSH Actions, change
their schemas, or turn a configuration entry into an unreviewed execution surface.

`exec.timeout_seconds`, when supplied, must be a positive number no greater than the configured
`command_timeout_seconds`; omission uses that configured maximum. A timeout after the remote command
may have started is an `execution_unknown` outcome, never an automatic retry.

The introspection result is derived from the same validated in-process configuration used for
execution. Every structurally valid configured target remains listed. Its `available` value and
bounded error reflect whether the referenced key file is currently present/readable; a missing key
does not remove the target from inventory. `list_targets` does not disclose secret names, paths, or
other credential-bearing details.

## Credential and process boundary

Prefer the boring first implementation: OpenSSH reads a mounted key file with restrictive
permissions. Evaluate an SSH-agent sidecar only if it materially improves rotation or prevents key
material from being readable by the Action Service process. An agent is not automatically better:
its socket becomes a bearer for every loaded key, so the socket must be private to the executor,
never mounted into the Action caller or general workload, and its key inventory must remain
configuration-controlled.

A later migration may use short-lived SSH certificates or an external signer. That is not required
for the initial long-lived-key slice. Whichever mechanism is selected, the Action Service must not
become the durable authority for issuing credentials.

## Layer 2: durable remote processes (future follow-up)

Once Layer 1 is proven, add a small `agentplane-execd` host component to `rugged` and `wyrm2` for
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

1. A reviewed Action reaches the SSH executor and runs exactly once on `wyrm2` as `coder` with the
   configured key; the durable Execution contains bounded output and redacted target/key provenance.
2. The same configuration works for `rugged`, while a mismatched host/user/key selection fails
   before SSH invocation.
3. Unknown and changed host keys fail closed.
4. Oversized stdout/stderr is truncated or rejected according to the documented bound and never
   escapes through an error or transcript.
5. SSH authentication failure and non-zero remote exit are terminal failures with safe bounded
   error codes.
6. Disconnecting the executor after SSH may have started produces `execution_unknown`; recovery
   never blindly starts the command again.
7. A duplicate dispatch/claim cannot create two SSH invocations.
8. Rotating a Kubernetes Secret and rolling the executor causes subsequent Actions to use the new
   key without exposing either key in Action state or logs.
9. A real staging run proves the existing decider/human approval path authorizes the full target and
   command, while the SSH executor itself performs no command allowlist check.
10. A reviewed introspection Action lists the configured `wyrm2/coder` and `rugged/coder` targets
    without exposing Secret names, key paths, fingerprints, or private-key material, and does not
    initiate network connections to either host.
11. `exec` accepts a shorter timeout, defaults it when omitted, rejects a timeout above the
    configured maximum, and reports a timed-out command as `execution_unknown` once it may have
    started remotely.

## Durable-process acceptance (future)

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
