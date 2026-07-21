# Gmail

Two read paths; prefer the first:

## Primary: haku-console's `gmail` MCP tools

haku-console proxies an in-process `gmail` MCP server (the console's own per-Operator Google
OAuth — refresh token in the console's Postgres, self-refreshed in-process — independent of the
`google-access-token` secret and Airlock entirely, so it survives that outage class). All
read tools are auto-approved for authenticated agents under the reviewed console policy:
`threads_list` (Gmail search-box `q` + `maxResults`/`pageToken` paging), `threads_get`,
`messages_get` (`id` + `format`: `minimal`/`metadata`/`full`/`raw`), `labels_list`,
`labels_get`, `filters_list`, `filters_get`, `drafts_list`, `drafts_get`. These are generated
straight from Google's discovery doc, so params and responses are Gmail's REST API **verbatim** —
native names (`id`, `q`, `maxResults`, `pageToken`, …), not friendly aliases.

Reach it however your runtime wires it: managed sessions expose the tools directly as
in-session MCP tools; otherwise call `https://haku.allegedly.works/mcp` over MCP-HTTP
(<mcp_over_http.md>) with the `haku-console-agent-api` bearer from
`haku-sandbox`. Example:

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-console-agent-api -o jsonpath='{.data.token}' | base64 -d)
fastmcp call https://haku.allegedly.works/mcp gmail__threads_list \
  --input-json '{"q":"after:1784133277","maxResults":100}' \
  --auth "$TOKEN" --transport http
```

Writes (draft CRUD, `threads_modify_labels`, label CRUD, filter create/delete) exist on the same
server but are approval-gated except the reviewed `haku/`-label carve-out — authority and its
bounds live in `../instructions.md` → _Hard rules_, policy in your state's `manage_gmail_labels`
procedure.

## Fallback: REST with the read-only Google token

The `google-access-token` path ([README](README.md)):
`curl -s -H "Authorization: Bearer $TOK" 'https://gmail.googleapis.com/gmail/v1/users/me/messages?q=newer_than:7d'`,
then `.../messages/{id}?format=metadata` (`format=full` only when you must read a body to judge
it). Useful `q=` filters: `is:unread`, `is:important`, `category:primary`.

## Gotchas (verified; apply to both paths — they're Gmail query semantics)

- **Resume precisely with `q=after:<epoch-seconds>`** — `after:YYYY/MM/DD` is only date-granular
  and re-scans or skips part of a day. First run: a window like `newer_than:7d`.
- **Count `messages[]`, not `resultSizeEstimate`.** `resultSizeEstimate` is a rough
  mailbox-wide estimate (it reads ~the same large number for _every_ non-empty query) — it is
  **not** the match count. Use `len(messages)` and page with `nextPageToken`.
- **Never hand-compute the `after:` epoch.** A bookmark accidentally set a few days in the
  **future** makes `after:` return 0 on every run — a silent blind spot, not an empty inbox.
  Derive the bookmark from data (the newest processed message's `internalDate`, which is ms —
  divide by 1000 for `after:` seconds). In haku-state this whole class is tool-enforced:
  read with `haku read --source gmail` (reads the typed ledger in
  `memory/bookmarks.md`'s frontmatter and refuses a future cursor) and advance the ledger
  **only after triaging the results** via `haku advance gmail --to <epoch>` —
  never by hand-editing it.
- A `0`/empty `messages` result is only trustworthy once the bookmark is sane — when in doubt,
  cross-check with a relative window (`newer_than:1d`). `haku read --source gmail` runs that
  cross-check automatically for a >24h-old bookmark and fails loud on the contradiction
  (0 results while `newer_than:1d` is non-empty is logically impossible).
