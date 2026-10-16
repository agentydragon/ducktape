# Cross-agent communication and authorization: design space

Status: **open design exploration**. This records requirements, candidate arrangements and questions;
it selects no authorization engine, identity model, messaging protocol, service or storage owner.
Implementation gates remain in the [task DAG](task_dag.md#3-multiagent-decisions-before-implementations).

## Requirements and preferences

- Agents with different access must be able to collaborate across separate Sandboxes. Useful
  workflows include a private coordinator assigning public-data work to specialists, peers within
  a project, and standing roles such as project manager and individual contributor.
- Static configuration should be able to express reusable rules such as “this project's manager
  may perform these operations on its contributors.” GitOps and Kubernetes are plausible places
  to author that configuration; this preference does not select its representation or evaluator.
- Sending and transcript reads are independent, directed permissions: `send(X, Y)`,
  `read_history(X, Y)` and `read_history(Y, X)` imply none of one another. A reply or correlation
  reference confers no permission. A named role may explicitly bundle grants without making those
  implications intrinsic to messaging.
- Communication should be an additive, optional capability. Running a harness, using Actions or
  reading history should not require deploying the entire communication system. The integration
  app remains a client of independently usable backends ([service boundaries](../docs/service_boundaries.md)).
- Low-regret choices keep identity, roles, execution placement, authority and delivery separable.
  Replacing Agentplane's existing authorization machinery across services is in scope for evaluation;
  retaining `PolicySet`/`PolicyBinding` is not a prerequisite.
- A communication classifier or trusted-model judge is a possible **later** addition, outside v1
  and not a messaging prerequisite. File attachments are also unnecessary for v1.
- HTTP, gRPC, MCP and CLI facades are lower-priority, replaceable choices. They do not need to be
  selected before the authority and message contracts. A2A's domain semantics deserve a separate
  evaluation from its transport bindings.

## Existing pieces

Source inventory checked on 2026-10-16 against `5d8e45e13a`; this is not a live deployment audit.

| Concern           | Existing implementation                                                                                                                                                              | Gap for general cross-agent use                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Workload identity | [Python principal](../workload_auth/principal.py) and [Go resolver](../runner_gateway/go/auth/auth.go) verify audience-scoped, Pod-bound SA tokens and retain SA/Pod UIDs.           | No authenticated per-Session sender or persistent logical-agent identity follows from that bearer.                                        |
| Standing grants   | [Policy sets](../docs/adr_policy_sets.md) bind typed Kubernetes, egress and Action rules to SAs.                                                                                     | No general session sharing or project-role model; human authentication and Action operator authority remain separate.                     |
| Command delivery  | [Dispatcher](../session_dispatcher/README.md) supports input, model/effort changes, interrupts, stop and Open, with durable queues for the queued operations.                        | Its service caller allowlist is not destination- and operation-scoped agent authorization.                                                |
| Runner access     | [Proxy](../runner_gateway/go/gateway/proxy.go) checks callers and Sandbox UID/owner identity; Quiesce is controller-only.                                                            | Identity fencing does not grant permission, and general authorization must cover direct paths around command admission.                   |
| Receiving         | [Notifications](../notification_service/docs/api.md) stores Actions/GitHub events in session inboxes, sends notices through Dispatcher, and exposes read/ack operations.             | Agent correspondence is not a source. An inbox is tied to an SA, Sandbox incarnation and Session, not a standing role across replacement. |
| History           | [History reads](../history_service/grpc_api.py) serve retained conversations and evidence to configured service readers.                                                             | Scoped agent discovery/read/follow permissions are not implemented.                                                                       |
| Provisioning      | [Sandbox inventory](../sandboxes/inventory.py) submits CRs; the [controller](../sandbox_controller/identity.go) creates a dedicated SA and binds the Sandbox's selected policy sets. | Constrained agent launch, delegation and session-create authority remain design work.                                                     |

The [scoped-read proposal](scoped_session_reads.md) is one candidate: exactly one compartment,
SA-scoped reads and no initial exact-Session exceptions. Those choices remain open against the
alternatives here. The [external-access evaluation](external_access.md#compatibility-evaluation-github-kubernetes-http)
already calls for comparing reusable authorization systems rather than inventing a universal broker.

## Identity, role, address and isolation

These levels answer different questions and can coexist:

| Concept                              | Purpose                                                               | Open choice                                                                              |
| ------------------------------------ | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Human / workload / logical agent     | Who acts; retain the actual actor through intermediaries.             | SA identity alone, or a persistent agent with authorized workload bindings?              |
| Project / group / role / compartment | Reusable grant assignment and resource grouping.                      | Relationships, explicit membership, attributes, exact-resource grants, or a combination? |
| Session                              | Conversation addressed, read or controlled.                           | Which rights inherit from a project or Sandbox, and which require explicit grants?       |
| Sandbox                              | Execution isolation, shared files/credentials, lifecycle and budgets. | Which compatible work may share it?                                                      |
| Standing recipient address           | Resolve “project P's coordinator” to a current recipient.             | Is addressing tied to an agent, role, mailbox or Session, and when is resolution pinned? |

“Project coordinator” can name a launch profile, a permission-bearing role or a recipient address.
They may be configured together without becoming one opaque object. Editing instructions or changing
a model should not silently change grants. Creator, manager and parent/provenance are separate
relationships; none needs to imply authority automatically.

Two plausible foundations are SA identities explicitly enrolled into roles, or persistent agents
with authenticated bindings to current workloads. The former reuses today's foundation; the latter
supports continuity across replacement but needs binding issuance, replacement and revocation rules.
Standing roles do not themselves require a persistent agent object. Human and agent subjects could
share an authorization vocabulary while retaining different authentication and explicit delegation.

The current [Sandbox boundary](../docs/thread_layering.md#sandbox-isolation-boundary) is a constraint:
co-resident Sessions share filesystem, credentials and SA authority. Session ACLs can restrict outside
callers but cannot establish confidentiality between co-resident agents. Incompatible trust or
credentials need separate Sandboxes under the current architecture. A caller-supplied `from_session`
is not verified authorship merely because its workload token is valid.

## Independent permissions and static rules

Candidate operation families, not an API or policy schema:

| Family               | Independently grantable operations                                         |
| -------------------- | -------------------------------------------------------------------------- |
| Discovery            | Find recipients; inspect bounded status/metadata.                          |
| History              | Read/follow retained transcript and evidence; decide representation scope. |
| Correspondence       | Send to a recipient; authorize the reverse direction separately.           |
| Inbox                | Read received messages; acknowledge handled work; administer the mailbox.  |
| Control              | Interrupt a specified turn, stop a Session, change model, change effort.   |
| Provisioning         | Open a Session; create/manage a Sandbox; select bounded configuration.     |
| Grant administration | Assign roles, change grants and authorize further delegation.              |

A manager can send work without reading a contributor's transcript. Reading a contributor need not
grant model changes; neither direction grants the contributor access to the manager's history.
Receiving a message exposes its content, not its sender's other conversations or authority.

Static configuration could grant a project-manager role selected operations on contributors related
to that manager. “Its contributors” still needs a definition: same-project membership, an explicit
management relationship, creation by that manager, or another reviewed relation. Who may assign or
change those relationships is itself an authority decision; self-editable labels cannot establish
trusted membership.

Compare native RBAC plus a constrained grant/template controller, CRDs compiled into an authorization
engine, and Git-authored engine policy with runtime membership elsewhere. Decide whether a rule
applies live to future contributors or snapshots membership, how exceptions combine, and how GitOps
interacts with runtime/expiring grants. A GitOps reconciliation must not recreate a revoked or
expired runtime grant accidentally. No CRD or rule syntax is selected here.

Model and effort controls may need argument constraints as well as target permissions. Moving a
Session with private context to another provider is a data-disclosure decision; switching providers
does not remove sensitive material already in that context.

## Authorization systems worth comparing

These are fit hypotheses from the linked primary documentation, not a completed compatibility,
performance or adoption assessment.

| Candidate                                                        | Potential fit                                                                                                | Work still owned by Agentplane                                                                                      |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Current grants plus explicit resource ACLs                       | Smallest initial operational change.                                                                         | Own the relationship, discovery, delegation and revocation semantics; measure whether this actually stays simpler.  |
| [OpenFGA](https://openfga.dev/docs/modeling/overview)            | Roles, groups, relationships, inheritance, conditions and permission-based object lookup.                    | Trusted relationship writers, resource synchronization and consistency choices.                                     |
| [SpiceDB](https://authzed.com/docs/spicedb/concepts/consistency) | Relationship authorization, filtered discovery and revision-based freshness requirements.                    | Relationship lifecycle, service operation and consistency integration with application data.                        |
| [Cedar](https://docs.cedarpolicy.com/overview/terminology.html)  | Principal/action/resource/context policy, roles, attributes, argument constraints and explicit prohibitions. | Authoritative entity inputs, policy management and authorized list/search integration.                              |
| [OPA](https://www.openpolicyagent.org/docs)                      | General policy evaluation across service authorization, launch/admission and egress.                         | Data ownership/distribution/freshness and a bounded policy surface; a general engine must remove custom complexity. |

A relationship engine versus Cedar is a useful first comparison: project roles and discovery favor
evaluating relationships, while launch/model conditions exercise contextual policy. Compare OPA as
the broader alternative. Do not assume two engines are needed before trying the same cases in each.

Check both individual decisions and authorized lists/search, including filtering before pagination,
metadata and count exposure. Compare revocation while following history, SA replacement, policy
changes during queued delivery and unavailable dependencies. SpiceDB's
[freshness tokens](https://authzed.com/docs/spicedb/concepts/consistency) and OpenFGA's
[consistency modes](https://openfga.dev/docs/interacting/consistency) are relevant mechanisms, not
automatic solutions to application-level stream or queue revocation.

Evaluate authoring, storage, evaluation and enforcement separately. Kubernetes-authored standing
policy, application/relationship-owned runtime grants, and an explicit combination are all open.
Retained histories and their access policy must survive disappearance of the live Sandbox; the
existing control design keeps per-Session records outside Kubernetes. An implementation needs one
authoritative owner per fact and an explicit reconciliation/transaction strategy for projections.

[AuthZEN](https://openid.net/specs/authorization-api-1_0.html) is a candidate standard decision/search
interface, independent of the engine; it does not select the grant model or approval workflow.
A common evaluator still needs enforcement in resource-owning backends and on bypass paths.
Kubernetes must enforce its own API access through native grants or a deliberate integration.
The Action Service's durable request/approval/execution states remain distinct: authorization to
request something is not approval to execute it, and approval is not execution evidence.

## Message storage, delivery and agent tools

| Candidate                            | Recipient experience                                                                       | Tradeoff                                                                                                                |
| ------------------------------------ | ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| Direct Session input                 | Authenticated correspondence is submitted through Dispatcher.                              | Fewest new pieces; messages may start turns. Batching, selective handling and handled acknowledgements need a contract. |
| Durable inbox plus notice            | Content is stored; Dispatcher delivers a notice, and the recipient retrieves/acks entries. | Reuses Notifications' cursors and batching; requires retrieval and explicit mailbox ownership/lifetime.                 |
| Existing messaging system / protocol | Map recipients, messages and receipts into its model.                                      | Reuse may help interoperability, but identity, authorization, delivery and storage mappings still need evaluation.      |

Inbox delivery for ordinary correspondence with explicit RPCs for controls is an initial hypothesis,
not a selection. Reusing Notifications does not settle whether its current SA/Sandbox/Session-scoped
inbox can serve standing roles, or who owns a mailbox after Sandbox deletion. Direct delivery must
retain provenance without making peer text an operator instruction. A new queue duplicating
Dispatcher retry/admission semantics needs a concrete justification.

An envelope could retain an idempotent message ID, authenticated sender, resolved destination,
correlation/reply reference and content. Which Session attribution can be verified, and who may
inspect provenance, are open. Do not trust sender identity in the body or launder a sender into a
Notification/Messaging service's broader authority. Forwarded actor/delegation proof needs its own
audience, scope, lifetime and revocation contract.

Distinguish durable acceptance, runner admission, harness confirmation, explicit handling
acknowledgement and task success. The current Dispatcher has no writer for `HARNESS_CONFIRMED`.
Reading an inbox is not acknowledgement, and an acknowledgement does not prove task success.
Choose offline behavior, ordering, duplicate handling, batching, retention and abuse limits.
Sending, waking a suspended Sandbox, and interrupting its active turn are distinct effects.

Decide whether revoking send authority affects only future sends or also accepted-but-undelivered
mail. Controls may need different execution-time checks from correspondence. Bytes already read
cannot be revoked. A retry must not silently retarget a replacement Sandbox or new role holder.

Illustrative tool operations are finding recipients, sending, checking delivery, reading/acking an
inbox and opening a Session. Control operations stay explicit, with target and parameter constraints
such as the expected turn ID. HTTP/gRPC/MCP/CLI facades can expose the selected contract later;
their names and schemas are not settled here. File exchange can later use authorized retained
object references without requiring shared filesystems; storage, retention and read rights remain
separate questions.

## Discovery, session creation and Sandbox launch

Discovery should expose authorized recipients and bounded metadata; decide its relationship to
history reads and to explicit addresses a sender knows without enumeration. Keep live runner state,
retained Session identity/history and a standing recipient directory distinct. A recipient can be
addressable without granting transcript access or exposing arbitrary Kubernetes inventory.

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
  Agentplane Sessions remains a separate [discovery design](native_session_discovery.md).

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

## Cases to resolve before choosing an implementation

| Case                                                              | Questions it distinguishes                                                                                                |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Private coordinator, public-data specialist in a separate Sandbox | Which role grants launch, send and reads independently? Which model/provider changes are permitted? V1 has no classifier. |
| Manager and contributors from static configuration                | What makes a contributor “its” contributor? Who can assign that relation? Do future contributors acquire the rule live?   |
| Project peers                                                     | Can peers discover/send without reading each other's history or controlling turns? Is reverse send separately granted?    |
| Extra Session in an existing Sandbox                              | Who may Open with which settings/budget? What sharing of files and credentials is acceptable?                             |
| Coordinator replacement                                           | Does identity, address, inbox or authority survive? Where do queued messages go, and who may read old history?            |
| Revocation during a stream or queued operation                    | Which policy revision applies, what stops, and what remains durably accepted? How does an outage affect enforcement?      |
| Human and agent using the same operation                          | Can one model express both without implicit impersonation or merging request, approval and execution?                     |

These cases should compare allowed/denied operations, authorized discovery, evidence and operational
cost across candidate models. Open decisions include identity continuity, grant and relationship
ownership, policy authoring/evaluation/storage, message/mailbox semantics, A2A/other protocol fit,
provisioning/delegation, and migration across existing services. No runtime implementation or engine
adoption is authorized by this document.
