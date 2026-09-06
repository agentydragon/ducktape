# Wyrm2 central proxy cutover

Cutover checkpoint: 2026-09-06 06:26 UTC; compatibility evidence through 06:52 UTC.
Wyrm2 uses the central proxy, but the migration
is not accepted: Desktop has incomplete conversation history and “Failed to
fetch” errors. A response-buffering defect is identified below; full Desktop
recovery remains unverified. Keep the exact-route mitigation during verification.

- Host opt-in #5692 merged as `8f3c3ff323e59980053b31145d69c1856f29efe6`.
  Its built NixOS generation is
  `/nix/store/769v149fnbzp9gfshyhsl57h9ibchavr-nixos-system-wyrm2-26.05.20260826.062346a`.
  Comparison to the old running generation found no kernel, driver, system
  package or unrelated unit upgrade; the changes are the relay and already-landed
  socket-snapshot instrumentation. The operator switched it; Home Manager
  successfully activated the matching generation at 05:47:32 UTC.
- The installed base proxy unit matched the reviewed Squid unit. Both old
  `80-graphql-5213-append.conf` and `90-cloud-github-block.conf` overrides still
  selected local MITM after the switch. Desktop was gracefully stopped; the old
  proxy was stopped; the verified overrides were moved out of the active unit
  directories before starting the new relay. No bare truncating capture command
  was started.
- At 05:50:09 UTC the single user proxy service started Squid 7.6 on loopback
  8788, with no drop-ins. SOPS credential and rendered runtime configuration are
  owner-only mode 0600; the runtime directory is 0700. Authenticated readiness
  through this installed service returned 200; the empty synthetic batch-route
  request returned 429 with `Retry-After: 3600`.
- The old `claude-desktop-github-proxy` user-profile package and its exact
  immutable launcher shadow were retired. The normal command now resolves through
  `/etc/profiles/per-user/agentydragon` to the centrally proxied wrapper. The
  `claude://` association remains `com.anthropic.Claude.desktop`.
- Normal `gtk-launch` initially did not leave an app running. Directly launching
  that same installed wrapper at 05:53:44 UTC did; the user confirmed the window,
  retained sign-in and working data. No new profile or reauthentication was needed.
  The network subprocess has established sockets to the loopback relay. The
  wrapper process has the pinned central CA and relay environment. App-private
  NSS has the expected central certificate fingerprint; all three global NSS
  database file hashes are unchanged.
- At the checkpoint the relay remained active with no drop-ins or restarts.
  The local raw capture retained inode 1105304 and 1,141,498,955 bytes; incremental
  session metadata retained inode 1107266 and 1,658,973 bytes. Both remain 0600
  and stopped growing after local MITM shutdown. No old capture was deleted.
- Central raw capture grew with real Desktop traffic. A bounded in-pod comparison
  found neither configured proxy password/Basic-auth representation nor proxy-auth
  header/legacy metadata in it. Raw and incremental files remain 0600/UID 1000.
  By 05:59 UTC the retained batch-route counter was 82: two known synthetic
  probes and 80 natural requests. Other Claude requests succeeded. Account quota
  was 4,996 remaining; this is a point-in-time containment observation, not the
  multi-day acceptance result.

## Desktop compatibility evidence

The fixed 21,984,723-byte central capture prefix contained 1,257 terminal flows;
session metadata extended through 06:16:31.781 UTC. Findings are bounded to
that window, not a claim about subsequent traffic:

- One `/v1/code/sessions/:session/events` chain returned three complete HTTP 200
  pages of 500 records at 06:13:48–06:13:50 UTC. Each request cursor matched the
  preceding response; the third response still advertised a next cursor. No
  continuation was saved. These are nested records, not 1,500 rendered messages.
  A completed upstream capture does not prove Desktop received or rendered it;
  a missing terminal flow does not distinguish an unrequested page from one
  still in flight.
- Two earlier session-detail requests had HTTP 200 headers but no complete body
  or response-end timestamp, followed by peer closure. Header status alone is
  not evidence of successful history delivery.
- Seven peer-closed requests at 06:16:18–06:16:21 UTC coincide with Desktop's
  `ERR_NETWORK_CHANGED`. Pod-interface attachment activity coincided too, but
  the operator reports the same routine pod churn with working Desktop before
  the proxy. Network-change errors are correlated symptoms, not an established
  explanation of the new regression; no host-network change is justified yet.
- An earlier, distinct burst contained 23 session/bootstrap requests with
  `Proxy destination resolution failed` starting at 05:56:57–05:57:34 UTC.
  Later same-pod destination/self DNS probes succeeded, and the bounded capture
  had no such failure after 06:15. The generic error hides the failed lookup and
  original exception. Error caching and synchronous capture stalls remain
  hypotheses, not selected repairs or explanations of current partial history.
- Blocked batch requests were same-origin Claude requests; missing CORS headers
  did not explain their behavior. No TLS-verification failure appeared in the
  inspected prefix. Neither observation establishes full proxy compatibility.

GNOME search also retained a stale application index from the removed temporary
Nix user profile. The current installed desktop file is valid and fresh GIO
lookup using Shell's actual search path finds it. As a minimal current-session
remedy, the user applications entry now points to the current declarative
`/etc/profiles/per-user/agentydragon/share/applications/com.anthropic.Claude.desktop`,
not the old temporary package. The user confirmed Claude is back in search.

## Approved history comparison and streaming defect

The operator approved up to 20 read-only history GETs; six were used. The first
pair returned HTTP 400 because the diagnostic omitted required
`anthropic-version`, a replay-construction error. With original API context
preserved, both direct and installed-relay/central paths returned identical
HTTP 200/H2 JSON for the third page and its next-cursor continuation:

- 06:41:08–06:41:09 UTC: 500 records, 687,156 decoded bytes; direct 380 ms,
  proxied 514 ms.
- 06:41:55–06:41:56 UTC: 500 records, 628,507 decoded bytes; direct 293 ms,
  proxied 487 ms.

Both pages remained nonterminal. This excludes deterministic content corruption
for those requests at those times, not client pagination or rendering failures.
No messages, POSTs, redirects, restarts or TLS bypasses were used. Temporary
credentials and response copies were deleted; original captures remain intact.

The operator's subsequent reopen showed stale messages absent compared with the
phone, then loading skeletons after reload. The capture confirmed the same
conversation loaded three complete, nonterminal 500-record pages at
06:42:28–06:42:29 UTC; page three's newest record timestamp was September 3.
The later bounded prefix contained no subsequent saved history page.

That prefix (30,736,457 bytes, 1,742 terminal flows through 06:52:08 UTC) also
contained 14 incomplete SSE responses and one complete response. In particular,
the static `GET /v1/code/sessions/watch` received upstream HTTP 200
`text/event-stream` headers at 06:36:04.934 UTC, but remained unfinished until
client closure at 06:42:28.123 UTC, 383 seconds after request start. Later watch
retries and `/api/organizations/:org/mcp/v2/bootstrap` also had unfinished SSE.
The watch route is shared live-update plumbing, not a per-conversation endpoint.

Mitmproxy 12.2.3 defaults to buffering response headers and bodies until EOF;
the deployed runtime does not enable event streaming. A gate-based regression
reproduced this: the origin sends an event but waits for client receipt before
closing, while the unchanged proxy withholds even the headers
([failing invocation](https://app.buildbuddy.io/invocation/0212ec6e-80f3-4424-b141-f2e567d6768d)).
The candidate enables streaming specifically for `text/event-stream` and retains
streamed bodies for terminal capture. Both before-EOF cases, all 32 runtime
cases and five capture cases pass; changed libraries also passed their aspects
([passing invocation](https://app.buildbuddy.io/invocation/41a7b5ba-1938-48ff-98bf-01b0dfcc0a71)).
This proves the buffering defect and its synthetic correction, not Desktop
recovery: the candidate is not yet deployed or verified against the reported UI.

## Acceptance

Verify full history and ordinary Desktop use before accepting the cutover or
retiring unused old signing keys and four owned GC roots. Rugged has not been
live-verified or activated. Quiet quota while Desktop is broken is not
representative acceptance. The operator also archived many old conversations
within the preceding couple of hours; reduced polling of archived conversations
could contribute to the recent lower burn. The archive timing relative to each
measurement is unknown, so do not attribute recent quiet solely to the block.
The controlled block/reversal remains separate route-attribution evidence.
The requested multi-day exhaustion-free window remains required. The immediate
deliverable is working Desktop through the proxy stack with capture intact, not
additional monitoring or robustness expansion.
