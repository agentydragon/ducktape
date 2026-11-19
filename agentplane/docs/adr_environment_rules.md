# Environment rules: lifecycle grants to Identities

Acting in a workload is implied for its bound Identity, and so are same-Identity messaging and the
wake it causes; direct lifecycle control over an environment needs an explicit grant (operator,
2026-11-10). That grant is the `environment` rule type of a `PolicySet`
(`policy/api/v1alpha1/types.go`), bound to an Identity by an ordinary `PolicyBinding` and evaluated
by `environments/access`. It accepts two operations: `resume`, an explicit `ResumeEnvironment`, and
`bind_session`, creating a Session–environment binding (today a native `CreateSession`). Rollout
order and rollback are in [environment policy rollout](environment_policy_rollout.md).

```yaml
kind: PolicySet
metadata: { name: own-environments }
spec:
  rules:
    - type: environment
      operations: [resume, bind_session]
      environments:
        - boundIdentity: { self: true }
```

## Decisions

- **Selectors.** A rule covers a Sandbox matching every field of at least one selector, checked
  against the current Sandbox on every lookup:
  - `sandbox`: by name, optionally pinned to one incarnation by UID;
  - `boundIdentity: {name: I}`: its current `spec.identity.name` is `I`; a Sandbox with no
    `spec.identity` matches none;
  - `boundIdentity: {self: true}`: as `{name: I}` with `I` the caller's Identity, so one set serves
    every Identity it is bound to (operator, 2026-11-11);
  - both together: that Sandbox while it is bound to that Identity.
- **`bind_session` on a named Sandbox also names its Identity** (CEL and the parser enforce it). A
  Session bound to an environment runs as the environment's Identity. A grant on `sandbox: worker-1`
  alone, written while it runs as `worker`, would allow Sessions as `audit` after a rebind; pinning
  the UID does not help, since a rebind keeps it. Naming the Identity makes a rebind void the grant
  instead of widening it.
- **No selector by inbox tag, label, namespace or creator** (operator, 2026-11-11). Tags address
  messages, lifecycle by namespace or label would be selection, and a creator never changes, so a
  grant keyed on it could not be revoked or expire. A launch that wants its creator to manage the new
  Sandbox writes an owned `PolicySet` naming it; the Dispatcher's recorded creator stays audit data.
- **Identity callers only.** A binding with a ServiceAccount subject grants nothing here;
  ServiceAccount callers keep their configured component roles, and
  `resumePolicy.allowOwnServiceAccount` gates automatic wake, not explicit resume. An actor's role
  never becomes the effective caller's grant ([delegated authorization](delegated_authorization.md)).
- **The rule is the caller's grant; the target keeps its own state gates.** A matching `resume`
  still refuses an operator-requested stop, and `bind_session` still refuses a deleting, stopped or
  operator-stopped Sandbox.
- **Automatic wake is not an operation** (operator, 2026-11-11). Whether delivering Session work
  wakes a suspended environment is the recipient's choice (`spec.wakeOnSend`), bounded by wake
  budgets; the implied same-Identity wake and the `wake_service_callers` broker role stay as they are.
- **Acting in a workload is implied for its bound Identity**: exec in a Sandbox or box, reading its
  files, interacting with Sessions running as that Identity. A harness-hosted Session already
  executes commands inside the Sandbox it runs in, as that Sandbox's Identity; any Session of
  Identity I executing in any Sandbox bound to I is the transitive closure of that, and grants
  nothing I does not already have.
- **Reading your own environments is implied** (operator, 2026-11-11): an Identity reads the
  environments bound to it without a rule.
- **Settings and rebinding stay with configured managers.** `SetEnvironmentWakeOnSend`,
  `SetEnvironmentIdleSuspend` and `SetEnvironmentIdentity` change the target's own policy; no
  operation grants them (rebinding deferred by the operator, 2026-11-10). A future rebind operation
  must also require the destination: rebinding from I to J makes whatever I left in the workspace run
  as J.
- **`bind_session` grants a relation, not a Session.** The Dispatcher admits a binding of Session S
  to environment E when the caller's Identity is S's (not applicable at a native `CreateSession`,
  where S does not exist yet), a `bind_session` rule of the caller's matches E, S's Identity is E's
  bound Identity, and E's own state allows it. No grant overrides the Identity match: a native
  `CreateSession` takes the new Session's Identity from the placed Sandbox. There is no limit on
  Sessions per environment (operator, 2026-11-11).
- **A Session's Identity never changes.** Its history was produced as that Identity; the way across
  is a new Session as the other Identity.
- **Dispatcher only** (operator, 2026-11-10). No grant lets an agent call a runtime's
  `CreateSession`; Environments' Runtime proxy admits only Sessions on that method.

## Evaluation

EnvironmentService (explicit resume) and the Dispatcher (native `CreateSession`) each build an
independent index from `environments/access` and keep the `EnvironmentReady` binding condition.
Policy decisions use the watched view: an unsynchronized or failed watch refuses as `UNAVAILABLE`,
and Sandbox UID/resourceVersion conflict retries re-authorize. A refused target the caller cannot
read is `NOT_FOUND`; one bound to the caller's own Identity is `PERMISSION_DENIED`.

## Rejected alternatives

- **Named targets only.** A standing Identity whose Sessions start Sandboxes over time would need a
  rule edit per Sandbox, and every launch would have to write that grant.
- **Derivation from acting-as** (`resumePolicy.allowOwnIdentity`, closed in #15380). It put the
  grant on the target: a flag in the Sandbox's spec, outside the bindings, so no expiry, no audit
  through `PolicyBinding` and no way to grant one Identity lifecycle over another's environment.
- **Creator-based selectors** (`createdBy: self`): see the selector decision above.
