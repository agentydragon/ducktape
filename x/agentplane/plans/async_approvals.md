# Asynchronous approvals and delivery

Status: **the human Decision path, synchronous DecisionProvider aggregation, and durable Action
event/query polling are implemented; human-provider notification, withdrawal, unknown-outcome,
progress, and batching remain open.** This plan is the `DEL` gate in [`task_dag.md`](task_dag.md),
not a second request or tool lifecycle.

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
- The v0 DecisionProvider is human/operator-backed. Future auto-approval policies are modular
  DecisionProviders over the same ActionRequest and Decision lifecycle, not a second request or
  authority path. A provider makes an authoritative provider decision of `allow`, `deny`, or
  `no_opinion`; it is not merely returning a UI hint or proposal. Each outcome includes a bounded
  provider-authored reason code and description suitable for the Action audit/projection; it does
  not expose unrestricted internal reasoning.
- There are no blind retries. An ambiguous in-flight loss becomes `execution_unknown`.
- Push/UI approval is a delivery channel into the same DecisionProvider, never a second authority.
  Pending human decisions may eventually produce a push notification. Final Decision/Execution
  delivery into the originating Agent/Thread belongs to the later Event & Notification Hub, not this
  initial Action Service/MCP execution slice.
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
- each new pending request records a durable Action event without arguments or credentials.

That evidence stops at the Action Service boundary. No originating-Thread consumer is required for
this initial MCP execution slice.

PR [#5732](https://github.com/agentydragon/ducktape/pull/5732) adds synchronous DecisionProvider
aggregation on top of that: configured providers run concurrently to completion (never first-wins),
deny dominates, otherwise allow, otherwise the request defers unchanged to the existing human path;
a provider timeout/exception is `no_opinion`, never `allow`, with its raw text never persisted or
logged; and human and auto-provider Decisions commit through the same optimistic-version/idempotency
path, so whichever wins a race, the loser's stale callback surfaces as an explicit already-decided
conflict rather than overriding the winner. No real policy provider or Thread notification is added;
production still runs with zero configured providers.

## Open delivery contract

### P0 behavior

A caller submits an ActionRequest, continues work, and can query one durable, redacted Action state
that says whether the request remains pending, was denied, or executed and produced a result/error.
Originating-Thread notification is a later integration node, not a prerequisite for proving that an
Agent can use an Action backed by MCP. Synchronous non-human providers are evaluated first; the
default aggregation is any `deny` -> deny, otherwise any `allow` -> allow, otherwise defer to the
asynchronous human provider.

### Needed support

1. **Machine envelope.** Define the minimum receipt/provider-outcome/Decision/Execution fields, reason
   codes, bounded explanations, redaction, and references. The Action schema gate owns projection
   rules, not a generic `sensitivity` field on every Action.
2. **Human provider contract.** Notify the human provider that a new ActionRequest needs a Decision,
   then let its authenticated UI/notification client call the Action Service's canonical decision
   endpoint. The endpoint returns a stale/already-decided result when another provider won; it never
   creates a parallel human lifecycle.
3. **Withdrawal.** Define who may withdraw before Execution starts, expected-version behavior, and
   the final event. Do not add post-dispatch cancellation unless the first adapter can prove its
   semantics.
4. **Unknown outcome.** Define the Agent/API-visible state for `execution_unknown` and whether the
   concrete adapter exposes an authoritative status lookup. Status reconciliation may update the
   existing Execution; it never starts another one.
5. **Progress.** If an adapter is long-running, define bounded status/progress observations and
   authorized output-so-far reads. Progress must be tied to the existing Execution and must not
   create a second claim, retry, or completion path.
6. **Batching.** If several events target one Action API consumer together, preserve each event and
   ordering in the Action event/query surface. Thread-specific batching belongs to the later Event &
   Notification Hub.

**Landed:** the durable Action event sequence, cursor-based (`after_sequence`) query behavior, and
process-restart recovery, without a second source-of-truth queue. See
`x/agentplane/action_service/README.md`.

### Acceptance evidence

Use a scripted scenario with the fixture executor first, then repeat it for the first real adapter:

1. submit returns `decision_pending` without blocking;
2. the durable Action event sequence survives an Action Service restart;
3. repeated reads/callbacks do not duplicate the pending Decision or Execution;
4. synchronous allow/deny/no-opinion providers aggregate with deny dominance and human fallback;
5. human allow and deny callbacks race through the same canonical Decision route, with one final Decision;
6. allow produces exactly one Execution and deny produces none;
7. duplicate callback/event reads do not create a second Decision or Execution;
8. the bounded provider `reason_code`/`reason_description` and safe terminal result reach the caller
   projection, while sensitive arguments, the operator's `private_reason`, credentials, and backend
   exception text do not; and
9. ambiguous dispatch loss reaches the Action API as `execution_unknown` without backend replay.

## Deferred

- expiry merely because queues conventionally expire;
- operator-presence heuristics;
- LLM DecisionProvider or policy DSL;
- standing-grant issuance through ActionRequest;
- a general notification bus or subscription framework; and
- cross-agent delivery before Agent identity and read policy exist.
