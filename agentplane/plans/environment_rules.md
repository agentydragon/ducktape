# The `environment` rule type

Status: **design for review, 2026-11-10; no code.** The decision node is
[`ENVIRONMENT_RULE_TYPE_DESIGN`](task_dag.md#environment_rule_type_design--explicit-environment-lifecycle-rules);
`ENVIRONMENT_LIFECYCLE_RULES` implements it. Settled and not reopened here
([identity plan](identity_and_kubernetes_accounts.md#identity-lifetime), operator 2026-11-10): acting
in a workload is implied for its bound Identity, and so are same-Identity messaging and the wake it
causes; lifecycle control (resume, wake, suspend, delete or rebind an environment as a direct request,
or create Sessions on it) needs an explicit grant. This is that grant: one `PolicySet` rule type,
bound to a subject by the ordinary `PolicyBinding`. Creating environments is
`AGENT_LAUNCH_POLICY_DESIGN`'s. Action boxes are not targets: a box is not a Sandbox and has no
`spec.identity`, and creating or disposing one stays with the Action Service's `sandbox` group under
`action_auto_approve`.

## Schema

`Rule.Type` gains `environment`. It reuses the two fields `session_inbox_access` introduced:
`operations` (proto 11), whose item enum grows, and `targets` (proto 12, `SandboxTargets`), which
gains one field. A Go struct cannot give one JSON key two types, so the per-type subsets are CEL rules
on `Rule`, in the style of the existing ones.

```go
// Operations lists what a session_inbox_access or environment rule allows on its targets.
// +listType=set
// +kubebuilder:validation:MinItems=1
// +kubebuilder:validation:items:Enum=send_message;read;resume;wake;suspend;delete;rebind;create_session
Operations []string `json:"operations,omitempty" proto:"11"`

// SandboxTargets ... The fields combine additively.
// +kubebuilder:validation:XValidation:rule="has(self.sandboxes) || has(self.inboxTags) || has(self.boundIdentity) || (has(self.allSandboxes) && self.allSandboxes)",message="Name a Sandbox, an inbox tag or a bound Identity, or set allSandboxes."
type SandboxTargets struct {
	// ... sandboxes (1), allSandboxes (2), inboxTags (3) unchanged ...
	// BoundIdentity covers every Sandbox whose spec.identity names this Identity when the rule is checked.
	BoundIdentity *IdentityRef `json:"boundIdentity,omitempty" proto:"4"`
}
```

The `Rule` validations change as follows (each `message="Rule fields must match type."`):

```text
# the field chain: environment takes the session_inbox_access branch (no role, hosts, policy, ...)
... : (self.type == 'session_inbox_access' || self.type == 'environment') ? (!has(self.role) && ...) : ...
# operations and targets are required exactly for these two types
self.type in ['session_inbox_access', 'environment'] ? (has(self.operations) && has(self.targets))
  : (!has(self.operations) && !has(self.targets))
# each type's operation vocabulary
self.type != 'session_inbox_access' || self.operations.all(o, o == 'send_message')
self.type != 'environment' || self.operations.all(o, o != 'send_message')
# each type's target vocabulary: no tags or allSandboxes for lifecycle, no boundIdentity for inboxes
!has(self.targets) || (self.type == 'environment'
  ? (!has(self.targets.inboxTags) && !(has(self.targets.allSandboxes) && self.targets.allSandboxes))
  : !has(self.targets.boundIdentity))
# proposed with question 3: delete names its Sandboxes
self.type != 'environment' || !self.operations.exists(o, o == 'delete') || !has(self.targets.boundIdentity)
```

The targets union for `environment` is `sandboxes` and `boundIdentity`, either or both:

- **`sandboxes`**: a Sandbox by name, optionally pinned to one incarnation by UID, matched exactly
  as `session_inbox_access` matches it (`inboxaccess.Rule.covers`).
- **`boundIdentity: {name: I}`**: a Sandbox matches when its current `spec.identity.name` equals `I`,
  as the evaluator's Sandbox informer sees it; nothing records which Sandboxes once matched. The rule pins no
  Identity UID: `spec.identity` is name-only, and the incarnation a Sandbox runs as is the Sandbox's
  record (its current `WorkloadCredential`). A recreated `I` therefore matches the same Sandboxes, as
  it acts in them; the caller's own incarnation is fenced by the reader's subject lookup, which names
  a binding only while the live Identity has the caller's UID. A Sandbox with no `spec.identity`
  matches no `boundIdentity`. `boundIdentity` names an Identity; it selects nothing by label.

Inbox tags and `allSandboxes` are refused: tags address messages, and lifecycle by tag or by
namespace would be selection. Bindings with a ServiceAccount subject grant no `environment` rule;
until `IDENTITY_CUTOVER` removes them, ServiceAccount callers keep today's
`resumePolicy.allowOwnServiceAccount` and configured roles.

## Operations

| Operation        | Allows                                                                                                         | Where it lands                                                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `read`           | `ListEnvironments`, `WatchEnvironments`, `GetEnvironment`, `WatchEnvironment`, `GetEnvironmentIdentityRecord`  | EnvironmentService (implemented RPCs)                                                            |
| `resume`         | `ResumeEnvironment` of an idle-suspended target                                                                | EnvironmentService → lifecycle manager (implemented RPC)                                         |
| `wake`           | the automatic wake for work the caller queued on a target not bound to its Identity                            | Dispatcher delivery → lifecycle manager `RequestWake` (implemented path)                         |
| `suspend`        | `SuspendEnvironment`                                                                                           | EnvironmentService (implemented RPC)                                                             |
| `delete`         | `DeleteEnvironment`                                                                                            | EnvironmentService (implemented RPC)                                                             |
| `rebind`         | `SetEnvironmentIdentity` on a stopped target                                                                   | EnvironmentService (implemented RPC, managers only today)                                        |
| `create_session` | `CreateSession` placed on a target; `AttachEnvironment` of it; with `boundIdentity: I`, a Session running as I | Dispatcher (implemented RPC; Identity admission is the addition), `AttachEnvironment` per #15436 |

Rules for the operations:

- **The rule is the caller's grant; the target keeps its own state gates.** A matching `resume`
  still refuses an operator-requested stop, and `wake` still needs `spec.wakeOnSend` and spends the
  caller's wake budget (`WakeBudgetKey` for the Identity). `resumePolicy` governs only the implied
  path for the Sandbox's own principal, so a rule needs no flag on the target.
- **`wake` versus the implied wake.** The implied wake (`SESSION_IDENTITY_MESSAGING`,
  `identityWakeAssessment`) covers work an Identity queues on a Sandbox bound to it. `wake` is the
  explicit grant for any other target: an orchestrator's `SubmitInput` to a worker Session on a
  suspended worker Sandbox. It is the Identity form of today's `wake_callers` broker role, limited
  to its targets. A cross-Identity send through Notification still needs `session_inbox_access` for
  the send; the wake it causes is the sender's, so it needs `wake` too.
- **`rebind` needs the destination.** Rebinding a target from I to J makes whatever I left in its
  workspace run as J, so the caller also needs the right to run as J: J is its own Identity, or it
  holds `create_session` on `boundIdentity: J`. `SetEnvironmentIdentity`'s implemented refusal,
  while a retained Session on that Sandbox UID names another Identity, applies first.
- **Settings stay with roles.** `SetEnvironmentWakeOnSend` and `SetEnvironmentIdleSuspend` change the
  target's own policy and stay with the configured managers; no operation grants them.

## Evaluation points

All points read one shared index: `policy/reader`'s `Index[R]` with rule type `environment`, a parser
beside the evaluator (as `inboxaccess.ParseRule`), `Grants(reader.Identity{Name, UID})` for the
subject, and a `EnvironmentReady` binding condition through `WatchRules`. EnvironmentService, the
lifecycle manager and Dispatcher share the Runner Gateway process, so the Gateway holds one index and
passes it to each. Reads and list filtering match against the Gateway's Sandbox informer, so a
just-rebound Sandbox may show for one informer lag. Mutations recheck the match against the uncached
Sandbox the lifecycle manager already rereads and patches under its `resourceVersion`, so a rebind
between check and patch is caught there.

| Evaluator                                                 | Caller it evaluates                                                                                    | Reads                                                     |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| EnvironmentService (`runner_gateway/environments/`)       | a direct Identity SVID, or a delegated Identity effective caller (Session delegation, central sampler) | the index; the Sandbox's name, UID and `spec.identity`    |
| Lifecycle manager (`lifecycle.WakeCaller`)                | an `IdentityWakeCaller` from Dispatcher's `wakeCaller(attribution)` in `delivery_wake.go`              | the index after the implied check fails; the Sandbox      |
| Dispatcher `CreateSession`, `AttachEnvironment`           | the Identity effective caller; the Identity the request names                                          | the index; the placed or attached Sandbox                 |
| Action Service (`session_delegation`, `orchestration_v1`) | nothing itself: it forwards the request's caller as the effective caller and the backend evaluates     | `action_auto_approve` only, for the Action request itself |

Platform components stay authorized by role
([delegated authorization](../docs/delegated_authorization.md)): the app and system-test driver as
privileged callers, Notification as a configured wake broker, the Action Service's own account for
`orchestration_v1`'s stopgap. An actor's role never becomes the effective caller's grant, and no
`environment` binding names a component. `ORCHESTRATION_V1_BACKEND_GRANTS` moves each
`orchestration_v1` operation onto this rule: the executor calls as the caller, and a launch's launcher
grant becomes an owned `PolicySet` whose `environment` rule names the new Sandbox by UID with
`resume` and `create_session`, replacing the `argument_schema` auto-approval.

A refusal is `NotFound` where the caller holds no `read` on the target (EnvironmentService does not
confirm existence), else `PermissionDenied`; an automatic wake records
`WAKE_REASON_CALLER_NOT_AUTHORIZED` as today.

## Worked examples

**A multi-agent Identity resumes a sibling.** Identity `research` runs Sessions in Sandboxes
`research-a` and `research-b`, both bound to it; `research-b` idle-suspended.

```yaml
kind: PolicySet
metadata: { name: research-siblings }
spec:
  rules:
    - type: environment
      operations: [read, resume]
      targets: { boundIdentity: { name: research } }
---
kind: PolicyBinding
spec: { subject: { identity: { name: research } }, policySets: [research-siblings] }
```

The Session in `research-a` calls `GetEnvironment(research-b)` then
`ResumeEnvironment(research-b, token)`: the index grants `resume`, the reread shows
`spec.identity: research`, and the manager patches the wake generation. A message to
`research-b`'s Session would have woken it without this rule; the rule is for the direct request.
Additions: Identity admission on the two RPCs and the rule check.

**An orchestrator drives two named workers.** Identity `orchestrator` holds
`operations: [resume, wake, create_session]`, `targets: {sandboxes: [{name: worker-1}, {name: worker-2}]}`;
both workers are bound to Identity `worker`. It resumes `worker-1` and calls `CreateSession` placed
there; the Session runs as `worker`, the Sandbox's Identity, with `orchestrator` recorded as creator.
Its `SubmitInput` to that Session wakes a suspended `worker-2` under `wake`; a message through
Notification also needs `session_inbox_access`. `DeleteEnvironment(worker-1)` is `PermissionDenied`,
and `GetEnvironment` is `NotFound` without `read`. Operations: implemented RPCs; Identity creation
in Dispatcher is the addition `SESSION_SUBAGENT_DELEGATION` builds on.

**No rule.** Identity `scratch` has no `environment` binding. `ResumeEnvironment` on its own suspended
Sandbox is refused; a same-Identity message still wakes it under `allowOwnServiceAccount`.

**A rebind stops a match.** The operator stops `research-b` and rebinds it to `audit`. The next
`ResumeEnvironment(research-b)` from `research` finds `spec.identity: audit` and is `NotFound`; its
list no longer shows it. A rule naming `research-b` directly would still match, which is why a
named target is a grant on that Sandbox whoever it runs as.

**#15436, zero environments.** The app creates nothing; `ci-daily`'s own Session, through the central
sampler as delegating actor, calls `CreateSession(identity: ci-daily, placement: central_sampler)`.
Dispatcher evaluates `create_session` on `boundIdentity: ci-daily` for effective caller
`ci-daily` and admits it. Naming `ops-admin` without that grant on `boundIdentity: ops-admin` is
`PermissionDenied` before any row is written.

**#15436, attach.** `AttachEnvironment(S, target: ci-build)` passes when the caller holds
`create_session` matching `ci-build` and the Sandbox is bound to S's Identity; `DetachEnvironment`
needs only Session write authority.

## Positions on #15436

- **`create_session` on a target is the attach grant: agree.** Attaching and creating a native Session
  there are the same act, putting a Session of the Sandbox's Identity to work in it; attachment adds
  no authority and wakes nothing, and one operation keeps them from diverging.
- **`create_session` on `boundIdentity: I` reads as "run as I": agree, for `boundIdentity` only.**
  Whoever holds it can already create Sessions running as I whenever an environment is bound to I;
  the zero-environment case adds no authority, only removes the need for one to exist. A rule naming
  Sandboxes never implies run-as: it covers those Sandboxes only. The operation's documentation says
  so, and `rebind` uses the same reading for its destination.
- **No Session target.** The rule's nouns are environments; a Session is addressed by UUID and by its
  Identity, and policy cannot name a Session before it exists. Whether an Identity may delete its own
  Sessions is Session authority, decided with reads and inboxes under `SESSION_IDENTITY_AUTHZ`.
  If it needs a grant, the shape is a `delete_session` operation on `boundIdentity: I`, under the same
  run-as reading, with no new target kind. Until then `DeleteSession` stays privileged, as #15436
  proposes.

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

## Questions for the operator

1. **Session target.** Recommendation above: none; Identity-initiated Session deletion is decided
   under `SESSION_IDENTITY_AUTHZ`, as `delete_session` on `boundIdentity` if it needs a grant.
2. **`read` here or implied.** Recommendation: reading environments bound to the caller's own
   Identity is implied, since acting in them already reveals more than the descriptor (the deferred
   branch's self-read); `read` stays in this rule for every other target, such as an orchestrator's
   workers. The alternative keeps every read explicit, so an Identity with no rule cannot list the
   Sandboxes it runs in.
3. **`delete` only by name.** Recommendation: yes, through the CEL rule above. A `boundIdentity`
   target covers environments that do not exist yet, and deletion loses their workspace and state
   volumes; naming them, optionally by UID, makes each grant say what may be lost.
4. **One set per Identity.** `boundIdentity` names a fixed Identity, so the multi-agent set above is
   written per Identity, which the Session-managed Identities of
   `SESSION_IDENTITY_PROVISIONING_DESIGN` would make routine. A "the binding's own subject" form of
   `boundIdentity` would let one set serve many; recommendation: wait until that design shows the need.
