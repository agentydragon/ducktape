# History tags and History's Sandbox records

Status: **agreed with the operator, 2026-10-19 to 2026-10-20 UTC; not implemented.** Inbox tags, the
other tag kind, shipped; their behavior is in the [Sandboxes README](../sandboxes/README.md).
Implementation gates are in the [task DAG](task_dag.md#3-multiagent-decisions-before-implementations).
The costs that led here and the directions weighed are in the
[architecture overview](architecture_overview.md#keeping-finished-sandboxes-as-records).

History tags group Sandboxes to grant history reads, as inbox tags group them to grant sends. History
keeps its own record of each Sandbox, so a Sandbox's history and the grants that read it outlive the
Sandbox.

## Tags

History tags are a set-typed list in `Sandbox.spec`, beside `spec.inboxTags`, written and gated the
same way (Flux and the app; Sandbox RBAC only).

|                                | Inbox tags (shipped)                     | History tags                            |
| ------------------------------ | ---------------------------------------- | --------------------------------------- |
| Gates                          | sends into the Sandbox's Session inboxes | reads of the Sandbox's retained history |
| Checked by                     | Notifications, against the live CR       | History, against its own record         |
| Once the Sandbox CR is deleted | gone with it                             | kept by History as it last saw them     |

The operator keeps them apart because they are "permission gates for different services", and
inbox tags do "not make sense to archive" (2026-10-20 00:21). Each is named for the service where it
counts (04:04). "Transcript tags", with History renamed to a transcript service, is a candidate
(04:05, `HISTORY_SERVICE_RENAME` in the [task DAG](task_dag.md#8-ownership-and-naming-cleanup)).

## Grants

Reads take a different kind of rule from sends, as they take a different kind of tag (2026-10-21
03:43). Its shape is held with `THREAD_READ_POLICY_DESIGN`. Whatever its shape, a read grant matches
history tags on History's record, never on the live CR, so a read is checked the same way before and
after the Sandbox is deleted.

## History's record of a Sandbox

History keeps one row per Sandbox in its own database: UID, namespace, name and history tags. Not a
Kubernetes object: "since history manages all the historical bulk in the database, it should
probably not split its storage" (2026-10-20 00:12).

- History watches Sandboxes read-only and copies `historyTags` into the record (00:32). It has no
  finalizer and handles no deletion, so a deleted Sandbox's record keeps the last values seen. A
  history-tag edit made while History is down just before the deletion is lost until a retag.
- The [service boundaries](../docs/service_boundaries.md) rule "never routes to, reads or manages
  Sandboxes" relaxes to reading their history tags (2026-10-19 23:29). The rule's text changes with
  the change that adds the watch.
- History tags are stored once per Sandbox, never copied into Session rows, and grants match them at
  read time, so retagging a deleted Sandbox later needs no backfill.
- Each Session links to its record by the Sandbox UID History records at bind
  ([README](../history_service/README.md#session-writer-and-execution-sandbox)).

## Deleting a Sandbox

Deleting the CR removes it and its children, its PVCs and its Dispatcher rows. History keeps the
transcript and its record, so the history stays readable under direct, `allSandboxes` and history-tag
grants. Nothing restores a Sandbox from History: the operator would not ask to "make a new Sandbox
and make it restore and continue that history" (2026-10-19 09:53). A Sandbox meant to run again is
stopped instead, with `spec.wakeOnSend: false`, and stays a CR.

## Steps

1. In the Go History Service: history tags and History's record (`HISTORY_SANDBOX_RECORD`).

## Deferred

- Retagging a deleted Sandbox. It must stay possible, from the app (2026-10-19 10:43), through a
  History write, since the CR is gone.
- Purging a Sandbox's history (2026-10-19 23:33).
- Dropping a Sandbox's workspace without deleting the Sandbox (2026-10-19 23:32).
