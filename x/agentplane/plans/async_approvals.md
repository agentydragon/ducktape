# Asynchronous approvals and delivery

Status: **the human Decision path is implemented; pending/result delivery and notification handling
remain open.** This plan is the `DEL` gate in [`task_dag.md`](task_dag.md), not a second request or
tool lifecycle.

## Preserved decisions

- Native harness approval prompts remain off. Sandboxes are the harness blast radius; a
  credential-bearing action must execute outside the harness rather than handing the credential to
  a native prompt path.
- `ActionRequest` is the invariant request shape for both human-reviewed and future auto-decided
  actions.
- Submission is non-blocking. A caller receives a durable `decision_pending` receipt and may
  continue; the Decision/result should arrive later as a machine-readable Thread input.
- Decision and Execution are separate. An allow auto-dispatches at most one Execution; a deny creates
  none.
- The v0 DecisionProvider is human/operator-backed.
- There are no blind retries. An ambiguous in-flight loss becomes `execution_unknown`.
- Push/UI approval is a delivery channel into the same DecisionProvider, never a second authority.
- Standing grants are separate access objects, not repeated Executions of one ActionRequest.

## Observed evidence from the landed Action Service

PR [#5700](https://github.com/agentydragon/ducktape/pull/5700) proves:

- a workload-authenticated caller can submit one request and read only its own redacted state;
- an independently authenticated operator can list/read all requests and issue `allow` or `deny`;
- expected-version checks reject stale decisions;
- provider/issuer/idempotency checks make duplicate same decisions harmless and conflicting reuse an
  error;
- concurrent duplicate Decisions produce one winning Decision and at most one Execution;
- allow auto-dispatches while deny does not;
- the state event sequence is durable and ordered;
- unsafe restart state becomes `execution_unknown` instead of replaying; and
- each new pending request writes a durable outbox reference containing only `request_id` and
  `capability`, not arguments or credentials.

That evidence stops at the Action Service boundary. `NullNotificationOutbox.wake()` has no delivery
behavior, no process drains `action_outbox`, and no event is injected into a Thread.

## Open delivery contract

### P0 behavior

A caller submits an ActionRequest, continues work, and later receives one durable, redacted input
that says whether the request remains pending, was denied, or executed and produced a result/error.
The same state is visible to the operator without a second lifecycle.

### Needed support

1. **Outbox claim and acknowledgement.** Define which process drains pending rows, how it claims work,
   when `delivered_at` is written, and how restart redelivery remains idempotent.
2. **Thread destination.** Define the authoritative origin reference needed to address a Thread.
   Current `origin`/`correlation` are untrusted provenance and cannot silently become authorization or
   routing truth.
3. **Thread-state delivery.** Pin idle/live, idle/resumed, and active-turn behavior against the runner
   contracts. A later input is the default; do not reintroduce a blocked native approval prompt.
4. **Machine envelope.** Define the minimum receipt/Decision/Execution fields, version, redaction,
   and references. The Action schema gate owns capability-specific input/result sensitivity.
5. **Withdrawal.** Define who may withdraw before Execution starts, expected-version behavior, and
   the final event. Do not add post-dispatch cancellation unless the first adapter can prove its
   semantics.
6. **Notification adapter.** Send only an opaque request reference and redacted summary/deep link.
   Approve/deny callbacks must authenticate the operator, bind the intended verdict, include an
   idempotency/current-version check, and call the existing Decision route.
7. **Unknown outcome.** Define the agent-visible message for `execution_unknown` and whether the
   concrete adapter exposes an authoritative status lookup. Status reconciliation may update the
   existing Execution; it never starts another one.
8. **Batching.** If several events target one Thread together, preserve each event and ordering while
   using one later input. Do not build urgency classes before an observed need.

### Acceptance evidence

Use a scripted scenario with the fixture executor first, then repeat it for the first real adapter:

1. submit returns `decision_pending` without blocking;
2. a durable outbox row survives a delivery-process restart;
3. a redacted pending notification is sent once logically despite duplicate delivery attempts;
4. allow and deny callbacks race through the same DecisionProvider, with one final Decision;
5. allow produces exactly one Execution and deny produces none;
6. the originating idle/live, idle/resumed, and active Thread receives the defined envelope;
7. duplicate callback/outbox delivery does not create a second Decision, Execution, or Thread event;
8. sensitive arguments, reviewer reason, credentials, and backend exception text do not cross their
   allowed projections; and
9. ambiguous dispatch loss reaches the Thread as `execution_unknown` without backend replay.

## Deferred

- expiry merely because queues conventionally expire;
- operator-presence heuristics;
- LLM DecisionProvider or policy DSL;
- standing-grant issuance through ActionRequest;
- a general notification bus or subscription framework; and
- cross-agent delivery before Agent identity and read policy exist.
