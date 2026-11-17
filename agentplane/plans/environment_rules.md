# The `environment` rule type: remaining operations

The rule type, its selectors and the `resume` and `bind_session` operations are built; their
decisions are in [environment rules](../docs/adr_environment_rules.md). This plan holds the selected
operations not built yet, Session creation without an environment, and the positions on #15436.
Creating environments is `AGENT_LAUNCH_POLICY_DESIGN`'s. Action boxes are not targets: a box is not a
Sandbox and has no `spec.identity`, and creating or disposing one stays with the Action Service's
`sandbox` group under `action_auto_approve`.

## Remaining operations

| Operation | Intended authority                                                                            | Current code                                                                                                       |
| --------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `read`    | the four inventory RPCs and `GetEnvironmentIdentityRecord` on targets not bound to the caller | Identity self-read of the four inventory RPCs is implied and built; the identity-record RPC is ServiceAccount-only |
| `suspend` | `SuspendEnvironment`                                                                          | ServiceAccount manager path                                                                                        |
| `delete`  | `DeleteEnvironment`                                                                           | ServiceAccount manager path                                                                                        |

The operation enum grows to these, and the schema adds:

```text
# environment operations never include the inbox vocabulary
self.type != 'environment' || self.operations.all(o, o != 'send_message')
# settled (operator, 2026-11-11): delete names its Sandboxes
self.type != 'environment' || !self.operations.exists(o, o == 'delete')
  || self.environments.all(e, has(e.sandbox))
```

`delete` only by name is settled (operator, 2026-11-11): a `boundIdentity` target covers
environments that do not exist yet, and deletion loses their workspace and state volumes; naming
them, optionally by UID, makes each grant say what may be lost.

Each refusal keeps the built rule: `NotFound` where the caller cannot read the target, else
`PermissionDenied`, so `read` also decides which refusals reveal a target.

## Deferred elsewhere

- **Binding across Identities:**
  [`CROSS_IDENTITY_SESSION_BINDING`](task_dag.md#cross_identity_session_binding--binding-a-session-to-another-identitys-environment).
- **Acting on another Identity's Session**, the first step of admitting a binding:
  [`SESSION_CROSS_IDENTITY_AUTHORITY`](task_dag.md#session_cross_identity_authority--acting-on-another-identitys-sessions).
- **A Runtime surface about native Sessions only:**
  [`ENVIRONMENT_RUNTIME_NATIVE_SURFACE`](task_dag.md#environment_runtime_native_surface--an-environment-runtime-surface-unmistakably-about-native-sessions).
- **`orchestration_v1` on these grants:** `ORCHESTRATION_V1_BACKEND_GRANTS` makes its executor call
  as the caller; a launch's launcher grant becomes an owned `PolicySet` whose `environment` rule names
  the new Sandbox by UID, with its `boundIdentity`, for `resume` and `bind_session`, replacing the
  `argument_schema` auto-approval.

An orchestrator holding `resume` and `bind_session` on
`[{sandbox: {name: worker-1}, boundIdentity: {name: worker}}]` can resume `worker-1` and create a
Session there, which runs as `worker` with the orchestrator recorded as creator. Sending input to that
Session is `SESSION_CROSS_IDENTITY_AUTHORITY`'s; whether it wakes a suspended worker is
`WAKE_ON_DELIVERY_DESIGN`'s. Identity creation in the Dispatcher is what
`SESSION_SUBAGENT_DELEGATION` builds on.

## Creating a Session with no environment

Settled (operator, 2026-11-11): an explicit grant, not implied even for a caller creating a Session
as its own Identity. The smallest shape is a `session` rule type whose only operation is `create`,
targeted by Identity:

```yaml
- type: session
  operations: [create]
  identities:
    - self: true # the binding's own subject, as boundIdentity: {self: true}
```

Only `self` is accepted at first; creating a Session as another Identity with no environment waits
for the deferred cross-Identity node. A native placement needs no `session` rule: its `bind_session`
on the target is the grant. The rule type is built under `SESSION_SUBAGENT_DELEGATION`; until it
exists, only privileged callers create a Session with no binding.

## Positions on #15436

- **One operation is the attach grant and the native placement grant: agree, named `bind_session`.**
  Both create a Session–environment binding. Attaching adds no authority and wakes nothing, and one
  operation keeps the two from diverging.
- **`boundIdentity: I` read as "run as I": disagree.** A Session with no environment has no
  binding, so creating one is not an operation on an environment. It is a `session` rule's `create`
  instead.
- **No Session target.** The rule's nouns are environments; a Session is addressed by UUID and by its
  Identity, and policy cannot name a Session before it exists. Whether an Identity may delete its own
  Sessions is Session authority, decided with reads and inboxes under `SESSION_IDENTITY_AUTHZ`,
  alongside who may create them. Until then `DeleteSession` stays privileged, as #15436 proposes.

Under #15436, `ci-daily`'s own Session calls `CreateSession(identity: ci-daily, placement:
central_sampler)`: no binding, so no `environment` rule applies, and it passes only with the `session`
rule above. A future `AttachEnvironment(S, target: ci-build)` (no RPC today) passes when the caller's
Identity is S's, a `bind_session` rule matches `ci-build`, and `ci-build` is bound to S's Identity;
`DetachEnvironment` needs only authority over S.

## Question for the operator

**Session target.** Recommendation above: none; Identity-initiated Session deletion is decided under
`SESSION_IDENTITY_AUTHZ`.
