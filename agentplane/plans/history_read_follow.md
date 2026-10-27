# History read and follow

A proposal for [`HISTORY_READ_FOLLOW_DESIGN`](task_dag.md#history_read_follow_design--read-and-follow-one-sessions-history),
not a decision. The operator, 2026-10-26 UTC, on orchestrating agents from an MCP client: "i think this would already
need to involve reads from history, so there would be at least a bit of grpc to design". It sketches one call that
reads one Session's raw history from a chosen start and optionally keeps following it, sized for the backend
consumers below.

## Consumers

| Consumer                                                                                     | Kinds it reads                                                                                       | Start                             | Follows                     |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------- | --------------------------- |
| Notifications command outcome (`notification_service/worker/deliver.go`), if it stays a read | `command_admitted`, `command_failed`, `command_noop`, `harness_user_message_confirmed`               | Its stored cursor                 | Until its command's outcome |
| Orchestration `read_turns`                                                                   | `turn_started`, `turn_completed`, `item_started`, `item_completed`, `harness_user_message_confirmed` | N turns back                      | No                          |
| Orchestration `wait_for_turn`                                                                | `turn_completed`                                                                                     | The cursor the last call returned | Up to the tool's wait       |
| `SESSION_ACTIVITY_STREAM`                                                                    | `turn_completed`                                                                                     | The tail                          | Yes                         |

The orchestration rows are the [orchestration plan's](mcp_orchestration.md#reading-turns-depends-on-historys-read-and-follow-design)
needs. The browser stays on Electric over the fold tables ([thread sync](thread_sync/README.md)): every consumer above wants
runner entries of a few kinds, none wants fold rows, and no gap in Electric has been named that this would fill.

## Recommendation

Raw runner entries with a kind filter, for backend readers only. Fold entities hold item text as payload references
(`history_service/fold/views.py`), carry a projection epoch a follower would have to restart on, and are what Electric
already serves; the runner's `Event` is the vocabulary every consumer above already decodes.

```proto
service HistoryService {
  // Reads one Session's committed entries from `start`; with `follow`, keeps sending them as they commit.
  rpc FollowSession(FollowSessionRequest) returns (stream FollowSessionResponse);
}

message FollowSessionRequest {
  string session_id = 1;
  oneof start {
    // Entries after this cursor. 0 is the beginning.
    uint64 after_cursor = 2;
    // From the count-th most recent entry of `kind`, or the beginning when there are fewer.
    StartBack back = 3;
    // Only entries committed after the stream opens.
    google.protobuf.Empty tail = 4;
  }
  // Empty sends every kind.
  repeated EventKind kinds = 5;
  // False: the stream ends after the first `caught_up`.
  bool follow = 6;
}

message StartBack {
  EventKind kind = 1;
  uint32 count = 2;
}

// One value per case of the `Event.observation` oneof, numbered as its field.
enum EventKind {
  EVENT_KIND_UNSPECIFIED = 0;
  EVENT_KIND_COMMAND_ADMITTED = 8;
  EVENT_KIND_HARNESS_USER_MESSAGE_CONFIRMED = 11;
  EVENT_KIND_TURN_STARTED = 12;
  EVENT_KIND_TURN_COMPLETED = 13;
  // ... every other case.
}

message FollowSessionResponse {
  oneof frame {
    Entries entries = 1;
    // Every committed entry through `through_cursor` has been sent or filtered out.
    google.protobuf.Empty caught_up = 2;
    SettledDeltas settled = 3;
    CursorRange removed = 4;
    // Planned renewal: reconnect with `after_cursor` = the last `through_cursor` and fresh authorization.
    google.protobuf.Empty reconnect_required = 5;
  }
  // Highest cursor this stream has covered, sent or filtered. Resuming after it misses and repeats nothing.
  uint64 through_cursor = 6;
}

message Entries {
  repeated agentplane.proto.EventEntry entries = 1;
}
```

## Semantics

- **Resume.** `through_cursor` covers filtered entries too, so a reader that keeps the last one it saw and reopens
  with `after_cursor` sees no gap and no duplicate. A follow that commits nothing of its kinds still sends
  `caught_up` as the tail moves, so the cursor advances.
- **Settlements.** Sent only when `kinds` is empty or names a delta kind (`text_delta`, `tool_arguments_delta`,
  `tool_output_delta`, `native`): every other kind is retained, and an `item_completed` holds the settled text.
  When sent, they follow `ReadSessionEvents`' rule, including settlements of ranges the stream already passed.
- **Removed ranges.** Sent whenever one intersects what the stream has covered, including a range removed after the
  stream passed it, so a reader that kept entries can drop them.
- **Projection epoch.** Not involved: raw entries keep their cursors across fold rebuilds.
- **Bounds.** A frame holds up to 128 entries or 1 MiB, an entry larger than that alone. The server sends the next
  frame only when the previous `Send` returns, so a slow reader slows its own stream and holds one frame of memory.
- **Wakes.** A follower subscribes to the existing `agentplane_session_history_changes` notification through the
  Service's wakeup hub before its first read, and re-reads its tail on each wake. The notification carries no
  Session, so every follower re-reads on any commit; adding the Session to it is an optimization once followers
  number more than a few dozen.
- **End.** A follow ends with `reconnect_required` when its lease (`watch_lease_s`) ends or the replica drains, as
  `WatchSessionSummaries` does; bare EOF is a failure. A non-follow ends after `caught_up`.
- **Errors.** `NOT_FOUND` for a Session History never recorded; `OUT_OF_RANGE` for an `after_cursor` above the
  committed cursor; `INVALID_ARGUMENT` for `EVENT_KIND_UNSPECIFIED`, a `count` of 0 or no `start`;
  `PERMISSION_DENIED` for a caller outside `reader_accounts`.
- **Authorization.** `reader_accounts`, checked when the stream opens; the lease bounds how long a removed reader
  keeps reading. `THREAD_READ_POLICY` replaces the allowlist with per-Session checks at the same points.
- **Storage.** `session_event.payload` is opaque bytes, so a filter or a `back` start would decode every entry. A
  migration adds the oneof field number as a `kind` column, filled at ingest and backfilled in its own PR, with an
  index on `(session_id, kind, cursor)`.

## What the orchestration executor builds on it

- `read_turns(N)`: `back {TURN_STARTED, N}`, the five kinds in the table, no follow. The executor groups entries by
  turn: the confirmed input, tool calls by `item_started.tool_name`, the turn's status, and its final assistant
  message, the last `item_completed` whose `item_started` kind was `ITEM_KIND_ASSISTANT_TEXT`. It returns the last
  `through_cursor` as an opaque resume token.
- `wait_for_turn(token, wait)`: `after_cursor` from the token, `kinds [TURN_COMPLETED]`, follow, with a gRPC deadline
  just under the tool's wait. It returns the turn that completed, or "still running" with the newest token, so the
  next call continues where this one stopped.

## What moves to it

The orchestration reads, and the Notification Service's outcome scan unless `NOTIFICATION_COMMAND_OUTCOME_DESIGN` removes that read. The app's raw
debug reads stay on `ReadSessionEvents`.

## Questions for review

1. Raw entries with a kind filter for backend readers, with the browser staying on Electric.
2. One call for read and follow (`follow = false` reads to the tail and ends), rather than a page read plus a separate
   follow.
3. Whether `ReadSessionEvents` later becomes `FollowSession` with `follow = false`, once its callers need nothing
   else from it.
