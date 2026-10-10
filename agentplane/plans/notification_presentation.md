# Notification presentation and input metadata

Status: follow-up to [generic command admission](command_admission.md), not part of its initial
persistence/routing or outbound-channel rollout. The [task DAG](task_dag.md#notification_presentation--compact-notification-rendering)
tracks the dependencies. Review metadata/provenance after admission settles; do not add notification
fields or producer integration to the admission foundation. This plan replaces the earlier proposal
to carry presentation metadata through runner commands and journals.

## Goal and boundaries

Give user inputs typed metadata that is not passed to the runner or harness. Initially, identify
Notification Service notices so the frontend can render them compactly, with full text available
on expansion. The agent continues receiving the existing actionable notice text.

- Sandbox Service owns input submission, authenticated provenance and durable input metadata.
- Notification Service owns subscriptions, inboxes, entries and delivery bookkeeping.
- Runners remain responsible for execution and canonical admission/confirmation evidence. They
  neither receive nor interpret this metadata; Claude/Codex integrations do not change.
- The integration app consumes authorized service APIs and renders annotations. No backend
  depends on app tables, availability or identity.

This does not add native subsessions, change notice pacing, acknowledge inboxes through rendering,
introduce another service, or provide notification-triggered startup/wake.

## Submission API

Extend the service command-submission envelope without changing the runner Command. Illustrative
JSON (not a choice of HTTP over the existing service transport):

```json
{
  "command": {
    "commandId": "...",
    "submitInput": { "text": "Agentplane inbox notice: ..." }
  },
  "metadata": {
    "notification_notice": {
      "inbox_id": "...",
      "through_cursor": 123
    }
  }
}
```

The destination/session remains scoped by the service API. Metadata is a typed, bounded schema,
not an arbitrary dictionary. Plain human input needs no notification attachment. Define the exact
notice identity and cursor fields from the existing persisted delivery record; do not mint another
identity if the command ID already uniquely identifies that notice. A notice may cover multiple
sources. Start with stable inbox/range references; add only bounded source descriptors needed for
presentation, not full provider payloads or a single misleading source label for a batch.

Sandbox Service persists the complete service envelope and immediately sends only a copy of its
runner Command. Notification metadata is valid only on `submit_input`; controls continue through
the same admission surface without it. The RPC returns OK only on runner admission, not merely
service persistence. Metadata never enters the runner or harness.

## Trusted provenance

Use the existing authenticated caller and destination authorization. Attaching notification metadata
requires an additional narrowly scoped permission granted only to the Notification Service workload
identity. Confirm the actual service authentication/policy integration before choosing the permission
representation; a claimed service name in a header or request body is not authentication.

Unauthorized metadata receives a permission-denied response before persistence or dispatch, not
silent stripping. Notification Service must still be authorized to submit to the target session.
Sandbox Service stamps trusted notification provenance after authorization; callers supply notice
references, not their own trusted producer identity. No new signatures or callback validation service
is required. Notification Service remains responsible for reference correctness.

Provenance means the service submitted this notice, not that provider text is trusted. Never infer
provenance from text prefixes. Metadata is excluded from model input, but is not thereby secret:
read APIs must enforce session access and return only authorized fields.

## Admission dependency and metadata persistence

[Durable command admission](command_admission.md) owns the base persistence, immediate-dispatch,
retry and spool-reconciliation contract for all runner commands. This notification extension is a
later phase, outside the initial command-admission PR; it must not add another input-only queue.
The source trace and admission recovery requirements live in that owning plan.

After the admission contract settles, extend its service-owned record with typed metadata and
server-stamped producer provenance. Preserve an immutable accepted snapshot across replay, inbox
expiration and subscription cancellation; no cross-service database joins or live inbox are required
for historical rendering. Conflicting metadata on a repeated command ID must conflict, not overwrite.
Review schema/auth integration, notice fields and retention with the owning command model.

## Correlation and read path

Existing runner evidence already supplies the necessary joins:

- Command admission identifies the submitted command.
- `HarnessUserMessageConfirmed.origin_command_ids` identifies originating commands for a confirmed
  harness message, alongside its harness message ID.

Sandbox Service exposes input metadata keyed by session/command identity through an authorized read
API. The message projection uses origin IDs to attach annotations, including pending or failed inputs
where no confirmed message exists yet. Preserve annotation availability across replay/reconnect and
client archival without rewriting canonical runner Events or requiring runners to echo metadata.
Select the exact read/projection integration after inspecting current archive ownership; an enriched
view is not a new canonical execution Event.

Coalesced inputs may mix human text and notices. Preserve metadata per originating command. Origin
IDs do not prove substring boundaries: do not collapse a whole mixed message or attempt to extract
human text using prefix heuristics. Initially render mixed messages normally with annotations.

## Frontend behavior

### Submission status TODO

Extend the command-state dots with **stored by Sandbox Service; runner admission unconfirmed**
between browser retention and runner admission. Drive this from authorized submission status reads
or a feed, including recovery after a lost RPC response; a waiting RPC alone does not expose the
intermediate state. Do not label it definitely unsent or guaranteed eventual delivery. Keep runner
admission distinct from harness confirmation. This status work is independent of compact notice
rendering and must not give the app dispatch ownership.

### Notification rendering

- Compact notification-only messages by default, with accessible expansion to full retained text.
- Use verified provenance, never text matching, to select notification presentation.
- Missing or unknown metadata falls back to ordinary text rendering.
- Provider titles/descriptions remain untrusted content and must be rendered safely.
- Keep mixed-origin messages readable; never hide human input.
- Rendering, expansion and reading metadata do not acknowledge an inbox.

## Implementation sequence and acceptance

The [DAG](task_dag.md#2-service-owned-command-admission-and-later-notification-presentation) owns status and edges:

1. `SESSION_COMMAND_CONTRACT`: review the generic durable command admission contract, which already
   runs over the existing relay.
2. `SESSION_COMMAND_STATUS_READ` and `SESSION_COMMAND_STATUS_UI`: expose authorized admission status
   and the service-retained state dot, independently of notification rendering.
3. `SESSION_INPUT_METADATA`: after generic admission settles, add typed annotations and producer
   authorization. This is not part of the initial command-admission PR.
4. `SESSION_INPUT_METADATA_READ` and `NOTIFICATION_NOTICE_METADATA`: authorized annotation reads and
   producer integration after the metadata extension exists.
5. `NOTIFICATION_PRESENTATION`: compact rendering after metadata reads and producer integration.

Tests cover ordinary input, restricted provenance, destination authorization, identical/conflicting
retries (including concurrent requests), crashes around dispatch, pending/rejected inputs, replay
and mixed-origin coalescing. Cover admission before a lost reply, ingestion racing an RPC receipt,
and crash/replay at the reconciliation checkpoint; prove none strands or regresses admission state. Keep
metadata out of runner commands; keep historical annotations after inbox expiry. Exercise backend
integration without the app. Add frontend visual coverage and one bounded real-notice demonstration
with unchanged agent-facing text. No provider-outage injection or repeated harness matrix is needed;
rendering must not acknowledge the inbox.
