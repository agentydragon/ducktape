# Architecture overview: where Agentplane's objects and data live

Status: **no direction chosen, 2026-10-19.** Written at the operator's request for one overview
before the design lanes go further (01:12 UTC: "some kind of unified idea/overview/plan"), about
"where do what objects live" (01:37), including "should we run on k8s, should our cp be the same as
the outer cluster's cp, or should we have some database for our objects that just reconciles via
e.g. tofu or something" (01:58). The operator is "uncomfortable with making definite decisions" about
this cluster of questions and wants "to go towards some point where i'll be comfortable picking a
direction" (02:14). So this file asks for no pick. It lays out whole directions, walks the
operator's workflows through each, prices building and backing out, and lists the moves that are
right under every direction and the experiments that would narrow the choice. Each plan and
[task DAG](task_dag.md) node stays authoritative until a direction is chosen; the chosen parts then
land in their owning docs and DAG nodes, and this file is deleted.

## Why the questions are one cluster

Each of these changes the others:

- whether a Sandbox's history outlives it;
- which control plane holds Agentplane's records: the outer cluster's Kubernetes API, Agentplane's
  own, or a database;
- where collection membership lives;
- whether History records the Sandbox UID, and keys Sessions by it;
- whether reads of retained history carry grants of their own.

The first two are the axes, and the other three follow from where a direction puts them. On the
first, the operator: keeping grants for Sandboxes that no longer exist is "mostly an artifact of my
workflow being a weird hybrid", and "one other consistent option would be e.g. 'deleting a Sandbox
also deletes its history records'" (02:04). Sandbox workloads run on Kubernetes in every direction
([principles](../docs/principles.md): Kubernetes is a shared baseline); what differs is where records
and retained data live.

## Four directions

- **D1. The Sandbox is the unit.** History dies with its Sandbox. A finished Sandbox is retired,
  stopped with `spec.wakeOnSend: false` (both fields exist today), rather than deleted, and deleting
  it is the one act that drops its history. Every record stays in the outer cluster's API, and
  anything that names a Sandbox, such as collection membership, is a Kubernetes object it owns.
- **D2. Kubernetes for live objects, databases for what outlives them.** Today's workflow made
  consistent: a finished Sandbox is deleted, and its history, collection membership and archive
  grants stay in service databases keyed by Sandbox UID, with git-authored sets reconciled in. Live
  grants stay in `PolicySet`s.
- **D3. Agentplane's own control plane.** A nodeless kube-apiserver per environment, on kine over the
  CNPG cluster, holds the Agentplane kinds; workloads stay on the cluster. Either lifetime works, and
  retiring comes naturally, since retired Sandboxes then cost the cluster's control plane nothing.
- **D4. Database-backed Agentplane.** A service owns Agentplane's records as rows behind its own API,
  as the Dispatcher owns its queue, and tofu-controller or an importer reconciles the git-authored
  ones in, as `tf/gitops/` does for Authentik and Forgejo. A controller still writes the workload
  objects into the cluster. Either lifetime works, and outliving comes naturally, since a deleted
  Sandbox can stay a row.

D1 and D2 differ only in lifetime; D3 and D4 change the store.

## The operator's workflows in each

| Workflow                                                                                                                         | D1                                                                       | D2                                                                            | D3                                                                                                                    | D4                                                                                           |
| -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Launch a Sandbox                                                                                                                 | As today                                                                 | As today                                                                      | The app writes it to the Agentplane plane; the controller creates its children on the cluster and deletes them itself | The app calls the records service; the controller follows its watch and creates the children |
| Finish with a Sandbox                                                                                                            | Retire it                                                                | Delete it; its history, membership and grants stay                            | Either                                                                                                                | Either; retired or deleted is a row state                                                    |
| Haku moves to a fresh Sandbox ([story 4](user_stories.md#4-haku-itself-lives-here): "the sandbox is disposable and Haku is not") | The old Sandbox's threads go if it is deleted, so it stays retired       | The old threads stay readable                                                 | As D1 or D2                                                                                                           | The old threads stay readable                                                                |
| A coordinator audits its agents, finished ones included                                                                          | Grants on Sandboxes that still exist                                     | Grants matched on History's record of the writer, apart from live grants      | As D1 or D2                                                                                                           | Grants and membership are rows beside the Sandbox rows                                       |
| Grant a git-authored collection                                                                                                  | Flux applies `PolicySet`s; membership is CRs owned by Sandboxes (#10987) | Flux applies `PolicySet`s; a reconciler copies the sets into History (#10990) | Flux applies to the second plane (`spec.kubeConfig`)                                                                  | tofu-controller or an importer writes rows                                                   |
| Try a CRD change on testing first                                                                                                | Not possible: one cluster-scoped CRD set from `devel` serves both        | Not possible                                                                  | Possible: each plane has its own CRDs                                                                                 | Possible: each environment has its own database                                              |
| Inspect records with `kubectl`                                                                                                   | Yes                                                                      | Live ones; retained data through History's API                                | Yes, with the second plane's kubeconfig                                                                               | No; the records service's HTTP API                                                           |
| Thousands of Sandboxes over time                                                                                                 | Retired ones stay in the cluster's etcd, about 10 MB per thousand        | Only live ones stay                                                           | Off the cluster's etcd                                                                                                | Off the cluster's etcd                                                                       |

The 10 MB is arithmetic from staging's object sizes (#10990): a Sandbox with its `PolicyBinding` and
ServiceAccount, `managedFields` included, is about 10 KB. It is not a measurement.

## What each costs, how hard it is to back out of, and what it bets on

|     | Build from today                                                                                                                                                                                                                                                                                                                                                                                         | Back out later                                                    | Bets that                                                                                                              |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| D1  | The Dispatcher skips retired Sandboxes. A delete cascades, held by the Sandbox Controller's finalizer: a History delete RPC (none exists) keyed by a recorded Sandbox UID, Notification's inboxes and entries, and app Threads from the app's own watch, since no service calls the app. History's and the app's documented promise that history outlives the Sandbox changes                            | Cheap: stop cascading. History deleted meanwhile is gone          | The operator never wants a deleted Sandbox's history back, and retired Sandboxes stay affordable in the cluster's etcd |
| D2  | The Sandbox UID recorded in History; a membership table with a git reconciler; archive grants apart from live grants; possibly the History re-key, which touches 11 of its 12 tables and the Electric shapes                                                                                                                                                                                             | Moderate: membership and grants move back into Kubernetes objects | Kubernetes and the databases stay consistent through UID keys and a reconciler                                         |
| D3  | A control plane per environment to run. Children deleted explicitly, since ownership does not cross apiservers. A new Pod-to-Sandbox binding for the Gateway and the launcher relay policy. Token authentication through the primary (not tried). A second client in services that read records and touch cluster objects, such as the app, both controllers, the Gateway, egress and the Action Service | Moderate: the protocol is the same; owner references come back    | Running a control plane costs less than what it buys: a separate etcd, CRDs per environment, CNPG backups              |
| D4  | The records service with a resumable watch, status, conflict checks, cascade deletion, authorization and audit (#10990 rates the watch hardest); every reader ported; a tofu provider or importer. Reverses the [2026-10-14 decision](../docs/adr_sandbox_session_control.md#decisions) that there is no Sandbox Service                                                                                 | Expensive: re-create the CRDs and port every reader back          | Rebuilding the Kubernetes record machinery pays for itself in lifetime and schema freedom                              |

D3 does not by itself keep records out of memory: the apiserver's watch cache is on by default, and
whether turning it off covers custom resources was not checked (#10990). The documented promises are
in the [History README](../history_service/README.md) ("its history outlives the Sandbox") and the
[app README](../app/README.md).

## Right under every direction

Candidates; each still needs the operator's go.

- **Record the writing Sandbox's UID in History and check it on every push.** D1 deletes by it, D2
  keys grants by it, and D3 and D4 still identify a Sandbox by an ID History must record. The re-key
  is not part of this; only D2's reads might need it (`HISTORY_SESSION_KEY_DESIGN`).
- **Send v1 with direct and `allSandboxes` targets only.** Every direction can serve them; collection
  targets wait for membership, which is where the directions differ. This takes the collection
  targets and the membership CR out of #10987.
- **Lease only the Sandboxes that have work.** The Dispatcher's loop wakes on its informer and on
  Postgres notifications and reads the watch cache; its 10 s tick is the Lease heartbeat, sent for
  every Sandbox whatever its state (`runner_gateway/dispatcher/delivery.go`). Ownership is consulted
  only to deliver (`processSession`), and a new owner requeues the Sandbox's in-flight rows
  (`RecoverSandbox` in `store.go`), so a Sandbox needs a Lease only while it has a queued or
  delivering command or opening. The trap: the queue heads list only queued rows, so a rule built on
  them would strand the delivering rows of a replica that crashed. The change cuts the writes in D1 to
  D3, and D4 replaces the Leases. Whether the Postgres fence alone could carry ownership was not
  checked.

## Configurable within sane options

The operator: "one way to make things no-regret might also be to make the 'what lives where'
configurable, or configurable within sane options" (02:19).

- **Which apiserver holds the records.** The outer cluster and an Agentplane-owned apiserver speak the
  same protocol, so each service could take a records connection apart from its cluster connection,
  both pointing at the cluster by default. Two things must first stop assuming one apiserver: the
  Sandbox Controller deletes a Sandbox's children itself instead of leaving them to garbage
  collection, and the Gateway (`runner_gateway/gateway/sandboxes.go`) and the launcher relay policy
  (`cluster/cdk8s/agentplane/launcher_relay.py`) tie a Pod to its Sandbox by something other than an
  owner reference. D3 then becomes a deployment setting that testing can try first.
- **Whether history outlives its Sandbox.** The delete cascade can be a setting, per Sandbox or per
  environment. Membership and grants are the catch: Sandbox-owned CRs work only when history dies
  with the Sandbox, while rows keyed by Sandbox UID work under both settings. Making lifetime
  configurable therefore settles membership in D2's shape.
- **Not a setting: database-backed records (D4).** It is a second backend for every record; offering
  both means building both.

Each setting is another mode to test and keep working, so configurability stays at these two.

## Moves that pick a direction

Held until one is chosen:

- collection membership as Sandbox-owned CRs (#10987: D1, D3) or as History rows (#10990: D2, D4);
- deleting history with its Sandbox (D1), or archive grants apart from live grants (D2);
- a retired state that the Dispatcher and watchers treat specially (D1, D3);
- the History re-key by Sandbox UID (D2);
- any second-plane or records-service work (D3, D4).

## Experiments that would narrow it

- **Agentplane's share of the cluster's control plane.** Read Agentplane's object counts and request
  rates from the staging apiserver's metrics, with a read-only RBAC PR first if `claude-ai` cannot.
  Separates D1 from D3 at scale; #10990 did not read etcd.
- **A nodeless apiserver on Postgres.** k3s, already pinned for `x/k3s_env`, can keep its datastore in
  Postgres through kine. Install the Agentplane CRDs and try what D3 leaves untried: token
  authentication through a primary, the watch cache turned off for custom resources, and a Flux
  `spec.kubeConfig` apply.
- **The price of a retired Sandbox.** On `agentplane-testing`, stop one with `spec.wakeOnSend: false`
  and count its records, Lease writes and watcher cache entries. Checks D1's arithmetic.
- **D4's git glue without a provider of our own.** Whether a generic REST Terraform provider can
  drive a records API. Not tried.

## The map today

### Kubernetes records

- **Sandbox.** A `Sandbox` CR. The app writes desired state, the Sandbox Controller writes status,
  and the Dispatcher patches the wake fields. Operator: deletes a Sandbox when done and wants its
  history kept (2026-10-18 23:29), floated suspending instead of deleting, and offered deleting the
  history with the Sandbox (2026-10-19 02:04).
- **Sandbox children** (Pod or VM, state PVC, ServiceAccount, `<sandbox>-control` Service, the
  Sandbox's own `PolicyBinding`). Owned by the Sandbox and removed with it.
- **Grants and catalog** (`PolicySet`, static `PolicyBinding`s, `SandboxTemplate`,
  `EgressCredential`). Authored in git and applied by Flux; the app writes runtime grants as bindings
  owned by the Sandbox. Operator: wants the Flux-managed shared permission sets kept gitops-driven,
  and floated a second control plane "if it came to it" (2026-10-18 22:54, 23:02).
- **Leases.** `dispatcher-<sandbox UID>`, renewed every 10 s (`LeaseRenewInterval`) for every
  Sandbox, suspended or not, and `<sandbox>-runner` from the Gateway while a runner is connected. The
  Dispatcher also keeps a Postgres `sandbox_ownership` generation fence per Sandbox. #10990 counts 0.1 to 0.17 writes per second per Sandbox and did not
  check whether the Dispatcher Lease is still needed beside the Postgres row.

### Databases and runner storage

- **Session history** (raw Events, observations, settlements, the Thread fold). The History database
  `sandbox_service`, which outlives the Sandbox; four fold tables replicate through Electric to the
  browser. Operator: agent traces and big objects should not go into the control-plane etcd
  (2026-10-18 22:39).
- **Which Sandbox wrote a Session.** History records the namespace and name its writer
  ServiceAccount implies, and no UID ([History README](../history_service/README.md#which-sandbox-wrote-a-session)).
  Proposed: a nullable `session_history.sandbox_uid`, checked on every push, and optionally keys by it
  (`HISTORY_SESSION_KEY_DESIGN`). Operator: wants the per-push check if the UID is recorded (2026-10-17
  04:36); "i'll want to think about it" (04:45).
- **Thread** (the app's link and metadata for a Session). The app database, keyed by Session ID with
  no Sandbox column. Proposed: add `sandbox_uid` only if History re-keys.
- **Commands and Session openings.** The Dispatcher database (`dispatch`, `session_opening`,
  `wake_attempt`, `sandbox_ownership`), every row carrying the Sandbox UID and ended when the Sandbox
  is gone. Proposed: `DISPATCHER_PERSISTENCE_MODEL_DESIGN` compares the durable queue with direct
  admission. Operator: the Dispatcher keeps no Session-to-Sandbox registry (2026-10-17 03:53, 04:12).
- **Inbox entries, notices, subscriptions.** The Notification database; entries survive their
  inbox's retirement when the Sandbox is gone. Proposed: peer messages and sender receipts join this
  store, and the service split stays unscheduled. Operator: wants receipts that tell "was this acked"
  from "did a notice cover this" (2026-10-17 03:30), and may revisit the service with later
  "refactors/splits/reshuffles" (04:18).
- **Action requests, Decisions, connections** (Actions database), **egress admission decisions**
  (egress database), **launch presets** (app configuration, with a label and a compact annotation on
  the Sandbox). Nothing proposed.
- **Runner journal.** One SQLite file per Session on the Sandbox's state volume; History is the
  retained copy once the runner pushes it. Nothing proposed.

### Not stored yet

- **Collection membership** (which Sandboxes a named collection holds, for grants). Proposed: #10987
  makes one Sandbox-owned `SandboxCollectionMembership` CR per Sandbox and collection; #10990
  recommends rows in History's database keyed by Sandbox UID, with git-authored sets reconciled in one
  way. Operator: collections must keep covering Sandboxes "no longer alive" (2026-10-18 23:33); a
  database is "okay, we could still have some kind of reconciler" (2026-10-19 01:10).
- **Session-access grants** (who may send to, or read, which Sandbox's Sessions). History's
  configured `reader_accounts` are the only gate on reads, and the Dispatcher trusts the app and
  Notification to authorize their users. Proposed: `session_access` rules in `PolicySet`s, assigned by
  `PolicyBinding`s and enforced by the owning service
  ([plan](multiagent_communication.md#tentative-implementation-direction)). Operator: sending and
  reading transcripts are "really separate capabilities" (2026-10-18 22:46); archive permissions maybe
  "in a possibly separate universe" from live objects (22:31, tentative); read decisions held
  (2026-10-17 10:56).

## What the operator has said

| Date (UTC)       | Operator                                                                                            | Consequence                                        |
| ---------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| 2026-10-17 03:30 | Sender receipts that tell "was this acked" from "did a notice cover this"                           | Receipts in v1                                     |
| 2026-10-17 03:41 | OAuth callers get an SA through an Actions connection and send from an actions-sandbox              | No separate OAuth sender path                      |
| 2026-10-17 04:12 | Messages come with the Sandbox; wake-on-send does not look it up from a Session                     | No Session-to-Sandbox registry                     |
| 2026-10-17 05:58 | `allSandboxes` in v1, "under some kind of scope of the controller"                                  | Bounded to the PolicySet's namespace               |
| 2026-10-17 10:56 | Keep read-permission decisions held "for now"                                                       | Read grants stay held                              |
| 2026-10-18 22:39 | Agent traces and archives should not go into the control-plane etcd                                 | Per-Session data stays out of Kubernetes records   |
| 2026-10-18 22:46 | Sending and reading transcripts are "really separate capabilities we probably don't need to bundle" | First `session_access` schema carries sending only |
| 2026-10-18 23:19 | No need to name a different namespace                                                               | Direct targets are same-namespace                  |
| 2026-10-18 23:33 | Collections keep covering Sandboxes no longer alive                                                 | Rules out live-members-only collections for reads  |
| 2026-10-19 01:37 | Python and Go ports can be sequenced in any order relative to the other things                      | Ports are not part of this map                     |
| 2026-10-19 02:14 | No definite pick yet on where data and objects live                                                 | No direction chosen                                |

Tentative words, not decisions: archive permissions "in a possibly separate universe" (2026-10-18
22:31), suspending instead of deleting Sandboxes as "one consistent way" (23:29), a second control
plane "if it came to it" (22:54), membership in a database with a reconciler (2026-10-19 01:10), and
deleting a Sandbox's history with it as "one other consistent option" (02:04).

## Assumed by a lane and never confirmed

| Assumption                                                                                                    | Where it lives                              | What the operator said                                                                   |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `session_access` rule shape, `targets` union, one `SandboxCollectionMembership` CR per Sandbox and collection | #10987, plan                                | Only the two target decisions above                                                      |
| Resync of 300 s, refusal after three missed cycles, `policy_resync_seconds`                                   | #10987                                      | Asked "Shouldn't freshness just come from a k8s watch?" The number is not the operator's |
| Payloads expire at 30 days, 128 unacknowledged per sender per inbox, idempotency key, recipient states        | [plan](multiagent_communication.md), #10987 | Never answered that batch; later asked for one question at a time                        |
| History owns membership, keyed by Sandbox UID                                                                 | #10990                                      | Tentatively okay to a database, not to the owner or key; D2 and D4 only                  |
| Records stay in the outer cluster's Kubernetes API                                                            | #10990 "Take"                               | Treated as open (2026-10-19 01:58); D1 and D2 keep it                                    |

## Open PRs that encode unconfirmed defaults

| PR     | Encodes                                                                      | Handling                                                                              |
| ------ | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| #10987 | The draft send schema with the membership CR, freshness and recipient states | The membership CR is D1 or D3; direct and `allSandboxes` sending is direction-neutral |
| #10990 | History as default membership owner; records stay in the outer API           | Its inventory backs D3 and D4; its membership section is D2                           |

## Parked

[API aggregation](api_aggregation_exploration.md) is held by the operator. Nothing waits on it.
