# Credentials across Sandbox suspension

Code-path audit, 2026-10-15; no cluster state was inspected. Resume does not
refresh app-created grants or upstream credentials.

- The Sandbox Controller creates its configured `PolicyBinding` from
  `spec.policySets`; it has no expiry and remains owned by the Sandbox.
- App-created bindings are additive and may have `expiresAt`. The Policy
  Controller deletes an expired binding and then removes its native RBAC grants
  on reconcile. Resume does not recreate or extend one. Egress readers use the
  extant binding and do not enforce a strict wall-clock expiry, so controller
  and watch delay may extend authorization past the configured deadline.
- Pod-bound projected tokens rotate and the sidecar rereads them for each
  request. Resume creates a new Pod; VM egress relays receive a projected token
  in the new launcher Pod. Secret-backed upstream credential lifetime remains
  with the Secret's writer; Agentplane has no refresh contract for it.

The deterministic tests cover binding creation/expiry, egress denial without a
binding, token-file rotation and resumed Pod templates. They do not establish a
strict reader-side expiry cutoff or live-token behavior.

**Decision remains open:** should an app-created runtime grant be an optional
capability or a prerequisite for a useful resumed Sandbox? Separately, should
readers enforce `expiresAt` against wall-clock time, or is controller deletion
the intended boundary? Under either choice, resume must not silently renew or
recreate authority. See the `RESUME_CREDENTIALS_VALID` node in
[`task_dag.md`](../plans/task_dag.md).
