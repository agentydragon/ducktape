# The Matrix channel

Matrix as one channel: everything that knows a homeserver exists. What the channel guarantees
the operator is <SPEC.md>; this README is the module map.

- `client.py` — the client-API calls the loop makes, over `matrix-nio`.
- `sync.py` — logs in as `@haku`, long-polls `/sync` (one owner for the user-wide token), binds
  each room the operator invites Haku into, and dispatches inbound events by room to their
  attached conversations. Holds the only Matrix credential, so everything that speaks into a
  room speaks through it; hosts the per-attachment reconcilers on the sync leader.
- `conversation.py` — each room's attachment to a conversation and ingress (`Turns`).
- `attachment_reconciler.py` — one owner per live attachment: its conversation cursor, reply
  outbox, span revisions and send budget; the sync leader sweeps the set per pass.
- `pacer.py` — one paced outbound queue per room, over Synapse's `rc_message` budget, addressed
  by the attachment (`RoomPacers`).
- `outbox.py` — the rooms' outbox: replies as `matrix_outbox` rows, and one drain per
  attachment that says them.
- `outbox_wake.py` — the outbox's own wake wire: the enqueue's transaction waking the drains.
- `revisions.py` — which homeserver event the channel is currently editing for a revisable
  subject.
- `spans.py` — the editable lines as spans of the conversation: the pure fold from the stream
  to each span's bounded body, its close, and the reconcile latches over a `RoomFrontend`.
- `conversation_subscriber.py` — the Matrix channel's subscriber to the conversation record,
  one per attachment: its durable position (`channel_cursor`), the replies it queues, the
  notices it seals, the span lines it reconciles.
- `room_copy.py` — the room's durable copy of projected events, read off their `/sync` echoes.
- `ingress_ledger.py` — which inbound events a prompt in the record carries.
- `formatted_body.py` — Haku's Markdown into the HTML subset Matrix clients render.

**The room reads the record; the turn loop never pushes at it.** The subscriber's module
docstring (<conversation_subscriber.py>) is that contract.

## What necessarily lives outside this directory

- `MatrixConfig` and `Settings.matrix` in <../../config.py>. Absent config, or a config whose
  reflected bot password has not landed yet, means the surface does not start and the console
  does.
- The `Matrix*` rows (`MatrixAccessToken`, `MatrixSyncWatermark`, `MatrixOutbox`,
  `MatrixRevision`, `MatrixRoomCopy`, `MatrixIngressEvent`) in <../../database_schema.py>, plus
  their Alembic revisions — migrations are one lineage for the whole database. The rows keep
  the `Matrix` prefix there: the central schema namespace is where it disambiguates.
