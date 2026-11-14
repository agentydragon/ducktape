# Environment policy rollout

Ship the PolicySet CRD and Runner Gateway implementation before creating an `environment` rule.
The Gateway must have `get/list/watch` on PolicySets, PolicyBindings and Identities, and `patch` on
PolicyBinding status. Confirm the new Gateway image is pinned, its policy watches are healthy,
and a fixture binding reports `EnvironmentReady`. Resume and native Session placement construct
independent readers from the same evaluator library. Confirm both watches are healthy before using
`bind_session`; its schema and enforcing Session consumer must ship together after the resume slice.
These source changes introduce no standing grant.

## Reader compatibility

The staging pins inspected on 2026-11-14 were Action Service, egress, MCP edge, Notification Service
and Policy Controller at `b4885e5317f0e46d8d2a596f055642b6dc20bb79`, and Runner Gateway at
`55e7df4226dd4d761a72b96ea055a362c953e3ec`. The corresponding live Deployments were Ready.

| Reader               | Unknown `Rule.Type` behavior at those pins                           |
| -------------------- | -------------------------------------------------------------------- |
| Action Service       | Shared typed index filters to `action_auto_approve` before parsing.  |
| Egress               | Shared typed index filters to `egress` before parsing.               |
| Notification Service | Shared typed index filters to `session_inbox_access` before parsing. |
| Policy Controller    | Kubernetes role resolution ignores non-Kubernetes rule types.        |
| MCP edge             | Watches Identities; does not decode PolicySets.                      |
| Runner Gateway       | Does not watch PolicySets yet; cannot enforce the new grant.         |

The typed Kubernetes decoder is non-strict: `Rule.Type` is a string, and unknown selector fields
are ignored. Thus these existing readers ignore an `environment` rule without rejecting the other
rules in its PolicySet. The shared filter is in `policy/reader/index.go` (`parseSet`); each consumer
supplies its own rule type. The old CRD's enum rejects `environment` at admission. Expanding that
schema alone supplies no enforcing consumer.

## Rollback

Before rolling back Gateway or narrowing the CRD, remove `environment` rules and their dedicated
bindings. Preserve other rules and bindings in mixed PolicySets. Confirm no persisted PolicySet
still uses the removed operation, then revert the relevant Gateway pin and CRD change. Once Flux
applies it, confirm existing inventory self-reads, direct configured operator resume, automatic wake
and the other readers' readiness conditions. No Session, Sandbox, Identity, workspace or History
needs deletion. Rolling back the Gateway with grants still present removes explicit grant-backed
authority; it does not turn it into ServiceAccount or implied Identity authority.

Policy decisions use the reader's observed view. Each mutation retries Sandbox UID/resourceVersion
conflicts with fresh authorization, but does not atomically serialize PolicyBinding changes with
Sandbox patches. A healthy watch may lag events; failed or unsynchronized watches refuse new
grant-backed mutations as `UNAVAILABLE`. Credential revalidation retains its normal cache bound.

The native Session slice adds nullable creator/actor/delegation attribution columns through the
normal Gateway startup migration. Existing writers can overlap the new schema. Rollback keeps
those columns and all retained Session rows: first remove `bind_session` rules (or remove just that
operation from combined rules), then revert the service image and narrow the operation enum.
An old service refuses new Identity creation; it does not reinterpret retained Identity creators as
ServiceAccount authority. While rolled back, a cross-Identity creator may lose its new create-receipt
recovery path, but the Session retains the target's execution Identity and its existing access rules.
Check exact CreateSession retry, execution Identity fencing, automatic wake and retained History
before and after the roll. No down migration or replay of an uncertain creation is required.
