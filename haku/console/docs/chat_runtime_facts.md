# Facts the chat runtime relies on, and where they were checked

The chat runtime depends on a handful of behaviours that are not in any spec we control, are not
visible from the code that relies on them, and are expensive to re-derive. The code keeps the
invariant; this keeps the evidence and where it was checked.

Nothing here is a plan — design work lives in <../../plans/chat_runtime_cleanup.md> and
<../../plans/chat_runtime_projection.md> — and nothing here is an incident note; those stay dated
in `debug/`.

## Synapse deduplicates a transaction per device, for 30–60 minutes

`EventTag.transaction_id` derives a reply's `txn_id` from its transcript row, so a replacement
replica re-sending the same answer is refused by the homeserver rather than posting twice. That
works because of two properties of Synapse's `synapse/rest/client/transactions.py`, checked against
`master` on 2026-08-15:

- `_get_transaction_key` returns `(path, "user", user_id, device_id)` for a regular user — keyed on
  the **device**, not the access token. `MatrixClient` pins `device_id` at construction and reuses
  it across logins, so a replacement replica is the same device. Had it keyed on the token, the
  derivation would buy nothing.
- `CLEANUP_PERIOD` is 30 minutes, and the comment there notes entries live "at _LEAST_ 30 mins and
  at _MOST_ 60". A console roll takes seconds, so the window is not the binding constraint.

Outside that window the derivation degrades to what a random `txn_id` did, which is why it is a
second line of defence and not the first. The first is `frame_uid`: a replayed `assistant` frame is
dropped before any send happens.

Pinned by `//haku/console/x:test_matrix_homeserver_e2e`, which sends one transaction twice against a
real Synapse and requires a single event back. It asks for the behaviour rather than the source, so
a Synapse that rekeys its cache fails the test instead of quietly invalidating this note.

## A `/sync` watermark is a valid `/messages` pagination token, at both ends

`MatrixClient._backfill` closes a truncated timeline by paginating from the sync response's
`prev_batch` back to the stored watermark, and `recent_messages` reads history backwards from that
same watermark — both passing an `s…` sync token where the client-server API talks about pagination
tokens. Synapse accepts it, and the backfilled span meets the truncated timeline exactly: nothing is
delivered twice and nothing falls in the join. Checked against Synapse v1.158.0 on 2026-08-15 by
`//haku/console/x:test_matrix_homeserver_e2e`, which fills a room past `TIMELINE_LIMIT` with the
loop stopped — the only way to reach the case at all (R1.7).

## nio's 429 retry is unlimited by default, not off

`AsyncClient._send` loops on `M_LIMIT_EXCEEDED`, sleeping the server's `retry_after_ms` or five
seconds, and `max_limit_exceeded=None` means forever. A rate-limited send therefore never returned
an error — it stopped returning, with the caller blocked inside `room_send` and nothing in any log.
`matrix_client.MAX_RATE_LIMIT_RETRIES` bounds it so a 429 can reach `matrix_pacer`, which is the
only measurement of the room's real budget the console ever receives.

## A websocket closed before `accept()` is an HTTP 403, not a close code

Every admission refusal in `handle_runner` closes before accepting, and uvicorn answers the
handshake with **403** rather than the close code passed to it (`websockets_impl.py`,
`websockets_sansio_impl.py`, `wsproto_impl.py`, all checked against the pinned version). The client
then raises `InvalidStatus` — a subclass of `InvalidHandshake`, **not** of `OSError`. The runner's
redial predicate keys on the handshake status for that reason: a 4xx is a refusal worth giving up
on, a 5xx or an `OSError` is transient.

## The CLI reports a prompt's lifecycle only when the prompt carries a `uuid`

`ClaudeCli.query` stamps one. Without it the CLI emits no `command_lifecycle` for that prompt at
all, which is the only confirmation of a mid-turn fold rather than an inference from behaviour, and
what makes the prompt reachable by `interrupt`'s `cancel_queued`. Measured in
<../../cli_protocol/probes/steering.py>.

## The runner replays everything except deltas

`runner.DELTA_TYPE` is excluded from the replay window because a delta is the one frame class that
cannot survive being sent twice. Everything a resumed turn needs that predates its adoption
therefore has to come from the console's own record, not from the replay.

## Investigations, which are not this

Findings from a specific incident stay in `debug/`, dated, and are not maintained:

- Sessions that recorded a container boot and a death:
  <../debug/2026_08_13_sessions_boot_and_die.md>. Its three production checks are **still unrun**,
  and every stage since has been built on top of them.
- CI-wide Docker test timeouts: <../../../debug/2026_08_14_docker_test_timeouts.md>.
