# haku/console/notifications — pushes, console events, and the wake wires

Two halves. Web Push for pending approvals (`push.py`, `push_routes.py`, `console_events.py`,
`connection_metrics.py`) is specified in <../README.md> § Notifications. The wake wires carry
"look now" and never content:

| Module                  | Role                                                                                                                                                                                                                                                                             |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pg_wake.py`            | The layer-neutral `LISTEN`/`NOTIFY` transport: `notify_raw`/`libpq_dsn` and the `WakeListener` connection, reconnect loop, parse and reconnect-gap dispatch, instantiated once per layer. The console's other LISTEN consumer is <console_events.py>.                            |
| `session_wakes.py`      | The session layer's wake channel (`session_events`) and its surface: `SessionWakes` (`wait`/`watch_session`/`watch`) over its own `WakeListener`; consumed by `SessionService` and the allocator, and nothing conversation-shaped reaches it.                                    |
| `conversation_wakes.py` | The conversation layer's wake channel (`conversation_wakes`) and its surface: `ConversationWakes.watch` over its own `WakeListener`, plus the cross-layer `notify_update`; a channel imports this and so names a conversation surface, never a session type (roll gotcha below). |

## Renaming a wake channel or payload

The Deployment rolls with `maxUnavailable: 0`, so old and new replicas run together for the
length of a roll. A renamed channel means the new replica notifies where the old one is not
listening, and the wakes are lost for that window — the same expand/contract discipline a
destructive migration needs. Notify on both names for one release, then drop the old, and gate
that second release on the roll having **converged** (every pod on an image at or after the
first) rather than on a release having elapsed, since `maxUnavailable: 0` means a bad image
stalls the roll with the old replica still serving. An explicit operator cutover under the
standing conversation-disruption allowance (<../AGENTS.md>) may skip the overlap, costing wake
latency for one roll and no data.

The trap in the overlap phase: while both names are being notified, every wake is delivered
twice, so a woken waiter proves nothing about which name woke it. Tests and production alike
will look healthy with the new path entirely broken, right up until the old one is deleted.
Cover the new path end to end on its own before contracting — a test driving `pg_notify` on
exactly one channel.
