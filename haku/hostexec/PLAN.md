# hostexec — outbound node execution daemons

`hostexec` lets Haku request shell commands on operator machines (`wyrm2`, `rugged`, …).
Every call goes through the console's ordinary MCP approval queue and executes under the
approving operator's own Authentik authority. There is no auto-approved host execution path.

## Architecture

The node initiates every connection:

```text
approved hostexec tool call
  -> console exchanges the operator login token for a short-lived per-host token
  -> console writes a pending execution to Postgres
  -> hostexecd heartbeats and long-polls haku-console over outbound HTTPS
  -> hostexecd claims the execution with a lease
  -> hostexecd verifies the embedded operator token and its host/run_as authority
  -> hostexecd executes, renews its lease, and submits an idempotent result
  -> console completes the waiting tool call
```

`hostexecd` has no listener or host port. Roaming and off-mesh nodes therefore need only
ordinary outbound HTTPS access to the public console origin.

## Two separate credentials

The protocol deliberately separates routing identity from execution authority:

- A random per-daemon bearer authenticates heartbeat, claim, lease-renewal, and result
  submission. The console stores/compares only its SHA-256 fingerprint. Possession lets a
  daemon receive work addressed to that daemon id; it does not authorize a command.
- Each execution carries the approving operator's short-lived, single-use Authentik token.
  `hostexecd` verifies the per-provider issuer and JWKS, `aud=hostexec-<host>`, expiry,
  replay, and the `hostexec-<run_as>-<host>` group before executing.

Compromise of a daemon routing bearer therefore does not create standalone root authority.
The operator token remains the load-bearing authorization boundary for every command.

## Durable broker

Postgres is authoritative across console replicas and restarts:

- `node_daemon_presence` records daemon instance id, version, advertised backends,
  capacity, connection time, and most recent heartbeat.
- `node_daemon_executions` records the payload, enum lifecycle state, dispatch deadline,
  claim instance, hashed lease token, lease deadline, and terminal result/error.
- Claims use `FOR UPDATE SKIP LOCKED`, so concurrent console replicas cannot dispatch the
  same execution twice.
- Pending executions fail after the dispatch deadline. Claimed executions fail closed
  when their lease expires or a replacement daemon instance appears; the error explicitly
  says that the execution outcome is unknown.
- Result submission is idempotent for an identical terminal result and rejects a conflicting
  second outcome.

Process-local events reduce long-poll latency, but claim and result paths poll Postgres as
the source of truth. Correctness does not depend on a request reaching the replica that
enqueued the work.

## Presence state

The operator Settings panel polls `GET /api/node-daemons` every ten seconds. Its state is a
derived enum:

- `connected`: a recent heartbeat and no active execution;
- `busy`: a recent heartbeat with a claimed execution;
- `stale`: heartbeat older than the connected threshold but not yet the offline threshold;
- `offline`: no heartbeat or one older than the offline threshold.

The panel also shows version, advertised backends, last heartbeat, and active execution id.
These are observations, not authorization signals.

## Machine API

All machine endpoints require the per-daemon bearer:

- `POST /api/node-daemons/v1/heartbeat`
- `POST /api/node-daemons/v1/work/claim`
- `POST /api/node-daemons/v1/executions/{id}/heartbeat`
- `POST /api/node-daemons/v1/executions/{id}/result`

The bearer selects the daemon id; callers cannot choose another daemon in the request body.
Instance ids prevent an old daemon process from continuing after a replacement process has
started, and opaque per-execution lease tokens bind renewal/result submission to a claim.

## Configuration ownership

- `cluster/k8s/haku/console/config.yaml` owns daemon ids, display names, allowed backends,
  token environment slots, and host-to-daemon routing.
- `cluster/k8s/haku/console/node-daemon-*.sops.yaml` owns one encrypted routing bearer per
  node. Each file is decryptable by both the cluster secret controller and that NixOS host.
- `nix/nixos/modules/hostexecd.nix` owns the outbound daemon service and reads the same SOPS
  file through sops-nix.
- `tf/gitops/agent-machine-access/hostexec.tf` owns the per-host Authentik provider and
  execution-authority groups. Routing bearer provisioning does not replace this authority.

## Security and failure semantics

- `bash` is never in the unconditional auto-approval policy.
- `cmd` is bash script text, run as `bash -c cmd` — full shell semantics apply (pipes,
  redirects, globs, quoting, `$VAR` expansion). This is intentional: the approving operator
  sees the exact script text before it runs, and the operator-approval gate (not argv
  restriction) is what bounds what a call can do.
- `hostexecd` runs as root only so it can switch to the authorized `run_as` user.
- Operator tokens, daemon bearers, and lease tokens are never logged or returned by the
  operator status API.
- Disconnection before claim is a fast tool error. Loss of a claimed execution is reported
  as outcome unknown; it is never silently retried because the original command may have run.
- The daemon uses exponential reconnect backoff and continues heartbeating while a command
  runs.

## Deployment order

1. Apply Alembic revision `0015` and deploy console configuration/secrets.
2. Roll the console so the machine API and durable broker are available.
3. Rebuild `wyrm2` and `rugged`; their daemons begin outbound heartbeats.
4. Confirm both nodes are `connected` in Settings before approving a hostexec call.

The console may safely deploy before the daemons: configured nodes appear offline and calls
fail closed until a valid heartbeat arrives.
