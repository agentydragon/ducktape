# Platform Sessions Flux ownership

The `ducktape-flux/platform-sessions` Kustomization owns the shared namespace, legacy
PVCs, public Service and route, and caller bearer Secrets. The
`ducktape-flux/platform-sessions-remote` Kustomization depends on it and owns the
separate gateway and provider workloads, internal credentials, provider certificates, and
provider PVCs. Both use `deletionPolicy: Orphan`; provider PVCs also have pruning
disabled. Preserve the PVCs and bearer Secret identities during any ownership change.

For the legacy service and its retained PVC, see
[Platform Sessions remote deployment](platform_sessions_remote.md#legacy-service).
