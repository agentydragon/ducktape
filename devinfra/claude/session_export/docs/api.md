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
expired"). An access token lasts about 8 hours.

### Pairing a dedicated OAuth grant

Authorization-code flow with PKCE, the way Claude Code logs in (constants from CLIProxyAPI's Claude login and
`aiquota`'s refresh; the client id is Claude Code's public one, in `claude_api/oauth_client.py`):

- Authorize: `https://claude.ai/oauth/authorize?code=true&client_id=…&response_type=code&redirect_uri=http://localhost:54545/callback&scope=…&code_challenge=…&code_challenge_method=S256&state=…`.
  The browser is redirected to the loopback `redirect_uri` with `code` and `state`.
- Exchange: `POST https://platform.claude.com/v1/oauth/token`, JSON
  `{grant_type: authorization_code, code, redirect_uri, client_id, code_verifier, state}` →
  `{access_token, refresh_token, expires_in, organization: {uuid}, …}`.
- Refresh: the same URL, JSON `{grant_type: refresh_token, refresh_token, client_id, scope}`.

Not established: whether a scope set narrower than Claude Code's full one is accepted by the authorize page or
enough for the sessions routes; whether the token response reports the granted `scope`; whether a refresh rotates
the refresh token (the client keeps the old one when none comes back).

## List sessions

`GET /v1/code/sessions?limit=100&cursor=<next_cursor>` → `{data, next_cursor?, resume_token}`.

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

- `asc` without a cursor starts at `sequence_num` 1; sequence numbers are contiguous.
- `next_cursor` is present exactly when more events lie beyond this page in the requested direction, so the last
  page needs no empty follow-up request even when it is exactly full. A cursor past the end returns an empty
  `data`.
- `resume_cursor` is always present; it is the last returned `sequence_num`. Use it to poll a live session.
- A session can have zero events.

Also seen in the web UI, unused here: `GET .../events/stream?from_sequence_num=<n>` (server-sent events for the
live tail), `POST .../events` (send a user message), `POST .../client/presence`.

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
