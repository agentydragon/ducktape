# The `environment` rule type

Status: **operator-selected design; implementation remains open.** The Identity cutover, #15742
and #15744, is merged with live acceptance recorded in
[`sandbox_identity_cutover_2026_11_12`](../docs/sandbox_identity_cutover_2026_11_12/README.md).
The cutover implements Identity self-read for the four Environment inventory RPCs, but not an
`environment` policy rule: explicit Identity resume and Identity native `CreateSession` remain
refused. The implementation slices are explicit `resume`, followed by native `CreateSession`
`bind_session`. The decision node remains
[`ENVIRONMENT_RULE_TYPE_DESIGN`](task_dag.md#environment_rule_type_design--explicit-environment-lifecycle-rules);
`ENVIRONMENT_LIFECYCLE_RULES` implements it. Settled and not reopened here
([identity plan](identity_and_kubernetes_accounts.md#identity-lifetime), operator 2026-11-10): acting
in a workload is implied for its bound Identity, and so are same-Identity messaging and the wake it
causes; direct lifecycle control (resume, suspend, delete or rebind an environment, or create
Sessions on it) needs an explicit grant. This is that grant: one `PolicySet` rule type, bound to a
subject by the ordinary `PolicyBinding`. Rebinding stays with configured managers for now
([below](#operations)). Creating environments is `AGENT_LAUNCH_POLICY_DESIGN`'s. Action boxes are
not targets: a box is not a Sandbox and has no `spec.identity`, and creating or disposing one stays
with the Action Service's `sandbox` group under `action_auto_approve`.

The complete operation vocabulary below is the selected target shape; the first implementation slice
accepts `resume` only. Add each later operation only with its enforcing consumer.

## Schema

`Rule.Type` gains `environment`. It reuses `operations` (proto 11), whose item enum grows, and adds
`environments` (proto 13): a list of selectors. A Sandbox is a target when it matches every field of
at least one selector. `session_inbox_access` keeps its additive `targets` (`SandboxTargets`, proto
12), and `environment` does not use it: a separate field keeps the conjunction from changing what
existing inbox rules mean. Inbox rules can move to selectors once one needs a conjunction.

```go
// Operations lists what a session_inbox_access or environment rule allows on its targets.
// +listType=set
// +kubebuilder:validation:MinItems=1
// +kubebuilder:validation:items:Enum=send_message;read;resume;suspend;delete;bind_session
Operations []string `json:"operations,omitempty" proto:"11"`

// Environments lists what an environment rule covers: a Sandbox matching every field of at least
// one selector.
// +kubebuilder:validation:MinItems=1
Environments []EnvironmentSelector `json:"environments,omitempty" proto:"13"`

// +kubebuilder:validation:XValidation:rule="has(self.sandbox) || has(self.boundIdentity)",message="Name a Sandbox or a bound Identity."
type EnvironmentSelector struct {
	// Sandbox matches one Sandbox by name, optionally pinned to one incarnation by UID.
	Sandbox *SandboxTarget `json:"sandbox,omitempty" proto:"1"`
	// BoundIdentity matches a Sandbox whose spec.identity names this Identity when the rule is checked.
	BoundIdentity *BoundIdentitySelector `json:"boundIdentity,omitempty" proto:"2"`
}

// +kubebuilder:validation:XValidation:rule="has(self.name) != (has(self.self) && self.self)",message="Set exactly one of name or self."
type BoundIdentitySelector struct {
	Name string `json:"name,omitempty" proto:"1"`
	// Self names the Identity of the binding's subject, so one PolicySet serves every Identity it
	// is bound to.
	Self bool `json:"self,omitempty" proto:"2"`
}
```

The `Rule` validations change as follows (each `message="Rule fields must match type."`):

```text
# the field chain: environment takes the session_inbox_access branch (no role, hosts, policy, ...)
... : (self.type == 'session_inbox_access' || self.type == 'environment') ? (!has(self.role) && ...) : ...
# operations for both types; targets only for inboxes; environments only for lifecycle
self.type in ['session_inbox_access', 'environment'] ? has(self.operations) : !has(self.operations)
self.type == 'session_inbox_access' ? has(self.targets) : !has(self.targets)
self.type == 'environment' ? has(self.environments) : !has(self.environments)
# each type's operation vocabulary
self.type != 'session_inbox_access' || self.operations.all(o, o == 'send_message')
self.type != 'environment' || self.operations.all(o, o != 'send_message')
# bind_session on a named Sandbox also names the Identity it must be bound to
self.type != 'environment' || !self.operations.exists(o, o == 'bind_session')
  || self.environments.all(e, !has(e.sandbox) || has(e.boundIdentity))
# settled (operator, 2026-11-11): delete names its Sandboxes
self.type != 'environment' || !self.operations.exists(o, o == 'delete')
  || self.environments.all(e, has(e.sandbox))
```

A selector's fields:

- **`sandbox`**: a Sandbox by name, optionally pinned to one incarnation by UID, matched exactly
  as `session_inbox_access` matches it (`inboxaccess.Rule.covers`).
- **`boundIdentity: {name: I}`**: a Sandbox matches when its current `spec.identity.name` equals
  `I`, as the evaluator sees it; nothing records which Sandboxes once matched. `spec.identity` is
  name-only. The caller's Identity subject is fenced by the policy reader against the live Identity
  UID; target checks use the current Sandbox spec and live Identity, not a Sandbox WorkloadCredential
  reissue gate. A Sandbox with no `spec.identity` matches no `boundIdentity`. `boundIdentity` names
  an Identity; it selects nothing by label.
- **`boundIdentity: {self: true}`**: as `{name: I}`, with `I` the Identity the binding's subject
  names, resolved per subject when the index computes its grants. It lets one PolicySet, such as
  "bind your own Sessions to your own environments", serve every Identity it is bound to.
- **`sandbox` and `boundIdentity` together**: that Sandbox, while it is bound to that Identity.

**Why `bind_session` on a named Sandbox names its Identity.** A Session bound to an environment runs
as the environment's Identity ([below](#operations)). A grant on `sandbox: worker-1` alone would
allow work as whatever Identity `worker-1` is bound to when checked: written while it runs as
`worker`, it would allow Sessions as `audit` after a rebind. Pinning the UID does not help, since a
rebind keeps it. Requiring `boundIdentity` makes a rebind void the grant instead of widening it.
Other operations on a named Sandbox create no work as a new Identity and keep the name-only form.

No selector matches by inbox tag, by label, by namespace or by creator: tags address messages,
lifecycle by namespace or label would be selection, and a creator never changes, so a grant keyed
on it could not be revoked or expire (operator, 2026-11-11). Bindings with a ServiceAccount subject
grant no `environment` rule. ServiceAccount callers retain their separately configured component
roles; `resumePolicy.allowOwnServiceAccount` gates automatic wake of the Sandbox's bound Identity,
not explicit resume.

## Operations

| Operation      | Intended authority                                                                                            | Current code / planned consumer                                                                                                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`         | `ListEnvironments`, `WatchEnvironments`, `GetEnvironment`, `WatchEnvironment`, `GetEnvironmentIdentityRecord` | The first four Identity self-read RPCs are already implemented; the identity-record RPC remains ServiceAccount-only today, with the selected future `read` semantics retained for when it is grant-backed. |
| `resume`       | `ResumeEnvironment` of an idle-suspended target                                                               | Existing RPC is ServiceAccount/operator-only; explicit Identity grant is the first implementation slice.                                                                                                   |
| `suspend`      | `SuspendEnvironment`                                                                                          | Existing ServiceAccount manager path; outside this implementation scope.                                                                                                                                   |
| `delete`       | `DeleteEnvironment`                                                                                           | Existing ServiceAccount manager path; outside this implementation scope.                                                                                                                                   |
| `bind_session` | Native `CreateSession` creating a Session–environment binding; future attachment if specified                 | Existing RPC rejects Identity callers; add the grant with its native Dispatcher consumer in slice two. No attachment RPC exists today.                                                                     |

Rules for the operations:

- **The rule is the caller's grant; the target keeps its own state gates.** A matching `resume`
  still refuses an operator-requested stop. `resumePolicy` governs only the implied
  path for the Sandbox's own principal, so a rule needs no flag on the target.
- **Automatic wake is not an operation here** (operator, 2026-11-11). `resume` is an explicit
  request to an environment. Whether delivering Session work wakes a suspended environment is a
  different layer: the sender's right to deliver is already a grant (`session_inbox_access` or
  same-Identity messaging), and whether delivery starts the Sandbox is the recipient's choice, a
  property of the Sandbox (today's `spec.wakeOnSend`) or of the Session, bounded by wake budgets.
  The wake paths stay as they are today: the implied same-Identity wake
  (`identityWakeAssessment`) and the `wake_callers` broker role. Decomposing them is deferred to
  [`WAKE_ON_DELIVERY_DESIGN`](task_dag.md#wake_on_delivery_design--whether-delivering-session-work-wakes-its-environment).
- **Rebinding is deferred** (operator, 2026-11-10). `SetEnvironmentIdentity` stays with the
  configured managers (the app for the operator); no agent use needs it yet. If an operation is added,
  it must also require the destination: rebinding from I to J makes whatever I left in the workspace
  run as J, so the caller needs the right to create Sessions as J.
- **Binding across Identities is deferred.** A later rule may allow a Session–environment binding
  where the Session's Identity differs from the environment's. It would come in
  [`CROSS_IDENTITY_SESSION_BINDING`](task_dag.md#cross_identity_session_binding--binding-a-session-to-another-identitys-environment), with its own model of what each side may see and do; until then step 3 of
  [admitting a binding](#operations) holds without exception.
- **Reading your own environments is implied** (question 2): an Identity reads the environments
  bound to it without a rule, and `read` grants reading any other target.
- **Settings stay with roles.** `SetEnvironmentWakeOnSend` and `SetEnvironmentIdleSuspend` change the
  target's own policy and stay with the configured managers; no operation grants them.
- **`bind_session` grants a relation, not a Session.** A Session–environment binding is its own
  entity, owned by Dispatcher: a native Session has one, fixed at creation; a central Session has
  #15436's attachments, each with its own ID, desired state and revision. `bind_session` lets the
  caller create such a binding to a target. A Session bound to an environment runs as its Identity,
  so binding to a target bound to J creates work as J; that is why the operation needs a grant.
  Removing a binding narrows and needs only write authority on the Session. Creating a Session
  with no binding is not an environment operation ([question 5](#questions-for-the-operator)).
- **Admitting a binding.** Dispatcher admits a Session–environment binding of Session S to
  environment E when all of these hold:
  1. The caller has authority over S: implied when the caller's Identity is S's Identity. Across
     Identities it is refused; that authority is deferred to
     [`SESSION_CROSS_IDENTITY_AUTHORITY`](task_dag.md#session_cross_identity_authority--acting-on-another-identitys-sessions). At a
     native `CreateSession`, S does not exist yet and this step does not apply.
  2. A `bind_session` rule of the caller's matches E.
  3. S's Identity is E's bound Identity. This is the existing Session–environment invariant, not a
     creator–environment identity requirement: native `CreateSession` takes the new Session's
     Identity from the placed Sandbox, delivery refuses a mismatch, and the [session model](session_environment_model.md) requires it of attachments. No
     grant overrides it. Rules allowing a binding across Identities may come later
     ([`CROSS_IDENTITY_SESSION_BINDING`](task_dag.md#cross_identity_session_binding--binding-a-session-to-another-identitys-environment)); this rule type does not express them.
  4. E's own state allows it: no operator stop or pending delete, and the incarnation is current.
     There is no limit on how many Sessions bind to one environment (operator, 2026-11-11): a
     binding costs nothing, and the runtime bounds the work it runs when it starts it.
- **A Session's Identity never changes.** No operation rebinds a Session to another Identity: its
  history was produced as that Identity, and later turns would run as the new one with the old
  one's context. The way across is a new Session as the other Identity. Moving a central Session to
  another environment of the same Identity is a detach and an attach, checked as above.
- **Dispatcher only.** Dispatcher owns which Sessions and bindings exist: it admits them, takes
  commands, and later materializes a native Session by calling the Environment Runtime's or the
  sampler's `CreateSession`. No grant of any type lets an agent call
  a runtime's `CreateSession` directly (operator, 2026-11-10): the Gateway admits only Dispatcher
  on that method (`runner_gateway/internal/gateway/gateway.go`), and a Session an agent wants goes through
  Dispatcher. Making the runtime's gRPC surface unmistakably about native Sessions (its
  `CreateSession` starts one Dispatcher already admitted) is deferred to
  [`ENVIRONMENT_RUNTIME_NATIVE_SURFACE`](task_dag.md#environment_runtime_native_surface--an-environment-runtime-surface-unmistakably-about-native-sessions).

## Evaluation points

Current state: `Rule.Type` has no `environment`; `operations` and `targets` are used by
`session_inbox_access`. The reusable `policy/reader.Index[R]` and `WatchRules` can supply a shared
Identity name+UID index and watch readiness, but there is no environment parser, index or
`EnvironmentReady` condition yet. EnvironmentService, the lifecycle manager and Dispatcher share
the Runner Gateway process, so the planned implementation uses one index and passes it to each.

All proposed grant points read that shared index: `policy/reader`'s `Index[R]` with an environment
parser beside the evaluator (as `inboxaccess.ParseRule`), `Grants(reader.Identity{Name, UID})` for
the subject, and an `EnvironmentReady` binding condition through `WatchRules`.

The watched policy view may lag Kubernetes events. Re-evaluate the grant on every Sandbox
UID/resourceVersion conflict retry and fail closed when the existing reader/watchhealth readiness
contract is unavailable. Sandbox UID/RV fencing does not atomically serialize a PolicyBinding or
Identity change with a Sandbox patch; document the accepted observation bound and error behavior,
without global locking or a promise of instantaneous revocation.

| Evaluator                                                 | Caller it evaluates                                                                                                                     | Reads                                                                                                      |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| EnvironmentService (`runner_gateway/environments/`)       | direct Identity or delegated Identity effective caller for existing reads; Identity admission is a planned addition for explicit resume | current self-read checks Sandbox name/UID/spec binding and live Identity; planned resume adds shared index |
| Dispatcher `CreateSession`                                | existing Identity admission is followed by an explicit Identity rejection; grant is planned for new environment placement               | planned shared index plus exact selected Sandbox and its current bound Identity                            |
| Action Service (`session_delegation`, `orchestration_v1`) | nothing itself: it forwards the request's caller as the effective caller and the backend evaluates                                      | `action_auto_approve` only, for the Action request itself                                                  |

Platform components stay authorized by role
([delegated authorization](../docs/delegated_authorization.md)): the app and system-test driver as
privileged callers, Notification as a configured wake broker, the Action Service's own account for
`orchestration_v1`'s stopgap. An actor's role never becomes the effective caller's grant, and no
`environment` binding names a component. `ORCHESTRATION_V1_BACKEND_GRANTS` moves each
`orchestration_v1` operation onto this rule: the executor calls as the caller, and a launch's launcher
grant becomes an owned `PolicySet` whose `environment` rule names the new Sandbox by UID, with its
`boundIdentity`, for `resume` and `bind_session`, replacing the `argument_schema` auto-approval.

A refusal is `NotFound` where the caller can't read the target, by a `read` rule or because it is
bound to the caller's Identity (EnvironmentService does not
confirm existence), else `PermissionDenied`.

## Implementation sequence and checks

1. Add the environment RuleType, selector/CEL validation, parser and shared typed reader for
   `resume` only, then wire explicit Identity resume through EnvironmentService and lifecycle. Keep
   the direct Pod-bound operator path. Add a typed Identity grant eligibility reason and expose its
   resume precondition only while currently authorized. Do not restore
   `ALLOWED_OWN_SERVICE_ACCOUNT`.
2. Add `bind_session` to the accepted operation vocabulary with its selector constraints and native
   Dispatcher consumer in the same dependent PR. Reuse the same index. Permit only a new
   environment-hosted Identity creation with a matching grant; preserve ServiceAccount privileges,
   receipt/idempotency and identity-change admission behavior. Do not add AttachEnvironment. P2
   depends on P1; if P1 is still open, stack it using native GitHub Stacks.

Implementation validation is scoped to the changed code:

- Run these Go suites with `bbr test`:
  `//agentplane/policy/reader:reader_test`,
  `//agentplane/runner_gateway/environments:environments_test`,
  `//agentplane/runner_gateway/environments/lifecycle:lifecycle_test`, and
  `//agentplane/runner_gateway/dispatcher:store_integration_test`.
- Build generated policy API targets with `bbr build`:
  `//agentplane/policy/api/v1alpha1:generated_crds` and
  `//agentplane/policy/api/v1alpha1:policy_types_proto`.

Then run relevant Agentplane system tests and include staging authorization rollback evidence. Extend existing tests for Identity subject name+UID/expiry/watch
readiness, grant-scoped resume and stale preconditions, rebind during native placement, caller vs
actor and creator vs target execution Identity, and exact receipt recovery. Retain existing tests for
self-read, direct operator requested-stop resume, incomplete current-generation idle stopping
(no budget or mutation before Stopped), conflicts, target UID, shared budget, unknown patch reply/no
replay, `wake_callers` automatic-only and Identity automatic wake. This planning refresh itself needs
no tests.

## Worked examples

**A multi-agent Identity resumes a sibling.** Identity `research` runs Sessions in Sandboxes
`research-a` and `research-b`, both bound to it; `research-b` idle-suspended.

```yaml
kind: PolicySet
metadata: { name: own-environments }
spec:
  rules:
    - type: environment
      operations: [resume, bind_session]
      environments:
        - boundIdentity: { self: true }
---
kind: PolicyBinding
spec: { subject: { identity: { name: research } }, policySets: [own-environments] }
```

The Session in `research-a` calls `GetEnvironment(research-b)` then
`ResumeEnvironment(research-b, token)`: the index resolves `self` to `research` and grants `resume`, the reread shows
`spec.identity: research`, and the manager patches the wake generation. A message to
`research-b`'s Session would have woken it without this rule; the rule is for the direct request.
The same set, bound to other Identities, serves each of them. `GetEnvironment` Identity admission
already exists; the additions are Identity admission for explicit `ResumeEnvironment` and the rule
check.

**An orchestrator drives two named workers.** Identity `orchestrator` holds
`operations: [resume, bind_session]` on
`environments: [{sandbox: {name: worker-1}, boundIdentity: {name: worker}}, {sandbox: {name: worker-2}, boundIdentity: {name: worker}}]`;
both workers are bound to Identity `worker`. It resumes `worker-1` and calls `CreateSession` placed
there; the Session runs as `worker`, the Sandbox's Identity, with `orchestrator` recorded as creator.
Sending input to that Session is acting on another Identity's Session, deferred to
`SESSION_CROSS_IDENTITY_AUTHORITY`; whether it wakes a suspended worker is
`WAKE_ON_DELIVERY_DESIGN`'s. `DeleteEnvironment(worker-1)` is `PermissionDenied`,
and `GetEnvironment` is `NotFound` without `read`. Operations: implemented RPCs; Identity creation
in Dispatcher is the addition `SESSION_SUBAGENT_DELEGATION` builds on.

**No rule.** Identity `scratch` has no `environment` binding. `ResumeEnvironment` on its own suspended
Sandbox is refused; a same-Identity message still wakes it only when the existing
`resumePolicy.allowOwnServiceAccount` setting permits automatic wake.

**A rebind stops a match.** The operator stops `research-b` and rebinds it to `audit`. The next
`ResumeEnvironment(research-b)` from `research` finds `spec.identity: audit` and is `NotFound`; its
list no longer shows it. A `resume` rule naming `research-b` alone would still match, a grant on
that Sandbox whoever it runs as. A `bind_session` rule must also name `boundIdentity: research`, so
it stops matching and no Session as `audit` can be created through it.

**#15436, zero environments.** `ci-daily`'s own Session, through the central sampler as delegating
actor, calls `CreateSession(identity: ci-daily, placement: central_sampler)`. No binding is created,
so no `environment` rule applies. It passes when `ci-daily` holds a `session` rule with `create` on
`identities: [{self: true}]` ([question 5](#questions-for-the-operator)); without one it is
`PermissionDenied`. The app, as a privileged caller, can still create it.

**#15436, future attach (no RPC today).** `ci-daily` holds `bind_session` on
`environments: [{sandbox: {name: ci-build}, boundIdentity: {name: ci-daily}}]`.
`AttachEnvironment(S, target: ci-build)` passes when the caller's Identity is S's (`ci-daily`), the
rule matches, and `ci-build` is bound to `ci-daily`. The same call on a Session of another Identity
fails step 1, and on a Sandbox bound to another Identity fails step 3. `DetachEnvironment` needs only
authority over S.

## Positions on #15436

- **One operation is the attach grant and the native placement grant: agree, named `bind_session`.**
  Both create a Session–environment binding. Attaching adds no authority and wakes nothing, and one
  operation keeps the two from diverging.
- **`boundIdentity: I` read as "run as I": disagree.** A Session with no environment has no
  binding, so creating one is not an operation on an environment. Reading it into this rule would
  make an environment grant mean something with no environment in it. It is a `session` rule's
  `create` instead (question 5).
- **No Session target.** The rule's nouns are environments; a Session is addressed by UUID and by its
  Identity, and policy cannot name a Session before it exists. Whether an Identity may delete its own
  Sessions is Session authority, decided with reads and inboxes under `SESSION_IDENTITY_AUTHZ`,
  alongside who may create them. Until then `DeleteSession` stays privileged, as #15436 proposes.

## Alternatives rejected

- **Named targets only.** Cannot express the multi-agent case: a standing Identity whose Sessions
  start Sandboxes over time would need a rule edit per Sandbox, and the launch would have to write
  that grant, which is `AGENT_LAUNCH_POLICY_DESIGN`'s problem leaking into every resume.
- **Derivation from acting-as** (`resumePolicy.allowOwnIdentity`, closed #15380). It put the grant
  on the target: a flag in the Sandbox's spec, set by whoever writes the Sandbox, given to whichever
  Identity it names, and outside the bindings, so no expiry, no audit through `PolicyBinding` and no
  way to grant one Identity lifecycle over another's environment. It also made lifecycle implied by
  acting, which the operator ruled out. Its EnvironmentService self-read and per-Identity wake
  budget carry over; its CRD flag and resume reason do not.

- **Creator-based selectors** (`createdBy: self`, operator 2026-11-11). "The environments I
  launched" reads naturally for an orchestrator, but a creator never changes: such a grant cannot be
  revoked, expire, or pass to someone else, and a recreated Identity of the same name makes its
  meaning unclear. A launch writes an owned PolicySet naming the new Sandbox instead. Dispatcher's
  recorded Session creator stays audit data.

## Questions for the operator

1. **Session target.** Recommendation above: none; Identity-initiated Session deletion is decided
   under `SESSION_IDENTITY_AUTHZ`.
2. **Reading your own environments: settled, implied** (operator, 2026-11-11). An Identity may
   read the environments bound to it without a rule, since acting in them already reveals more than
   the descriptor and this self-read is already implemented. `read` stays in this rule for every other
   target, such as an orchestrator's workers.
3. **`delete` only by name: settled yes** (operator, 2026-11-11). A `boundIdentity` target covers
   environments that do not exist yet, and deletion loses their workspace and state volumes; naming
   them, optionally by UID, makes each grant say what may be lost. The CEL rule in
   [Schema](#schema) enforces this.
4. **One set per Identity: settled, `boundIdentity: {self: true}`** (operator, 2026-11-11). A
   fixed `boundIdentity` would need the multi-agent set written per Identity, which the
   Session-managed Identities of `SESSION_IDENTITY_PROVISIONING_DESIGN` would make routine. `self`
   lets one set serve every Identity it is bound to ([Schema](#schema)).
5. **Creating a Session as an Identity, with no environment: settled, an explicit grant**
   (operator, 2026-11-11). Not implied even for a caller creating a Session as its own Identity, to
   stay conservative. The smallest shape is a `session` rule type whose only operation is `create`,
   targeted by Identity:

   ```yaml
   - type: session
     operations: [create]
     identities:
       - self: true # the binding's own subject, as boundIdentity: {self: true}
   ```

   Only `self` is accepted at first; creating a Session as another Identity with no environment
   waits for the deferred cross-Identity node. A native placement needs no `session` rule: its
   `bind_session` on the target is the grant. The rule type itself is built under
   `SESSION_SUBAGENT_DELEGATION`; until it exists, only privileged callers create a Session with no
   binding.
