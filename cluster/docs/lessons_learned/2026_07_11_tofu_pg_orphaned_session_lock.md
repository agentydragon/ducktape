# tofu PG Backend Lock Survives Runner Node Reboot

**Date**: 2026-07-11
**Status**: Recovered manually; prevention pending

## Summary

A `wyrm2` reboot killed several tofu-controller runner pods. The controller itself also
wedged on a dead runner RPC (see
<2026_07_03_tofu_controller_runner_rpc_hang.md>) and had to be restarted. After that
restart, `Terraform/flux-system/sso-providers` repeatedly failed to acquire its PG backend
state lock, blocking this Flux dependency chain:

```text
sso-providers-tf -> forgejo -> haku-state -> haku-console
```

Image automation had already selected and committed a new `haku-console` image, but the
Deployment stayed on the old image until the dependency chain recovered.

The PG backend did not contain a durable lock row. The lock was a PostgreSQL
session-scoped advisory lock held by an orphaned server backend. The runner pod and its
node-side TCP state were gone, but PostgreSQL had received neither FIN nor RST and still
considered the idle TCP connection alive.

## Smoking Gun

Querying the **current CNPG primary** showed the orphaned session:

```text
pid=343808
client_addr=10.244.5.135
state=idle
wait_event=ClientRead
query=SELECT data FROM "sso_providers"."states" WHERE name = $1
locktype=advisory
classid=0
objid=177
granted=true
connected since 2026-07-11 08:37Z
```

No pod still owned `10.244.5.135`. Terminating only that PostgreSQL backend immediately
released the lock:

```sql
SELECT pg_terminate_backend(343808);
```

`Terraform/sso-providers` then reached `Ready=True`; `forgejo`, `haku-state`, and
`haku-console` followed. The console Deployment rolled successfully to
`devel-20260711101943-459cd26` with `2/2` replicas available.

## Why It Did Not Recover Quickly

PostgreSQL was configured to inherit the host TCP keepalive defaults:

```text
tcp_keepalives_idle = 0                 # use kernel default
tcp_keepalives_interval = 0             # use kernel default
tcp_keepalives_count = 0                # use kernel default
client_connection_check_interval = 0   # disabled

net.ipv4.tcp_keepalive_time = 7200      # first probe after 2 hours
net.ipv4.tcp_keepalive_intvl = 75
net.ipv4.tcp_keepalive_probes = 9
```

Worst-case dead-peer detection was therefore about 2 hours 11 minutes. The orphan was
about 1 hour 46 minutes old when diagnosed, so the kernel had not sent its first keepalive
probe. PG advisory locks do auto-release when their owning database session ends; the
mistake was assuming runner-pod disappearance implies prompt database-session death.

## Recovery-State Matrix

| State                                                                                                | Automatic recovery                                                               | Required action                                                                                   |
| ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Runner exits gracefully and closes PostgreSQL connection                                             | Immediate                                                                        | None                                                                                              |
| Runner process/pod dies and the server receives FIN/RST                                              | Immediate                                                                        | None                                                                                              |
| Runner node reboots or disappears without FIN/RST while its connection is idle                       | Eventually, after TCP keepalive failure; about 2h 11m with the observed defaults | Terminate the confirmed orphaned PostgreSQL backend, or wait for keepalive expiry                 |
| tofu-controller reconcile is parked in deadline-free gRPC `Init` after runner loss                   | Never on v0.16.1                                                                 | Restart tofu-controller; track upstream PR `flux-iac/tofu-controller#1838`                        |
| A new runner retries with `lockTimeout: 0s` while the orphan exists                                  | Never succeeds before the holder disappears; churns every retry interval         | Remove the orphaned DB session; a bounded `lockTimeout` reduces churn but cannot evict the holder |
| `tfstate.forceUnlock` targets the lock while another PostgreSQL session still owns the advisory lock | Does not solve the owning session                                                | Identify and terminate the orphaned DB backend first                                              |
| Flux Kustomizations depend on the unhealthy Terraform/Kustomization                                  | No progress until dependency is Ready                                            | Recover the root Terraform object; then let Flux unwind the dependency chain                      |
| Existing Deployment is healthy while its Kustomization is dependency-blocked                         | Keeps serving the old image indefinitely                                         | Recover the dependency chain; verify desired policy tag equals Deployment image afterward         |

## Safe Diagnosis

First find the current primary; `pg_stat_activity` and session locks are local to each
PostgreSQL server, so querying a replica produces a misleading empty result:

```bash
kubectl -n tofu-state get cluster tofu-state-db-ovh \
  -o jsonpath='{.status.currentPrimary}{"\n"}'
```

Then inspect advisory-lock owners on that primary:

```sql
SELECT
  a.pid,
  a.client_addr,
  a.state,
  a.wait_event_type,
  a.wait_event,
  a.query_start,
  a.query,
  l.classid,
  l.objid,
  l.granted
FROM pg_stat_activity AS a
JOIN pg_locks AS l ON l.pid = a.pid
WHERE l.locktype = 'advisory';
```

Before terminating anything, prove that the client IP no longer belongs to a live runner
and that the lock maps to the affected backend state. Terminate only the confirmed
orphaned PID; do not restart CNPG or kill all `tfstate` sessions.

## Prevention

- Configure materially shorter TCP keepalive/dead-client detection for the tofu-state
  PostgreSQL cluster so a vanished runner cannot retain an advisory lock for two hours.
- Give tofu-controller Terraform CRs a bounded `spec.tfstate.lockTimeout` to avoid rapid
  retry churn during legitimate short overlaps. This is mitigation, not orphan eviction.
- Land or carry `flux-iac/tofu-controller#1838` so runner loss cannot also park the
  reconcile goroutine forever.
- After any runner-node reboot, check both stuck Terraform reconciles and advisory locks
  on the **current CNPG primary**.
