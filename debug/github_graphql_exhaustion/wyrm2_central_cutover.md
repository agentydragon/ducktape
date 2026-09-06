# Wyrm2 central proxy cutover

Cutover checkpoint: 2026-09-06 06:26 UTC; operator acceptance checkpoint 08:43 UTC.
Wyrm2 uses the central proxy. The response-buffering and connection-reuse defects
described below are repaired in the deployed image, with passing live transport
checks. The operator test-drove Desktop, confirmed it works, and requested a
wind-down with another 1–2 weeks of observation. Functionality is accepted;
quota reliability is not yet established. Keep the exact-route mitigation.

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
the pre-#5699 runtime did not enable event streaming. A gate-based regression
reproduced this: the origin sends an event but waits for client receipt before
closing, while the unchanged proxy withholds even the headers
([failing invocation](https://app.buildbuddy.io/invocation/0212ec6e-80f3-4424-b141-f2e567d6768d)).
The candidate enables streaming specifically for `text/event-stream` and retains
streamed bodies for terminal capture. Both before-EOF cases, all 32 runtime
cases and five capture cases pass; changed libraries also passed their aspects
([passing invocation](https://app.buildbuddy.io/invocation/41a7b5ba-1938-48ff-98bf-01b0dfcc0a71)).
This proves the buffering defect and its synthetic correction, not Desktop
recovery. The deployed candidate and remaining symptoms are described below.

## Streaming deployment and remaining failure

[#5699](https://github.com/agentydragon/ducktape/pull/5699) merged as
`50b8f3111384b973997e7aa75cfdb75b44135a8b`. Its devel CI was superseded, not
failed; successor `956c496caf04f23f234749b8d53424b5893ab106` retained the tested
proxy source and [published its image](https://github.com/agentydragon/ducktape/actions/runs/34018009405/job/101445754412).
Flux applied image-update commit `540cd90e9c9f9c44e9a94f779c907ab42ca9f8ea`;
the application Kustomization was Ready and Healthy at 07:14:18 UTC.

- Running image: `devel-20260906070647-956c496`, digest
  `sha256:b7b4a5103a00320d458e41c66a8f3a039b237354ee82fedc84763594a48739de`.
  Installed runfiles contain the SSE header hook and `store_streamed_bodies=True`.
  The new pod had no restarts; the existing local Squid also had no restarts.
- Central capture retained pre-deployment records and remained append-only,
  owner-only. Natural batch-route requests still returned 429. Open or aborted
  SSE bodies are not an incremental capture; terminal raw records alone cannot
  demonstrate whether an in-flight request exists or a client received data.
- An unauthenticated seven-second GET of the public
  [Wikimedia event stream](https://wikitech.wikimedia.org/wiki/Event_Platform/EventStreams_HTTP_Service)
  through the installed Squid and central proxy returned HTTP 200/H2 with normal
  certificate verification, `text/event-stream`, and 365,955 bytes. First byte
  arrived in 0.655 seconds, before the deliberate time limit closed the stream.
  Its body was discarded. This verifies streaming through the deployed stack,
  not Claude rendering or authenticated history.
- At 07:14:33 UTC the previously affected conversation returned recent event
  records through 07:14:32.950 UTC. The operator later reported it briefly
  matched the phone, but a 07:24 reload again looked stale, followed by some
  arriving deltas. Another old conversation's send remained pending for at
  least 1 minute 47 seconds. Desktop compatibility is therefore still failing.
- Local logs show session-loading timeouts at 07:18–07:19 UTC, 45
  `Failed to fetch` entries at 07:23:21, and another burst at 07:24:12–07:24:15.
  `ERR_NETWORK_CHANGED` coincides with the bursts; this remains insufficient
  to assign the new proxy regression to routine host pod-interface churn.

The approved authenticated history replay count remains six, not twenty;
no additional Claude replay or message send was made for this checkpoint.

## Connection-pool starvation

The operator's built-in 30-second Desktop Net Log at 07:34:11–07:34:41 UTC
exposes a different failure from the earlier network-change bursts. History GETs
and event POSTs send HTTP/2 headers immediately, then receive no response headers
within the recording. Synthetic batch-route responses still arrive, and separate
WebSockets upgrade and exchange frames. The recording has no
`ERR_NETWORK_CHANGED`. Two session-detail requests abort after fifteen seconds;
their duplicate cache waiters report `ERR_CACHE_RACE`, then issue requests that
also wait for headers. The cache race is not established as the initiating fault.

The proxy source supplies a concrete mechanism:

1. `PublicOrigins.server_connect` replaces the logical hostname in
   `Server.address` with its validated IP.
2. Mitmproxy 12.2.3 `GetHttpConnection.connection_spec_matches` compares the
   request's hostname against `Server.address`. The existing sockets no longer
   match, so later requests open new ones instead of reusing keep-alive sockets.
3. `ConnectionHandler.open_connection` holds a per-address semaphore of five
   throughout each socket's lifetime, not just its handshake. Five idle sockets
   therefore occupy every slot and subsequent requests wait for a socket to close.

An eight-request nested-TLS regression on the unchanged implementation times out
waiting for headers ([invocation](https://app.buildbuddy.io/invocation/48397ad5-c988-4aa6-abd0-6ec568719ed3)).
The deployed route independently reproduces the predicted boundary: eight
unauthenticated `GET https://claude.ai/robots.txt` transfers using one curl
connection return five HTTP 200s (167, 65, 54, 62, 92 ms), then three transfers
with no headers or body before their three-second limits. Curl reports no new
client connections after the first request. Bodies were discarded; no session
credential, user message, or GitHub request was involved.

The candidate preserves `Server.address` and moves numeric dial enforcement to
the application's public `SelectorEventLoop.sock_connect` hook. Each mitmproxy
connection task carries its validated DNS answer set; the actual numeric socket
destination must belong to it. This retains hostname-based reuse and rejects DNS
changes before the socket connects. Runtime startup requires the guarded loop.
Tests cover repeated requests beyond the five-connection limit and DNS changes to
both private and different public addresses. All eight sequential requests now
complete with one upstream dial, and both rebinding cases fail before any origin
dial. The runtime, destination, and capture tests plus changed-library type/lint
aspects pass ([invocation](https://app.buildbuddy.io/invocation/6a3cde5a-3603-485c-b836-a341c59f9218)).
The operator permits a larger pool if needed; no increase is currently included,
because it would defer the same starvation without repairing connection reuse.
The deployed repair and remaining acceptance boundary are described below.

## Connection-reuse deployment verification

[#5703](https://github.com/agentydragon/ducktape/pull/5703) merged at 08:07:39 UTC
as `eb58b4860859cbcea0dbbb0e975e1e6bff0874dd`. Its PR Bazel CI passed all six
proxy test targets, including unprivileged image startup and capture persistence
([invocation](https://app.buildbuddy.io/invocation/1fcfd631-c446-5217-a7a1-c18fed08d2e7)).
Pre-commit, Gazelle, and CodeQL passed; the separate Nix wheel check was still
running at this checkpoint, not proven successful.

Two superseded devel runs were cancelled, not failed. Successor
`589e2c75224ab98f78d94e12f8d3bb9b01aad641` retained the proxy source, passed
the devel test/build job, and [published the tested proxy image](https://github.com/agentydragon/ducktape/actions/runs/34021161691/job/101454461110).

- Image: `devel-20260906081725-589e2c7`, digest
  `sha256:7a8c000c74eca271e4d2e0f8253e6ef2d99efe944214afc34306c4a712857544`.
- Flux image-update commit: `b4408f9174d7a190a48052116b35ae1d745690d7`.
  The application Kustomization became Ready and Healthy at 08:30:50 UTC.
  The replacement pod reports the expected digest and zero restarts.
- The same eight unauthenticated `claude.ai/robots.txt` GETs now all return
  HTTP/2 200 with certificate verification enabled, over one curl client
  connection. Durations are 179, 45, 47, 46, 46, 40, 41, and 41 ms; the last
  three no longer stall. Bodies were discarded. The synthetic regression,
  separately, verifies one upstream dial for all eight requests.
- A seven-second public Wikimedia SSE probe returns HTTP 200/H2, first byte
  at 0.679 seconds, and 419,150 bytes before the deliberate time limit.
  Streaming still works through the installed relay and repaired central proxy.
- One empty synthetic batch-route POST returns HTTP/2 429 with
  `Retry-After: 3600`. No Claude credential or user message was replayed.
  The authenticated history-replay count remains six.
- The first 51,750,438 raw-capture bytes have the same checksum before and
  after rollout. Raw and session metadata retain their inodes, mode 0600,
  and UID/GID 1000; both files grew. Existing metrics are receiving samples
  from the new pod, including `up=1` and successful requests.

Rollout was briefly blocked by unavailable trust-manager and CNPG admission
webhooks in the dependency chain. Trust-manager exited after losing leader
election; both controllers recovered through their normal restarts around
08:28 UTC. The old proxy also had readiness timeouts during this interval.
These are separate deployment observations, not an attribution of the original
Desktop stalls to host interface churn. No unrelated service or host network
was changed to clear the blocker.

## Operator acceptance and wind-down

At the 08:43 UTC checkpoint the operator said they were satisfied that Desktop
works, after test-driving a new conversation, and requested winding down active
debugging while retaining another 1–2 weeks of observation. This is user-visible
acceptance, not a claim that every old conversation was exhaustively checked.

A body-free scan of a fixed 95,959,269-byte capture prefix, covering requests
started after the replacement process at 08:30:33 through 08:42:34 UTC, found
178 successful history-event GETs and 18 successful event POSTs. The latest
POSTs completed in 0.16–0.90 seconds. All eight public canary GETs share one
recorded client connection and one recorded upstream connection. There were
93 blocked batch attempts, including one known synthetic probe. No authenticated
history replay was added; its count remains six.

The capture is not error-free: twelve watch streams ended with `peer closed`
after receiving HTTP 200, and four finite requests closed before headers around
08:37:51–08:38:08 UTC. These finite closures occurred within 0.09–0.35 seconds,
not the previous pool stall. Their initiator was not established; do not label
them harmless reloads without further evidence. Retain this as a follow-up lead
if user-visible failures recur, not grounds for more intervention after the
operator accepted the application. Open flows and rendering remain outside
terminal-capture coverage.

The latest retained quota sample checked during the test drive was
08:40:58.191 UTC: 51 used, 4,949 remaining. The new pod was Ready with zero
restarts. [Acceptance monitoring](acceptance_monitoring.md) defines the longer
confirmation window; #5213 remains open. No more active experiments or rollout
changes are planned during this wind-down.

## Acceptance

The operator accepted Desktop functionality; retain the working setup during
observation. Unused old signing keys and four owned GC roots remain deferred
cleanup, with ownership checks required before removal. Old local interception
is no longer active. Rugged has not been live-verified or activated.
Quiet quota while Desktop was broken is not
representative acceptance. The operator also archived many old conversations
within the preceding couple of hours; reduced polling of archived conversations
could contribute to the recent lower burn. The archive timing relative to each
measurement is unknown, so do not attribute recent quiet solely to the block.
The controlled block/reversal remains separate route-attribution evidence.
The requested multi-day exhaustion-free window remains required. The immediate
deliverable is working Desktop through the proxy stack with capture intact, not
additional monitoring or robustness expansion.
