# A2A suitability evaluation

Status: **evaluated and not adopted**. This records the A2A 1.0 evaluation requested during design
review. It is not an implementation plan, experiment lane, or future commitment. The evaluated A2A
repository commit is
[`c0f30b35`](https://github.com/a2aproject/A2A/tree/c0f30b35390c59d2cc398a1100823a9115b97a20)
(latest GitHub release `v1.0.1` on 2026-05-28).

## Decision

Do not use A2A for Agentplane. It is not the harness-neutral control protocol, not the
web UI API, not an agent-to-agent facade in this design, and not part of the initial or planned
experiment matrix.

A2A's intentionally opaque task boundary is useful for independently implemented agents that
exchange tasks, messages, artifacts, and coarse status. Agentplane is solving a
different problem: supervising native harnesses while preserving exact protocol evidence,
admission boundaries, queue and steering behavior, interruption, runtime fencing, and recovery.
Using A2A here would require either hiding the evidence the control plane exists to understand or
adding private extensions so extensive that A2A would no longer supply the neutral language.

## What A2A supplies

A2A has reasonable concepts for an external delegation boundary:

| External delegation need | A2A 1.0 concept                |
| ------------------------ | ------------------------------ |
| Conversation scope       | `context_id`                   |
| Delegated unit of work   | `Task`                         |
| Caller/agent content     | `Message` with typed `Part`s   |
| Working/terminal status  | `TaskStatusUpdateEvent`        |
| Deliverable output       | `Artifact` / artifact updates  |
| Long-running response    | streaming message operation    |
| Later follow-up          | another message in the context |
| Cancellation             | task cancellation              |

The normative model supports text, files, URLs, structured JSON parts, task and artifact metadata,
streaming updates, multiple bindings, and declared extensions. Those are enough for a useful opaque
coding-agent service. They are not enough for this harness supervision boundary.

Relevant pinned sources:

- [A2A goals and opaque-execution principle](https://github.com/a2aproject/A2A/blob/c0f30b35390c59d2cc398a1100823a9115b97a20/docs/specification.md#L13-L41)
- [`Task`, `Message`, `Part`, and `Artifact`](https://github.com/a2aproject/A2A/blob/c0f30b35390c59d2cc398a1100823a9115b97a20/specification/a2a.proto#L167-L293)
- [streaming task status and artifact updates](https://github.com/a2aproject/A2A/blob/c0f30b35390c59d2cc398a1100823a9115b97a20/specification/a2a.proto#L296-L321)

## Why it does not fit this boundary

A2A's standard stream contains tasks, messages, task-status updates, and artifact updates. It does
not define the native lifecycle needed to answer:

- Was an input only durably accepted, written to a process, admitted by a native queue, or delivered
  to an active execution boundary?
- Does the provider own a prompt queue, and can a pending prompt be dequeued?
- Did steering target the active run or become a later prompt?
- Which exact native request, response, notification, or frame supports a terminal/recovery claim?
- Can a replacement Runtime safely resume without a partitioned predecessor still causing side
  effects?

A wrapper could place tool progress in `WORKING` messages, structured parts, artifacts, or private
extensions, but that would not discover or standardize these semantics. They must be learned from
Claude and Codex directly.

A2A also does not define:

- central input commit versus bridge durability versus native admission;
- runtime-generation fencing and stale-writer rejection;
- bridge-log sequence/ack exchange;
- native process generation inside a surviving Pod;
- exact native-frame storage and replay;
- Kubernetes Sandbox/PVC lifecycle;
- harness Thread start/resume evidence; or
- uncertain dispatch after a crash near side effects.

These remain in the private bridge protocol and PostgreSQL schema. No A2A projection is planned.

## Existing wrapper evidence

[`a2acode`](https://github.com/kanywst/a2acode/tree/12b4b20cf8a1f6f129704b5580a1da4176bb5072)
is the most relevant implementation found in the preflight. It serves coding agents over A2A 1.0,
maps assistant text/reasoning/plan/diffs into artifacts, maps tool starts/outcomes into working
status messages, and uses A2A context ids for continuity. It is Apache-2.0 and has an offline echo
backend.

It remains useful only as external prior art and evidence that A2A can wrap a coding agent at an
opaque task boundary. It is not the harness supervisor for this architecture. Its Claude paths use
ACP or the Claude Agent SDK and do not own the exact CLI stream/control frames, queue/admission
evidence, Kubernetes runtime fencing, bridge log, or crash-window recovery model required here.

## Consequence

Remove A2A from the architecture topology, protocol model, implementation lanes, and experiment
matrix. If a future product independently needs interoperability with third-party opaque agents, it
should begin as a new requirements exercise rather than inheriting a presumed commitment from this
evaluation.
