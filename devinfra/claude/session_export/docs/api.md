# claude.ai Claude Code session API

Private and undocumented: not supported by Anthropic, may change without notice. Reverse-engineered
2026-09-29 from a browser HAR of `claude.ai/code` plus live read-only requests. Only `GET` is used.

## Authentication

Two credentials work, against two hosts; routes and response shapes are identical.

### Cookie, against `claude.ai`

Send the account's `sessionKey` cookie and three more headers. Nothing else is required (`cf_clearance`,
`anthropic-beta` and the `anthropic-client-*` headers the web UI sends are not).

```text
cookie: sessionKey=<value>
x-organization-uuid: <value of the lastActiveOrg cookie>
anthropic-version: 2023-06-01
user-agent: <anything except the default python-urllib/httpx one>
```

| Symptom                                     | Cause                                                  |
| ------------------------------------------- | ------------------------------------------------------ |
| 403 HTML `Just a moment...`                 | Default library `user-agent` (Cloudflare challenge)    |
| 401 `authentication_error`                  | `x-organization-uuid` missing, or `sessionKey` expired |
| 400 `anthropic-version: header is required` | Header missing                                         |

### OAuth bearer, against `api.anthropic.com`

```text
authorization: Bearer <access token>
anthropic-version: 2023-06-01
```

Observed with a Claude Code login token (the full scope set): the list route, and `/events` for a newest and an
archived session, answered as on `claude.ai`. On the list route, dropping `anthropic-beta: ccr-byoc-2025-07-29` or
`x-organization-uuid` alone still worked; dropping both is untested, and the events probes sent both. The client
sends both, as Claude Code does. An expired token gives 401 `authentication_error` ("OAuth access token has
expired"). An access token lasts 8 hours.

A grant with only `user:profile user:sessions:claude_code` lists all sessions and reads events (a 1,531-session
listing; a 216,491-event session), and the Messages API refuses it: 403 `permission_error`, "OAuth token does not
meet scope requirement any_of(org:service_key_inference, user:ccr_inference, user:developer, user:inference,
user:voice, workspace:developer, workspace:inference, workspace:messages_create)". Claude Code uses the sessions
scope to create and steer cloud sessions, so it very likely permits writes too; that is untested, so treat such a
token as writable.

### Pairing a dedicated OAuth grant

Authorization-code flow with PKCE, the way Claude Code logs in (constants from CLIProxyAPI's Claude login and
`aiquota`'s refresh; the client id is Claude Code's public one, in `claude_api/oauth_client.py`):

- Authorize: `https://claude.ai/oauth/authorize?code=true&client_id=…&response_type=code&redirect_uri=http://localhost:54545/callback&scope=…&code_challenge=…&code_challenge_method=S256&state=…`.
  The browser is redirected to the loopback `redirect_uri` with `code` and `state`.
- Exchange: `POST https://platform.claude.com/v1/oauth/token`, JSON
  `{grant_type: authorization_code, code, redirect_uri, client_id, code_verifier, state}` →
  `{access_token, refresh_token, expires_in, organization: {uuid}, …}`.
- Refresh: the same URL, JSON `{grant_type: refresh_token, refresh_token, client_id, scope}`.

Observed: the authorize page accepted `user:profile user:sessions:claude_code`, and the resulting grant is enough
for the list and events routes. A refresh returned a new refresh token and kept the scopes, so the refresh token is
single-holder state: the client persists the new credential before using it, and a crash between the response and
the write would lose the grant (re-pair).

Not established: whether the token response reports the granted `scope` (the credential records the requested set
when it does not), whether the old refresh token stays valid after a rotation, and whether `user:profile` is needed.

## List sessions

`GET /v1/code/sessions?limit=100&cursor=<next_cursor>&include_trigger_sessions=true` →
`{data, next_cursor?, resume_token}`. Claude Code Web exposes this opt-in on its generic session-list helper; the
sync passes it to discover routine runs. Its response effect has not been independently probed. List items retain
`trigger_id` and `origin` when present.

- `next_cursor` is absent on the last page. The cursor is opaque (base64).
- Without a `statuses` filter the list includes archived sessions; statuses seen: `active`, `archived`. The web UI
  passes `statuses=active&statuses=paused` for its live view.
- `limit=100` works; larger values are untested.
- The list is ordered by `last_event_at`, newest first (true of all 1,531 sessions of one account), and the cursor
  decodes to `<last_event_at in nanoseconds>|<session uuid>`. An incremental sync can therefore page from the top
  and stop at the first session whose `last_event_at` it already holds.
- A list item carries `id` (`cse_<x>`), `title`, `status`, `status_bucket`, `environment_kind`, `config`
  (model, origin, sources, `outcomes` with the repo and branch), `external_metadata` (branches; usage and cost
  only on some sessions), `created_at`, `updated_at`, `last_event_at`, `user_message_count`, `tags`.

The same session is `session_<x>` in `claude.ai/code/` URLs; both prefixes are accepted in the paths below.

## List events

`GET /v1/code/sessions/<id>/events?limit=500&sort_order=asc&cursor=<sequence_num>` → `{data, next_cursor?, resume_cursor}`.

| Parameter    | Behaviour                                                                                    |
| ------------ | -------------------------------------------------------------------------------------------- |
| `limit`      | 1..500, else 400. Default 50.                                                                |
| `sort_order` | `asc` or `desc`, else 400. Default `desc`.                                                   |
| `cursor`     | A `sequence_num`, exclusive: `asc` returns greater, `desc` returns smaller. Default: an end. |

- `asc` without a cursor starts at `sequence_num` 1; sequence numbers are contiguous. `iter_event_pages` relies on
  it and raises `ValueError` on a page that skips one, before yielding it.
- `next_cursor` is present exactly when more events lie beyond this page in the requested direction, so the last
  page needs no empty follow-up request even when it is exactly full. A cursor past the end returns an empty
  `data`.
- `resume_cursor` is always present; it is the last returned `sequence_num`. Use it to poll a live session.
- A session can have zero events.

Also seen in the web UI, unused here: `POST .../events` (send a user message), `POST .../client/presence`.

## Event stream

Read from the web client's code (capture `2026-09-29-c20643cea8`), and partly observed. The client sends the same
headers as for the routes above, plus `Accept: text/event-stream`.

`GET /v1/code/sessions/<cse_id>/events/stream[?from_sequence_num=<n>]` carries `last-event-id: <n>` as well, both
omitted for a client with no position. Frames, by `event:`:

| Frame                | Data                                                        | The client                                            |
| -------------------- | ----------------------------------------------------------- | ----------------------------------------------------- |
| none                 | as the stream opens, then every 12-15 s (observed)          | nothing but its idle timer: a keepalive               |
| `client_event`       | an event as the events route sends it; `id: <sequence_num>` | stores it; a frame with no data only moves its cursor |
| `catch_up_truncated` | none                                                        | pages for what the stream did not replay              |
| `session_update`     | session metadata                                            | applies it                                            |
| `ephemeral_event`    | a transient event, not persisted                            | shows it                                              |
| `delivery_update`    | delivery status of a sent message                           | applies it                                            |

An open answered 410 means the position is gone: the client restarts from nothing. Other 4xx except 429 are fatal to
it; 429 honors `Retry-After`. It restarts a stream that delivered nothing for 35 s, backs off 1 s doubling to 30 s
with jitter, and after two connections that delivered no frame polls `GET .../events?sort_order=asc&cursor=<n>`.

Observed on 2026-09-30, with the OAuth bearer against `api.anthropic.com`, on a session in use (150 s) and on an
archived one (100 s):

- **Open.** An unnamed frame, then a `session_update` carrying only `connection_status`. The server closed neither
  stream, and an unnamed frame came every 12-15 s, so a quiet stream stays inside the client's 35 s limit.
- **`client_event`.** `id` is the `sequence_num`; the frames were contiguous and none was sent again. A `worker` event
  carries `created_at`, `event_id`, `event_type`, `payload`, `sequence_num` and `source`: no `device_attestation_status`
  and no `sent_by_account_id` (the events route sends both; `Event` defaults them) and no worker stamps. The events
  route sends stamps only on events with `source: client` (a user's message, a queued notification, a control
  response): 6 of a session's newest 500, and none of the 494 `worker` ones.
- **`delivery_update`** carries `event_id`, `status` and `timestamp`. It follows a client-sent event by about 0.1 s
  with `DELIVERY_STATUS_RECEIVED`, and later `DELIVERY_STATUS_PROCESSING`; a third status has not been seen. The
  client event itself is pushed without stamps and is not sent again: this frame is how they change on a live stream.
  Its `timestamp` is 12-19 ms after the `received_at` the events route reports, so it is not the stamp.
- **Catch-up.** A `from_sequence_num` in the past replays the stored events as `client_event` frames carrying the
  stamps the events route reports, only those that are set: 471 in 12 s, with no `catch_up_truncated`.
- **`ephemeral_event`** carries `event_type`, `payload`, `source` and `timestamp`, no id: 44 in 150 s, all `system`
  events of the `worker`.

## Session watch

`GET /v1/code/sessions/watch?exclude_tags=-&resume_token=<token>` streams changes to the account's sessions; the token
is the list route's `resume_token`. Observed 2026-09-30 with the OAuth bearer against `api.anthropic.com`, using
`export_sessions_bin probe`:

- **It needs `anthropic-client-platform: web_claude_ai`.** Without it the answer is `404`, `text/plain`,
  `endpoint not enabled`, whatever else is sent: with or without `anthropic-beta`, with or without a resume token,
  with `anthropic-client-feature: ccr`. With it the answer is 200, and adding `anthropic-client-feature: ccr` changes
  nothing. The web client sends the header on this request. `claude.ai` with the bearer answers the same way.
- **Frames**, `event:` names and JSON data: `added` for each live session on connect, then `sync` (data `{}`, and
  the `id` is the next resume token), then `changed` as sessions change. Only `sync` carries an id, and it recurs every
  100-120 s as a checkpoint; a watch held open for seven minutes kept delivering `changed` throughout. `added` and `changed` carry a session as the
  list route sends it (`id`, `title`, `status`, `created_at`, `updated_at`, `last_event_at`, `config`,
  `worker_status`, …); `removed` carries `{id}` (from the web client's code, not seen). A frame with no `event:` name
  and no data also arrives, seconds after connecting: a keepalive.
- **The token must be fresh.** One up to 225 s old opened a watch that delivered `added`, `sync` and `changed`; one
  of 240 s or more got a 200 and a keepalive, then nothing at all, not even `sync`, and no later change either. The
  answer is not a 410, so nothing in the response says the watch is dead. The token is a nanosecond timestamp,
  base64-encoded. Page size does not matter, and neither does an `Accept: text/event-stream` header.
- 410 means the token expired and 400 that none was sent (both from the web client's code, not seen).

`GET /v1/sessions/watch`, the older route family, needs no such header and streams `session_updated` frames of a
different session shape (`session_status`, no `last_event_at`), 200 of them on connect. The sync does not use it.

The web client itself uses the watch only while the server-side gate `amber_harbor_beacon` is on for the account, and
otherwise polls the list every 30 s, doubling to 10 min while nothing changes.

## Event

```text
event_id, event_type, sequence_num (decimal string), source ("worker" | "client"), created_at, payload,
sent_by_account_id, device_attestation_status; sometimes received_at, processing_at, processed_at
```

`event_type` values seen: `system`, `assistant`, `user`, `tool_progress`, `tool_use_summary`, `control_request`,
`control_response`, `env_manager_log`, `result`, `prompt_suggestion`, `rate_limit_event`, `autocompact_state`,
`active_goal`.

- `assistant` and `user` payloads carry a Messages-API `message` whose content blocks are `text`, `thinking`,
  `tool_use` and `tool_result`; `user` tool results also carry a structured `tool_use_result`.
- `system` payloads discriminate on `subtype`: hooks (`hook_started`, `hook_response`), `init`,
  `thinking_tokens`, `task_*`, `post_turn_summary`, `compact_boundary`.
- Full history survives compaction: events before a `compact_boundary` remain retrievable.
- Subagent traffic is inline, marked by a non-null `payload.parent_tool_use_id`.
- `thinking` blocks are mostly empty (a signature only): 53 of 803 in a five-session sample had text.
- `result` events carry per-turn cost, usage and timing.

## Not established

- Behaviour under sustained HTTP 429; the client backs off exponentially and never hit one.
- Image content in tool results (none in the sampled sessions).
- Whether an archived session's events can still change. Retention: the oldest session of the tested account
  (created 2025-11-14) still returned all its events; any limit beyond that is unknown.
