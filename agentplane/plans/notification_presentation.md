# Notification presentation and input metadata

Status: open, unscheduled. The [task DAG](task_dag.md#notification_presentation--compact-notification-rendering)
tracks the dependencies. The metadata's carriage (`SESSION_INPUT_METADATA`) is the operator's lean,
not a settled decision: the Runner Gateway stamps verified provenance on the runner Command and the
runner echoes it into the Event that journals the input, so metadata reaches history with the input.
The gateway keeps no command records ([Runner Gateway API](../runner_gateway/API.md#sessions-and-commands)).

## Goal and boundaries

Give user inputs typed metadata that the harness and model never see. Initially, identify
Notification Service notices so the frontend can render them compactly, with full text available
on expansion. The agent continues receiving the existing actionable notice text.

- The Runner Gateway authenticates the producer and stamps provenance on the Command it forwards.
- Notification Service owns subscriptions, inboxes, entries and delivery bookkeeping, and supplies
  the notice reference.
- Runners journal the metadata with the input and stay responsible for execution and canonical
  admission/confirmation evidence. Claude/Codex integrations do not interpret it.
- The integration app consumes authorized history and renders annotations. No backend depends on
  app tables, availability or identity.

This does not add native subsessions, change notice pacing, acknowledge inboxes through rendering,
introduce another service, or provide notification-triggered startup/wake.

## Metadata content

Metadata is a typed, bounded schema, never an arbitrary dictionary, and valid only on
`submit_input`; controls take none. Plain human input needs no notification attachment. Define the
notice identity and cursor fields from the existing persisted delivery record; do not mint another
identity if the command ID already uniquely identifies that notice. A notice may cover multiple
sources. Start with stable inbox/range references; add only bounded source descriptors needed for
presentation, not full provider payloads or a single misleading source label for a batch.

## Trusted provenance

Attaching notification metadata requires an additional narrowly scoped permission granted only to
the Notification Service workload identity. Confirm the actual service authentication/policy
integration before choosing the permission representation; a claimed service name in a header or
request body is not authentication.

Unauthorized metadata receives a permission-denied response before dispatch, not silent stripping.
Notification Service must still be authorized to submit to the target session. The gateway stamps
trusted notification provenance after authorization; callers supply notice references, not their own
trusted producer identity. No new signatures or callback validation service is required.
Notification Service remains responsible for reference correctness.

Provenance means the service submitted this notice, not that provider text is trusted. Never infer
provenance from text prefixes. Metadata is excluded from model input, but is not thereby secret:
read APIs must enforce session access and return only authorized fields.

## History and read path

The echoed metadata is part of the journalled input, so it survives replay, inbox expiry and
subscription cancellation without cross-service joins or a live inbox. A repeated command ID with
different metadata must conflict, not overwrite.

The Thread fold joins annotations to messages through
`HarnessUserMessageConfirmed.origin_command_ids`, including pending or failed inputs where no
confirmed message exists yet. An enriched view is not a new canonical execution Event.

Coalesced inputs may mix human text and notices. Preserve metadata per originating command. Origin
IDs do not prove substring boundaries: do not collapse a whole mixed message or attempt to extract
human text using prefix heuristics. Initially render mixed messages normally with annotations.

## Frontend behavior

- Compact notification-only messages by default, with accessible expansion to full retained text.
- Use verified provenance, never text matching, to select notification presentation.
- Missing or unknown metadata falls back to ordinary text rendering.
- Provider titles/descriptions remain untrusted content and must be rendered safely.
- Keep mixed-origin messages readable; never hide human input.
- Rendering, expansion and reading metadata do not acknowledge an inbox.

## Implementation sequence and acceptance

The [DAG](task_dag.md#2-service-owned-command-admission-and-later-notification-presentation) owns
status and edges:

1. `SESSION_INPUT_METADATA`: add typed annotations and producer authorization.
2. `SESSION_INPUT_METADATA_READ` and `NOTIFICATION_NOTICE_METADATA`: authorized annotation reads and
   producer integration after the metadata extension exists.
3. `NOTIFICATION_PRESENTATION`: compact rendering after metadata reads and producer integration.

Tests cover ordinary input, restricted provenance, destination authorization, identical/conflicting
retries (including concurrent requests), pending/rejected inputs, replay and mixed-origin
coalescing. Keep metadata out of what the harness receives; keep historical annotations after inbox
expiry. Exercise backend integration without the app. Add frontend visual coverage and one bounded
real-notice demonstration with unchanged agent-facing text. No provider-outage injection or repeated
harness matrix is needed; rendering must not acknowledge the inbox.
