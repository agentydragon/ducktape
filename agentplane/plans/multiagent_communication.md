# Cross-agent communication and authorization: design space

Status: **messaging shipped (2026-10-21); transcript reads, discovery and launch open.** The send
rule (`session_inbox_access`), inbox tags, peer messages, receipts and notices are the Notification
Service's contract ([API](../notification_service/docs/api.md#messages)). This plan keeps the agreed
requirements and the open design: the read rule and the
[archive split](#option-authorize-the-archive-apart-from-live-objects), discovery, Session creation
and Sandbox launch. Gates are in the [task DAG](task_dag.md#3-multiagent-decisions-before-implementations);
the decisions still open are summarized under [cases to resolve](#cases-to-resolve-before-implementation).
The operator may revisit the service-enforced rule direction if implementation does not work well.

## Requirements and preferences

- Agents with different access must be able to collaborate across separate Sandboxes. Useful
  workflows include a private coordinator assigning public-data work to specialists, peers within
  a project, and standing roles such as project manager and individual contributor. These are
  configurable workflow examples, not Agentplane agent categories or required topology.
- Static configuration should be able to express reusable rules such as “this project's manager
  may perform these operations on its contributors.” GitOps and Kubernetes are plausible places
  to author that configuration; the tentative evaluator direction is recorded below.
- Sending and transcript reads are independent, directed permissions: `send(X, Y)`,
  `read_history(X, Y)` and `read_history(Y, X)` imply none of one another. A reply or correlation
  reference confers no permission. A named role may explicitly bundle grants without making those
  implications intrinsic to messaging.
- OAuth-connected clients can be callers for permitted sends and reads through their verified bound
  Kubernetes SA, from an Action box ([OAuth callers](#oauth-callers)); preserve
  namespace/name assignment for static Flux-managed SAs. V1 recipients are
  Sandbox-hosted Sessions. Externally running clients have no receiving mechanism; external inboxes
  are [explicitly deferred](task_freezer.md#external_agent_inboxes--inboxes-for-externally-running-agents).
- Communication should be an additive, optional capability. Running a harness, using Actions or
  reading history should not require deploying the entire communication system. The integration
  app remains a client of independently usable backends ([service boundaries](../docs/service_boundaries.md)).
- Low-regret choices keep identity, roles, execution placement, authority and delivery separable.
  Multiagent work builds on the current SA and policy foundation, preserving the
  [live permission bundle semantics](#live-permission-bundles). Broader auth changes may be useful,
  but their evaluation and adoption are separate from implementing multiagent capabilities.
- A communication classifier or trusted-model judge is a possible **later** addition, outside v1
  and not a messaging prerequisite. File attachments are also unnecessary for v1.
- HTTP, gRPC, MCP and CLI facades are lower-priority, replaceable choices. They do not need to be
  selected before the authority and message contracts. A2A's domain semantics deserve a separate
  evaluation from its transport bindings.

## Topology-neutral authorization

**Agreed with the operator, 2026-10-16.** Agentplane should authorize concrete operations, such as
sending peer correspondence into an inbox or reading a Session's retained
history. Multiagent topology belongs in runtime and/or GitOps configuration. Agentplane must not
require teams, projects, a management hierarchy, or coordinator/specialist categories. Configuration
may use such concepts without making them mandatory backend entities or assigning them built-in
privileges.

The authorization model should accommodate direct resource grants, peers, overlapping groups and
multiple management relationships without selecting one arrangement for every deployment. For any
configured relationship such as “X belongs to Y,” policy must define its exact authorization effect:
which subjects may perform which operations on which resources, and who may establish or change the
relationship. Creation provenance, management and parentage are separate facts; any grants based on
them must be explicit policy.

Evaluate each proposed abstraction by asking what useful configurations it makes impossible and
what exact operations it permits or denies. Impose structural constraints only where an actual
security or execution requirement needs them. The existing Sandbox isolation and persistence
boundaries below are such constraints.

**Caller attribution agreed with the operator, 2026-10-16:** Sandbox/ServiceAccount-level caller
attribution is sufficient for v1, as for existing Kubernetes and Action permissions. Authenticated
per-Session sender identity is not required. For Sandbox-hosted Sessions, the minimum authorization
target is their Sandbox; v1 has no individual-Session grants. Exact schemas and administrative RBAC
scopes remain open; replacing the broader authorization machinery is a separate decision.

**App auth scope agreed:** the app's existing auth path may remain a special case for v1.
Reducing that special handling is an optional follow-up within the
[broader auth review](external_access.md#compatibility-evaluation-github-kubernetes-http), rather
than a multiagent prerequisite; no replacement app-auth path is selected.

### Target collections and launch enrollment

Permissions apply to a Sandbox directly, through a tag on the Sandbox, or through an `allSandboxes`
target. Individual Session IDs remain addresses for delivery and reads; they do not select a
narrower permission boundary. A tag such as `developers` has no built-in organizational meaning. A
bundle can grant read/send to that tag and be assigned to any SA, including one otherwise given
different capabilities. Independent per-SA grants can add rights on unrelated Sandboxes or
Kubernetes resources, optionally temporarily. Removing a tag ends the grants derived from it;
independent overlapping grants remain.

**`allSandboxes` target agreed with the operator, 2026-10-17:** a rule may target every Sandbox in the
namespace of the PolicySet holding it, never the whole cluster; the Sandbox Controller likewise
reconciles a single namespace. A Sandbox in another namespace is out of v1's reach, since direct
targets are same-namespace too (settled with the operator, 2026-10-18). The target
covers later Sandboxes without tags, and the backends match it on the recorded Sandbox namespace,
so it keeps working after a Sandbox is deleted. It grants only the operations its rule lists:
read-any does not imply send-any.

**Collections are tags on the Sandbox, agreed with the operator, 2026-10-19 to 2026-10-20 UTC:**
set-typed string lists in `Sandbox.spec`, with no membership or collection objects
([design](sandbox_tags.md)). Inbox tags gate sends and go with the CR; history tags gate history
reads, and History keeps them after the CR is deleted. Flux and the app write tags, with Sandbox
RBAC as the only gate. An app preset supplies a launched Sandbox's initial tags; a tag removed later
stays removed, and preset edits change only later launches. A recreated Sandbox gets the tags its
manifest or preset gives it, and no old Session's recorded Sandbox association changes. No computed
selection is needed for v1. Tags select Sandbox targets; OAuth caller SAs are assigned through
PolicyBindings, not tags.

**Storage preference agreed:** grants are Kubernetes resources (PolicySets and PolicyBindings),
managed through GitOps or authorized runtime edits. This is a preference, not a hard requirement:
another store is acceptable if concrete implementation friction justifies it while preserving the
agreed authorization semantics and administration boundary. Rule schemas and concrete RBAC grants
remain open. Storage and operation evaluation are separate choices; the tentative service-enforced
rule direction below builds on this storage preference.

## Live permission bundles

**Agreed with the operator, 2026-10-16.** Assignment to a named permission bundle and the bundle's
definition are separate. Existing `PolicyBinding`s enroll SAs into shared, mutable `PolicySet`s;
editing a set changes the grants for every currently bound SA as the readers reconcile. This live
indirection is a requirement for any replacement authorization model. Expanding a bundle once at
launch or assignment into a fixed snapshot loses that capability.

For example, ten SAs can each receive thirty RoleBindings/ClusterRoleBindings from one set. Editing
the set to require twenty-five per SA causes the Policy Controller to remove the five obsolete
bindings for each SA. The same shared definition can also carry egress and Action rules. An
authorized runtime or GitOps edit adding an egress rule to `github` applies to every current
recipient of that bundle without editing their assignments. Removing the rule removes its grant
from all of them; independent overlapping grants still apply. The name `github` has only the
meaning its configured rules give it.

“Give every current recipient of `github` this extra egress rule for the next hour” is a temporary
change to the shared definition, followed by centrally removing that addition. This is distinct
from expiring an SA's whole binding. Automatic scheduling or rule-level expiry is not selected or
claimed to exist by this requirement; the chosen design must explain how the temporary change ends.

## Current identity and policy wiring

The [scoped-read plan](scoped_session_reads.md) applies the agreed Sandbox minimum and tags to
retained history and the tentative Session-access rule family; its exact schema and
administration remain open. The [external-access evaluation](external_access.md#compatibility-evaluation-github-kubernetes-http)
already calls for comparing reusable authorization systems rather than inventing a universal broker.

- External MCP OAuth Connections act as an existing labeled SA selected by the operator
  at enrollment ([Connection authority](../mcp_edge/connections/store.go)). This is an explicit
  Connection-to-SA mapping, not an automatic one-SA-per-OAuth-client rule. GitOps defines static
  accounts such as `claude-ai` and their `PolicyBinding`s. Human/operator authentication remains
  separate from the caller SA.
- For managed Sandboxes, the app's preset fills concrete `PolicySet` selections. The app writes
  those selections, plus platform defaults, into `Sandbox.spec.policySets`; the
  [Sandbox Controller](../sandbox_controller/identity.go) creates the dedicated SA and its
  Sandbox-owned base `PolicyBinding`. The app can also write additional runtime bindings for
  operator grants. Presets are defaults, not identities or authority.
- The same subject-free `PolicySet` and SA-subject `PolicyBinding` CRs apply to static and
  runtime-created accounts. The Policy Controller expands Kubernetes rules into native
  RoleBindings/ClusterRoleBindings and deletes expired bindings. Egress and Actions independently
  watch the CRs and evaluate their own rule types; they do not enforce through those native RBAC
  bindings. Action policies govern auto-approval; an unmatched request may still receive explicit
  operator approval.
- Editing a `PolicySet` changes the rules for all bound accounts as readers reconcile. Changing a
  preset's selections does not itself rewrite existing Sandboxes' policy bindings.

## Identity, role, address and isolation

These concepts answer different questions; optional policy concepts are not required resource types:

| Concept                              | Purpose                                                               | Open choice                                                                              |
| ------------------------------------ | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Human / workload / logical agent     | Who acts; retain the actual actor through intermediaries.             | V1 uses SA-level caller attribution; no persistent logical-agent identity is required.   |
| Optional group / role / relationship | Reusable grant assignment and resource grouping in configuration.     | Relationships, explicit membership, attributes, exact-resource grants, or a combination? |
| Session                              | Conversation addressed, read or controlled.                           | V1 grants cover a Sandbox and its Sessions; Session IDs address individual operations.   |
| Sandbox                              | Execution isolation, shared files/credentials, lifecycle and budgets. | Which compatible work may share it?                                                      |
| Optional standing recipient address  | Resolve a configured address to a current recipient.                  | Is addressing tied to an agent, role, mailbox or Session, and when is resolution pinned? |

“Project coordinator” can name a launch profile, a permission-bearing role or a recipient address.
They may be configured together without becoming one opaque object. Editing instructions or changing
a model should not silently change grants. Creator, manager and parent/provenance are separate
relationships; none needs to imply authority automatically.

V1 reuses authenticated SA identities; it requires no additional persistent logical-agent identity.
An optional future logical-agent object would need authenticated workload bindings, replacement and
revocation rules, and would not preserve a deleted Sandbox's Sessions. Standing roles do not
themselves require such an object. Human and agent subjects could share an authorization vocabulary
while retaining different authentication and explicit delegation.

The current [Sandbox boundary](../docs/thread_layering.md#sandbox-isolation-boundary) is a constraint:
co-resident Sessions share filesystem, credentials and SA authority. Session ACLs can restrict outside
callers but cannot establish confidentiality between co-resident agents. Incompatible trust or
credentials need separate Sandboxes under the current architecture. A caller-supplied `from_session`
is not verified authorship merely because its workload token is valid. V1 requires only
Sandbox/SA-level caller attribution. Any supplied originating-Session identifier is attribution
metadata, not a separately authenticated principal or a basis for stronger intra-Sandbox isolation.

### Sandbox and Session persistence

The existing [storage contract](../docs/sandbox_storage.md) distinguishes workload replacement from
Sandbox deletion. Suspension or Pod/VM replacement retains the Sandbox's owned PVCs and permits
Session continuation on retained state. Deleting the Sandbox deletes its owned PVCs and ends the
ability to deliver into or continue its Sessions. A newly created Sandbox, including one with the
same name, cannot continue those Sessions. The History Service retains their history for separately
authorized reads; retained history is not executable native Session state.

The [Dispatcher deletion contract](../docs/adr_sandbox_session_control.md)
already ends the deleted Sandbox's outstanding commands; retries must not retarget a new Sandbox.
Optional standing addresses or independently retained mailboxes would need an explicit contract for
addressing a different Session. They cannot make a deleted Session deliverable or resumable, and
continuation after Sandbox deletion is not an open multiagent requirement.

## Independent permissions and static rules

Candidate operation families, not an API or policy schema:

| Family               | Independently grantable operations                                         |
| -------------------- | -------------------------------------------------------------------------- |
| Discovery            | List a Sandbox's Sessions; inspect bounded status/metadata.                |
| History              | Read/follow retained transcript and evidence; decide representation scope. |
| Correspondence       | Send to a recipient; authorize the reverse direction separately.           |
| Inbox                | Read received messages; acknowledge handled work; administer the mailbox.  |
| Control              | Interrupt a specified turn, stop a Session, change model, change effort.   |
| Provisioning         | Open a Session; create/manage a Sandbox; select bounded configuration.     |
| Grant administration | Assign roles, change grants and authorize further delegation.              |

A manager can send work without reading a contributor's transcript. Reading a contributor need not
grant model changes; neither direction grants the contributor access to the manager's history.
Receiving a message exposes its content, not its sender's other conversations or authority.

Configuration could grant operations through project-wide membership, explicit management
relationships, creation provenance, direct Sandbox grants or another reviewed relation. These are
alternative policy expressions to support, not a choice of one topology for Agentplane. A rule
using “its contributors” must define that relation in configuration. Who may assign or change the
underlying facts is itself an authority decision; self-editable labels cannot establish trusted
membership.

Implement the tentative rule family below against direct, tag and `allSandboxes` targets and existing
SA assignments. Finish the grant administration and GitOps/runtime ownership contracts.
Shared definition edits remain live for current assignees, and independent overlapping grants
remain additive. GitOps reconciliation must not recreate a revoked or expired runtime grant
accidentally. Exact CRD and rule syntax remain open.

Model and effort controls may need argument constraints as well as target permissions. Moving a
Session with private context to another provider is a data-disclosure decision; switching providers
does not remove sensitive material already in that context.

## Multiagent authorization options

Build on the existing SA identities, live permission bundles and service ownership in the
[policy-set design](../docs/adr_policy_sets.md), preserving the authorization already used by
Kubernetes, egress and Actions.

### Tentative implementation direction

**Selected with the operator, 2026-10-16, subject to revision:** add service-enforced Session-access
rules to existing `PolicySet`s and assign them through existing SA `PolicyBinding`s. This follows
the typed-rule pattern already used by egress and Actions. Kubernetes RBAC controls administration
of the policy resources and Sandbox tags; the resource-owning services enforce Session operations.

The send rule is implemented: `session_inbox_access` in `policy/api/v1alpha1`, read through the
shared `policy/reader` package and enforced by Notifications
([send contract](../notification_service/docs/api.md#messages)).

**Send and transcript reads are separate rule types (operator, 2026-10-18 and 2026-10-21 UTC):**
they are separate capabilities, and transcript reads "should probably go through a different kind of
rule, just like they'll go through a different kind of tag" (2026-10-21 03:43). The send rule is
`session_inbox_access`, whose one operation is `send_message`. `list_sessions` and `read_transcript`
get a rule type of their own when the held read decisions reopen; the rule reading and target
matching are shared.

Support direct Sandbox targets as well as tags. For Sandbox-hosted Session operations, the backend
authenticates the SA, takes the Sandbox from the request (or, in History, which owns the record of
the Sandbox that wrote each Session, from that record), and allows the operation if any current
bound rule matches that operation and Sandbox, directly or through its current tags (in History,
the history tags on its record). Share rule-reading and evaluation code while enforcing each
backend's own API, including paths that bypass Dispatcher. Preserve live bundle edits, tag changes,
expiry and additive overlapping grants; listing, transcript reads and sending remain independently grantable.

The send operation appends correspondence to a particular Session's inbox. Its raw peer payload
is stored there; the platform-generated notice is a separate delivery effect. Sandbox and tag targets
select permitted Sandboxes; a tag is not an inbox recipient. Session addressing and
canonical state stay outside Kubernetes; this direction requires no Session CRs or projection of
these rules into native Roles.

Before the read rule, finalize its schema, concrete administrative RBAC, filtered enumeration and
retained-history behavior; the propagation contract below applies. Revisit the representation if
implementation exposes a poor fit or disproportionate maintenance cost.

### Policy propagation and freshness

**V1 direction agreed:** follow the existing Actions/egress pattern of independently watched
policy state with freshness checks. Each enforcing backend evaluates its local view of PolicySets,
PolicyBindings and Sandbox tags. Changes apply as replicas observe them; replicas can
briefly disagree, and an edit is not an atomic cluster-wide authorization cutover.

Refuse protected operations before initial sync or when required policy data exceeds a configurable
freshness limit. A watch outage must not permit indefinite use of old grants. Re-evaluate
authorization and freshness throughout transcript streams, and stop further Event emission when
the replica observes loss of the required permission or its policy view becomes too stale.
Independent overlapping grants still apply. Notifications implements this for sends (refused while its
policy informers are unsynced or failing; [README](../notification_service/README.md#running-and-deployment)),
following the egress informer; History's read rule reuses `policy/reader`. No global replica-acknowledgement
barrier is required for v1.

### Alternatives if the tentative direction needs revision

Both native RBAC and service-enforced rules can use PolicySets/Bindings for assignment. The
distinction is how operation/target rules are represented and evaluated, not PolicySets versus
Kubernetes RBAC. Alternatives remain available without requiring their implementation first.

| Candidate                                 | How it could fit the existing design                                                                                                                                      | Questions to evaluate                                                                                                                                                                                                          |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Native RBAC with tag projection           | Represent Session operations using Sandbox-scoped authorization attributes. Generate Roles from Sandbox tags and assign them through existing PolicySet Kubernetes rules. | A Role per tag and independent permission, or configured bundle, needs new Role reconciliation and constrained write authority; today's Policy Controller only maintains bindings. A tag on no Sandbox must produce no grants. |
| Native RBAC with backend scope resolution | Backends resolve the Sandbox and its tags, then submit access reviews for direct or tag scopes.                                                                           | Avoids Role expansion, but authorization attributes must faithfully distinguish the Session operation, Sandbox boundary and tag selector. Tags are not inbox recipients.                                                       |
| RBAC-gated named policies                 | A domain policy defines operations/targets; native RBAC grants `use` of that policy. Existing PolicySet Kubernetes rules can assign the referenced Roles.                 | Keeps native assignment but adds policy interpretation and Roles for named policies. Compare the extra layer with direct PolicyBinding enrollment.                                                                             |

Related precedents are [Knative EventPolicy](https://knative.dev/v1.21-docs/eventing/features/authorization/)
for domain-specific delivery policy, [Argo CD](https://argo-cd.readthedocs.io/en/stable/operator-manual/rbac/)
for application permissions stored in Kubernetes configuration, and
[OpenShift SCCs](https://docs.redhat.com/en/documentation/openshift_container_platform/4.12/html/authentication_and_authorization/managing-pod-security-policies)
for RBAC-granted `use` of a named policy. These are patterns to adapt, not products or their
organizational models selected for Agentplane. Namespace-scoped Roles/ClusterRoles are another
option, but require reviewing namespace lifecycle/layout and tag binding fan-out.

For the native-RBAC option, [SubjectAccessReview](https://kubernetes.io/docs/reference/access-authn-authz/authorization/#checking-api-access)
allows a service to ask the API server about another caller's access. The backend must derive that
caller from verified authentication and construct the target attributes itself. Review the API
server's authorizer composition; `SubjectAccessReview` is not an RBAC-only evaluator. Decide whether the
representation needs real Kubernetes objects; do not assume that choosing RBAC moves canonical
Session history or execution state into Kubernetes. Inspect
[`resourceNames` and list/watch restrictions](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources),
request-body constraints and relationship expressiveness against actual use cases. A broad list
permission does not by itself provide permission-filtered Session discovery.

Validate the tentative direction, and any replacement, using the same cases below: mixed static
and runtime-created SAs, live
bundle edits across services, overlapping grants, and independent send/read/control rights on
concrete Sandbox targets, with Session IDs used only as operation addresses. Configuration must express different topologies without
requiring agent categories. Identify who may author definitions, enroll SAs or change trusted
relationships; retain the current separation between grant administration and workload use.

Show the authoring, storage, evaluation and enforcement path, including API bypasses, filtering
before pagination/counts, policy freshness, stream/queued-operation revocation and dependency
outages. Retained history and its access policy must survive disappearance of the live Sandbox.
Preserve the current native-binding ownership, pruning and constrained `bind` authority where that
projection is used. A common evaluator does not eliminate enforcement in each resource-owning
backend. Action request authorization, operator approval and execution evidence remain distinct.

The [broader authorization-system evaluation](external_access.md#compatibility-evaluation-github-kubernetes-http)
is separate, deferred work. A concrete multiagent limitation can motivate a bounded extension or a
separately reviewed auth proposal; it does not automatically make an engine migration a multiagent
prerequisite. Compare the incremental option first, and identify the benefit and migration cost of
any broader change explicitly.

### Option: authorize the archive apart from live objects

**Floated by the operator, 2026-10-18 UTC, tentatively and with no date to decide.** Permissions on
the **archive**, the retained Session history the History Service serves (raw Events, evidence,
Thread folds, summaries), would live in a separate universe from permissions on **live objects**:
Sandboxes, Session inboxes, Dispatcher commands and the Runner Proxy. A grant in one confers nothing
in the other, and each names its targets in terms it owns. The rest of this section is the plan's
reading of the idea. The archive here is not [`SANDBOX_ARCHIVE`](task_dag.md#sandbox_archive--a-suspended-sandbox-archived-to-object-storage),
a suspended Sandbox's storage.

**Where the line falls.** History is the only way into retained history, so every transcript read,
including one of a running Sandbox's Session, is an archive read. The tentative Session operations
already divide by the service that enforces them:

| Aspect               | Live objects                                        | Archive                                                                          |
| -------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------- |
| Operations           | `send_message`; control, when designed              | `list_sessions`, `read_transcript`                                               |
| Enforced by          | the inbox owner, the Dispatcher, the Runner Gateway | History, on its own API                                                          |
| Targets are named as | live Sandboxes: direct, inbox tags, `allSandboxes`  | the Sandbox as History recorded it: namespace, name, UID once kept, history tags |
| Lasts as long as     | the Sandbox                                         | the history                                                                      |

Today History checks only that the caller is one of its configured `reader_accounts` (staging: the
app and Notifications), then serves any Session by ID; there is no per-Session or per-Sandbox check,
and a workload ServiceAccount is refused (`api/service.go`, `workload_auth/go/auth`). Writes are
fenced separately: the ingress binds a Session to its first pushing ServiceAccount (namespace, name,
UID) and refuses any other (`ingress/ingress.go`). The archive therefore already holds an identity of its
own for every history bound to a writer, which is what the split would build on.

**What it would mean.**

- Archive rules are their own rule type whose targets are archive subjects: a direct Sandbox
  (namespace and name, optional UID) and `allSandboxes`, both matched on History's record. For
  these two the split changes where the rule lives, not what it matches, since neither reads a live
  object.
- Tags already take this split ([design](sandbox_tags.md)): inbox tags are live and go with the
  CR, while history tags are archive data that History copies into its record of the Sandbox and
  keeps after the CR is deleted. A history-tag grant matches that record, never the live object.
- How separate is open. **Separate rule type**: same PolicySets and PolicyBindings, so assignment,
  GitOps authoring, live bundle edits and the shared policy watch carry over. **Separate
  authority**: History stores and administers archive grants itself, with its own API and audit;
  this alone removes History's dependency on Kubernetes policy resources, but archive grants then
  lose GitOps and the [live-bundle semantics](#live-permission-bundles) a replacement must keep.
  The first fits the tentative direction. Either way, reads get a rule type of their own (operator,
  2026-10-21 UTC).

**What it decouples.**

- A read needs no live Kubernetes object: no ownerReference or Sandbox has to exist, and no
  Kubernetes object becomes a historical log.
- History's read path checks archive grants against its own records, never a live Sandbox.
- Deleting a Sandbox or removing an inbox tag never changes archive access, and an archive
  grant never keeps a Sandbox alive.

**Trade-offs.**

- A person who expects one grant gets two: "X may send to and read `developers`" is a rule in each
  universe, and the inbox and history tags of that name can drift apart. Only tags cost this;
  direct and `allSandboxes` targets read the same either way. Send and read are already independent
  grants.
- A second rule type and target vocabulary to review, test and document. Evaluator and watch code
  are shared.
- Archive access outlives the Sandbox, so deleting one no longer withdraws it. Ending it means
  editing the grant or deleting the history, which is a separate data deletion.
- A history with no recorded Sandbox stays operator-only in either universe.

**Interaction with the History key decisions.**

- History records and checks the Sandbox UID (#11835). The UID is the archive's own owner
  identity, the value UID-pinned archive grants match, copied from the writer ServiceAccount's
  ownerReference at bind; afterwards the archive never reads the live object. A name-based archive
  grant covers a deleted Sandbox and its same-name replacement alike; a UID-pinned one covers one
  incarnation.
- The global Session ID stays the key (operator, 2026-10-24): a grant matches the recorded UID
  column, so the split does not need a `(sandbox_uid, session_id)` key.
- The send path is unaffected: it checks `send_message` against live Sandboxes and History's
  recorded namespace and name. `session_inbox_access` carries `send_message` alone,
  and the archive operations get their own rule type with or without the split
  ([separate rule types](#tentative-implementation-direction)).

## Message storage, delivery and agent tools

**Shipped (2026-10-21).** Agent correspondence uses the Notification Service's durable Session
inboxes; its [API](../notification_service/docs/api.md) is the contract for sends, authorization,
sender attribution, receipts, notices, wake-on-send, revocation, retention, quotas and size bounds.
Renaming and splitting the service is
[`NOTIFICATION_SERVICE_SPLIT`](task_dag.md#notification_service_split--split-move-or-rename-the-notification-services-responsibilities).
Still open:

- Validate actual agent receive, read and acknowledge behavior before calling the workflow effective.
- Inbox authority is the owning Sandbox's ServiceAccount, which no send or read grant confers.
  Delegated inbox reading and acknowledgement are
  [deferred](task_freezer.md#delegated_inbox_access--delegated-inbox-reading-and-acknowledgement).
- The longer-term intent is wake-on-message by default, with suspension and resume transparent to
  users and agents. V1 keeps per-Sandbox `spec.wakeOnSend`, Dispatcher's wake allowlist and operator
  lifecycle controls; messaging and history grants imply no lifecycle edits
  ([lifecycle authorization](task_freezer.md#sandbox_lifecycle_authorization_design--delegated-lifecycle-controls)).
  Per-source wake switches and moving notice timing into Dispatcher are
  [deferred](task_freezer.md#notification_dispatcher_ownership--review-notification-and-dispatcher-ownership).
- A2A, MCP and other facades can expose the same contract later without changing the SA and
  operation/target policy. Protocol roles or room membership never grant history reads, controls or
  reverse sends, and no facade adds a command queue beside Dispatcher's.
- Forwarded actor or delegation proof needs its own audience, scope, lifetime and revocation contract.
  Controls may need execution-time checks that correspondence does not. File exchange can later use
  authorized retained object references; storage, retention and read rights stay separate questions.

## Discovery, session creation and Sandbox launch

### OAuth callers

**V1 route agreed with the operator, 2026-10-17:** OAuth clients act as the SA their Action Service
Connection is bound to, using the same PolicySet/Binding assignment model as workload callers. They
open an Action box (a Pod, not a `Sandbox`) through the `sandbox__*` Actions and send the messaging
and history requests from it as an ordinary workload, with the egress proxy substituting that SA's projected
token for the destination. Notifications and History therefore gain no OAuth admission path and keep
authenticating workload tokens only. Projecting the Notifications and History audiences into the box,
and the matching egress rules on the SA's PolicySet, are deployment configuration for the build nodes
where not yet present. Such a sender's verified identity is the SA shared by the Connection's boxes,
not a `Sandbox` of its own, which SA-level attribution accepts. Their agent loops run outside the
cluster and have no receiving mechanism in v1. External inboxes are deferred; Connection
representation or storage changes are outside this multiagent work.

### Sandbox Session discovery and provisioning

**V1 discovery direction agreed:** listing a Sandbox's Sessions is a distinct read-type permission,
weaker than reading their transcripts and independently grantable from sending. Enumeration exposes
bounded metadata, not transcript content or evidence. The fields are the Session ID, the Sandbox that
wrote it, harness kind, feed state, idle and last-turn state, and first and last event times. The
listing is not `SessionSummary` as it stands: that message embeds the Session spec, whose `instructions`
and `cwd` are content.
It uses the same Sandbox authorization granularity as other multiagent permissions. A sender with
a known Session address need not have enumeration permission. Keep live runner state, retained
Session identity/history and a standing recipient directory distinct; listing Sessions does not
grant arbitrary Kubernetes inventory access.

Opening Sessions in an agent's own Sandbox is a desired early use case. Define what “own” means,
allowed models/settings, quota, setup authority, creator visibility and revocation. It adds no
isolation from co-resident Sessions. Launch-and-open can compose grants without implicitly granting
history reads, unrestricted messaging or grant administration.

Sandbox creation must bound the effective authority, not only the chosen template: images, mounts,
secrets, policy sets, credentials, resources, fanout, lifetime and later edits. The controller creates
a dedicated SA and binds selected policy sets; granting CR creation therefore needs constraints on
those selections. Compare a constrained launch service with admission enforcement or native RBAC
plus a reviewed grant/template mechanism. Plain Kubernetes RBAC cannot express arbitrary request-body
constraints; [workload creation](https://kubernetes.io/docs/concepts/security/rbac-good-practices/#workload-creation)
can expose mounted resources and other SA privileges, and top-level
[`create` cannot be restricted by resource name](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources).

Delegation need not be automatic inheritance or simply a subset of the creator's own usable rights.
A manager might be authorized to provision a specific worker role it cannot itself exercise.
Define that grant authority, further delegation, expiry and parent-termination behavior explicitly.

## Prior art and protocol scope

- **A2A remains open for agent-to-agent communication.** The existing
  [A2A evaluation](../docs/a2a.md) rejects it for the harness/thread protocol's exact native evidence,
  admission, fencing and recovery needs. It did not evaluate or reject this communication boundary.
  Evaluate its discovery cards, messages, tasks, artifacts, streaming and multi-turn model here,
  including whether an optional facade fits without forcing every correspondence into a task.
  Its [authorization responsibilities](https://a2a-protocol.org/latest/specification/#75-server-authorization-responsibilities)
  remain implementation-specific; protocol adoption would not settle Agentplane permissions.
- **Matrix** provides rooms, membership, history visibility, event permissions and a messaging
  ecosystem ([specification](https://spec.matrix.org/latest/client-server-api/)). Human chat
  interoperability or federation would strengthen its case. Compare identity mapping, room versus
  Session semantics, permission duplication, retention and control integration before adopting it.
- **Temporal** distinguishes asynchronous signals, queries and tracked updates
  ([message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing)). This is useful
  prior art for correspondence versus controls and receipts, without selecting a workflow platform.
- **Native harness delegation** can remain useful inside a Sandbox. It does not by itself supply
  Agentplane's cross-Sandbox identity and authority; exposing native children as independent
  Agentplane Sessions remains a separate [discovery design](native_session_discovery.md). V1
  excludes native children from the messaging and access model.

## Information transfer and v1 scope

An agent allowed both to read private information and send arbitrary text to another recipient has
an information-transfer path. Recipient permissions control the allowed channel, not whether the
content contains sensitive data. Likewise, incoming peer content is not trusted instruction or an
authority grant; a more privileged recipient can act as a confused deputy if it treats it that way.

A classifier/judge on communication is an optional future safeguard, not v1 work, acceptance or an
authorization dependency. Its placement, outbound disclosure versus inbound injection checks,
allow/redact/block behavior, latency and false decisions remain open. It must not substitute for
access control or silently discard accepted messages. The deferred item is
[`AGENT_MESSAGE_CLASSIFICATION`](task_freezer.md#agent_message_classification--optional-outbound-content-safeguards).

## Cases to resolve before implementation

| Case                                               | Questions it distinguishes                                                                                                                                                        |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Asymmetric collaboration across separate Sandboxes | Can configuration grant launch, send and reads independently? Which model/provider changes are permitted? V1 has no classifier.                                                   |
| Different configured topologies                    | Can policies express project-wide rules, explicit relationships and direct resource grants without a mandatory hierarchy? Who may change the facts?                               |
| Live bundles with mixed SA enrollment              | Can one bundle serve static OAuth caller SAs and new Sandbox SAs, propagate additions/removals across services, and end a temporary shared grant without editing each assignment? |
| Independent overlapping grants                     | Does removing one assignment or rule preserve what another still grants, and does removing the last grant revoke access?                                                          |
| Configured peers                                   | Can peers discover/send without reading each other's history or controlling turns? Is reverse send separately granted?                                                            |
| Extra Session in an existing Sandbox               | Who may Open with which settings/budget? What sharing of files and credentials is acceptable?                                                                                     |
| Sender attribution                                 | Does each request retain verified Sandbox/SA provenance without treating an optional Session label as authenticated authorship?                                                   |
| Workload replacement within a retained Sandbox     | How do authentication and grants follow the new workload while delivery remains bound to the retained Sandbox and Session?                                                        |
| Sandbox deletion                                   | Are further delivery and continuation refused, outstanding commands ended and retained history still separately authorized?                                                       |
| Revocation during a stream or queued operation     | Which policy revision applies, what stops, and what remains durably accepted? How does an outage affect enforcement?                                                              |
| Human and agent using the same operation           | V1 may retain the app's auth path. Any later convergence must preserve caller attribution and distinct request, approval and execution authority.                                 |

These cases should compare allowed/denied operations, authorized discovery, evidence and operational
cost for the tentative direction and any replacement. Open decisions include identity continuity,
grant and relationship ownership, exact rule schemas, freshness settings, whether the
archive is [authorized apart from live objects](#option-authorize-the-archive-apart-from-live-objects),
remaining inbox/API semantics, optional protocol facades,
provisioning/delegation, and migration across existing services. No runtime implementation or engine
adoption is authorized by this document.
