# Headscale: Single Replica Only (No HA)

**Date**: 2026-03-07
**Status**: Permanent architectural constraint

## Summary

Headscale cannot run with multiple replicas. It is architected as a single-process
application with extensive in-memory state that serves as the authoritative data plane.
The database is used for persistence/durability only, not as a shared coordination layer.

We previously ran 2 replicas and hit OIDC split-brain — this was the expected symptom
of a deeper architectural limitation.

## In-Memory State Inventory

All references are to the Headscale source at `/code/github.com/juanfont/headscale`.

### Critical (would cause immediate breakage)

| Component          | Source                                 | Description                                                                                                                                                                                                         |
| ------------------ | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| NodeStore          | `hscontrol/state/node_store.go`        | Copy-on-write `atomic.Pointer[Snapshot]` of ALL nodes. Map responses are built from this in-memory snapshot, not from the database. Writes serialized through a single goroutine channel.                           |
| LockFreeBatcher    | `hscontrol/mapper/batcher_lockfree.go` | Tracks all active long-poll connections (`multiChannelNodeConn`), connected nodes, and pending map update changesets. A node connected to replica A would never receive updates triggered on replica B.             |
| OIDC state         | `hscontrol/oidc.go:57`                 | `zcache.Cache` mapping OAuth `state` strings to `RegistrationInfo` (PKCE verifier, registration ID). If the OIDC callback hits a different replica than the initial redirect, auth fails — the state doesn't exist. |
| Registration cache | `hscontrol/state/state.go:56`          | `zcache.Cache` with 15-min expiry for in-progress node registrations. Contains a `Registered chan struct{}` that signals OIDC completion — purely in-process.                                                       |

### High (would cause subtle correctness issues)

| Component     | Source                        | Description                                                                                                                                  |
| ------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| PrimaryRoutes | `hscontrol/routes/primary.go` | In-memory struct with mutex tracking which node is primary for each subnet route prefix. Different replicas would elect different primaries. |
| IP Allocator  | `hscontrol/db/ip.go`          | Sequential IP allocation with in-memory state. Concurrent replicas could assign the same IP to different nodes.                              |

### Medium (would cause operational issues)

| Component     | Source                           | Description                                                                                                                                                                        |
| ------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| EphemeralGC   | referenced in `hscontrol/app.go` | Schedules cleanup of ephemeral nodes after disconnect. Timers are in-process — if a node disconnects from replica A and reconnects to B, A's timer would still fire and delete it. |
| DERPMap       | `hscontrol/state/state.go`       | `atomic.Pointer[tailcfg.DERPMap]`, periodically refreshed. Minor — each replica fetches independently.                                                                             |
| PolicyManager | `hscontrol/policy/`              | In-memory ACL policy evaluation. Loaded from config/DB, no cross-instance sync.                                                                                                    |

## OIDC Flow (Why It Breaks)

1. `RegisterHandler()` generates random `state` + `nonce`, stores `RegistrationInfo` in
   in-memory cache keyed by `state` (replica A)
2. Sets `state` as HTTP cookie, redirects to OIDC provider (Authentik)
3. Provider redirects back to `OIDCCallbackHandler()` — load balancer may route to replica B
4. Replica B calls `getRegistrationIDFromState()` → cache miss → **auth fails**
5. Even with sticky sessions, the `Registered` channel signal is in-process — the node's
   long-poll connection on a different replica would never receive it

## PostgreSQL Backend

PostgreSQL is supported (`database.type: postgres` in config) but explicitly "highly
discouraged" (config comments, `docs/about/faq.md` lines 108-121). It's in maintenance
mode — bugs fixed but no active development. Even with PostgreSQL as a shared database,
multi-replica still fails because the DB is not used as a coordination layer.

## What Would Be Required for HA

Making Headscale multi-replica would require:

- Replacing NodeStore with a shared coordination layer (Redis, NATS, or DB-backed event bus)
- Externalizing OIDC state and registration cache to a shared store
- Adding distributed locking for IP allocation
- Adding a consensus protocol for primary route election
- Replacing in-process channels with a pub/sub system for map update notifications

This is essentially a rewrite of the core control plane. The project describes itself as
implementing "a single Tailscale network, suitable for personal use" — HA is not a goal.

## Recommendation

Run Headscale at 1 replica with:

- Liveness/readiness probes for fast restart on failure
- `local-path` storage (already done — VPS-resilient)
- Pod disruption budget of 0 (prevent voluntary eviction during maintenance)

CNPG does not help — the bottleneck is in-process state, not the database.
