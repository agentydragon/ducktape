# GraphQL attribution: September 5 evening control

Parent: [#5213](https://github.com/agentydragon/ducktape/issues/5213).
Times below are September 5, 2026, Pacific daylight time unless marked UTC.
The corresponding UTC date after 17:00 is September 6.

## Current conclusion

**The Desktop cloud batch-status route is causally implicated in the observed
large burn.** Blocking only its POST endpoint for two minutes intercepted 76
natural requests while the personal counter rose by just one point. After
automatic pass-through restoration, the counter rose by 651 points within
thirteen seconds and exhausted again forty seconds after restoration.

The route is `claude.ai/v1/code/github/batch-branch-status`, not a direct local
request to `api.github.com`. GitHub-only interception/reporting therefore
missed it. Source analysis and captured repeated terminal-PR replies identify
avoidable polling as a candidate mechanism; they do not establish every
upstream GraphQL operation or the exact renderer refresh trigger.

This is not proof of the sole consumer or every historical exhaustion. Earlier
claims that Desktop, CLI, or the cluster were conclusively cleared exceed
their controls. The operator approved a temporary exact-route mitigation,
including reboot persistence until explicitly disabled, on wyrm2 and rugged.
**Do not call this solved until at least several days, preferably one week,
of covered post-mitigation quota windows without exhaustion.**

This investigation read issue #5213 and all 21 comments available at session
start, related #5596 and its comments, and both the tracked historical note and
the operator's untracked hyphenated note. The untracked file was not modified.

## Counter evidence and controls

The existing GraphQL-header exporter remains the independent account-wide
measurement. REST `/rate_limit.resources.graphql` is not a substitute. In this
session, three consecutive direct `query { rateLimit { cost } }` probes returned
unchanged `x-ratelimit-used: 565`; their body reported `cost: 1`. Thus these
probes had no observable debit, but saying their returned cost was zero would
be incorrect.

Minute-resolution Mimir samples, with finer direct probes at the final burst:

| Pacific time | Personal `used` | Observation                     |
| ------------ | --------------: | ------------------------------- |
| 17:13–17:14  |             572 | Before the new large bursts     |
| 17:15        |            1706 | +1134                           |
| 17:16        |            2257 | +551                            |
| 17:17        |            2826 | +569                            |
| 17:18        |            2835 | +9                              |
| 17:19        |            2850 | +15                             |
| 17:20        |            3408 | +558                            |
| 17:21        |            3969 | +561                            |
| 17:22:32     |            4520 | 480 remaining                   |
| 17:22:37     |            4520 | No debit in this interval       |
| 17:22:42     |            5086 | Exhausted; +566 in five seconds |
| 17:25:31     |            5106 | Still exhausted                 |
| 17:30:41     |            5143 | Still exhausted                 |

These are account-wide deltas, not measured query costs. They suggest a
roughly minute-scale burst regime in this window, not the older note's
universal 35–40-second/150-point signature. Request batching and timeout-related
quota charges remain distinguishable hypotheses.

Controls reported by the operator:

- No configuration changes since 13:00; Claude Code Web sessions and two Codex
  instances were running. No unbound GitHub sessions.
- Regular Claude Desktop was running on wyrm2. Its main PID `3258052` and
  network child `3258101` started at **16:44:13**, independently confirmed by
  `ps`. Its presence is not proof it caused the later bursts.
- The Pixel6, outside the cluster and workstation configuration, has the GitHub
  app. The operator **force-stopped it at 17:24**, after this exhaustion. The
  next reset is **17:34:13** (`1788654853`). Flat usage before reset cannot
  clear the phone; observing another large burst after reset would show that
  the phone app need not be actively running for that recurrence. It would not
  exclude server-side activity triggered previously.
- The unproxied Desktop remained running during the separate-profile test.
  This is not a Desktop-off experiment.

Five-second header recording began at 17:32:30 and host socket snapshots at
approximately 17:33, covering the reset without stopping the operator's apps.
Local raw files are `/tmp/github-quota-phone-control-20260906T0033Z.log` and
`/tmp/github-socket-phone-control-20260906T0033Z.log`. The filename is a session
label; the record timestamps are authoritative. Socket snapshots do not
enumerate separate pod network namespaces.

Post-reset result through **17:38:39**: `used` remained zero until 17:35:40,
then rose to 3 at 17:35:45 and 26 by 17:38:23. No large burst recurred in
these first four minutes. The ordinary Desktop PIDs remained running. This
is a useful phone-off observation, not a causal verdict: the earlier burst
regime was intermittent, and no phone-on rechallenge has been performed.

The socket log still observed the original Desktop network process at
17:39:19, and a host-namespace `ps` check confirmed both original PIDs alive
at approximately 17:43:50. A sandboxed `ps` had hidden those processes; it was
not evidence of their exit. System observations must use the host namespace.
At 17:41:59 the account had used 56 points, and at 17:43:13 it had used 63.

At **17:49:40**, the original Desktop was deliberately replaced with the
always-proxied normal-profile app (new main PID `3398172`). This ends the
unchanged-original-Desktop part of the phone-off control. Before the restart,
usage was 100 points at 17:49:35: no recurrence of the earlier large bursts
in approximately fifteen minutes after reset. No phone-on rechallenge has
been performed, so causation remains open.

## Proven coverage defects

### Host recorder

The installed eBPF recorder emits TCP connection events on `TCP_SYN_SENT`, not
each request. An owned loopback experiment sent 1024 then 8192 more bytes over
one socket: the old log had one connection event, while `ss` retained the same
PID/inode/socket cookie and showed `bytes_sent:1024` then `9216`.

Its UDP probe observed connected IPv4 only in a four-case live test. Unconnected
IPv4 and connected/unconnected IPv6 were absent. The UDP deduplication map is
never cleared; neither deduplication nor a live service establishes complete
traffic coverage. These are recorder gaps, not evidence of GitHub using QUIC.

[PR #5665](https://github.com/agentydragon/ducktape/pull/5665) adds retained
established-socket snapshots under the existing host opt-ins. It does not
provide HTTP costs, fix UDP capture, or observe pod namespaces. See its
`debug/github_graphql_exhaustion/recorder_coverage_2026_09_06.md` for measured scope and validation.

### Hubble

Live Cilium was 1.19.6, with medium monitor aggregation and a five-second
aggregation interval. Its flow observations are neither HTTP requests nor
simply TCP connection-open events: active established traffic can generate
periodic trace observations. The historical claim that HTTP/2 necessarily
appears as zero additional Hubble flows is incorrect.

Loss occurs before metrics accounting. A 24-hour query found approximately
216,880 observer-queue lost events on ovh-ns102453 and 35,734 on ovh-ns103711.
Both also reported perf-buffer loss notifications. The latter counter counts
notifications, not the number of missing events. At approximately 17:22, the
proposed ten-minute loss alert already matched live counters.

[PR #5664](https://github.com/agentydragon/ducktape/pull/5664) alerts on these
pre-metric loss sources. It excludes history-ring overruns, which occur after
metric processing, and does not duplicate the existing target-down alert.
Detection is not remediation of the loss.

Further read-only diagnosis found two raw-counter bursts on ovh-ns102453:
11,308 observer rejects and 9 perf-loss notifications during 00:26–00:28 UTC,
then 9,199 rejects and 8 notifications during 00:39–00:41. Cilium's own
cgroup I/O-full PSI reached 32.5% and 43.3% over two-minute windows; CPU wait
was approximately 1–2%. Its CPU quota was unlimited and `nr_throttled` zero.
Two Haku CI pods accounted for 99.3% of observed container writes to the busy
system disk during the first window. That is evidence for an I/O-contention
experiment, not proof those writers are the sole cause of every loss.

A retained Cilium goroutine dump captured the sole Hubble observer blocked
resolving an endpoint identity on `RWMutex.RLock`. The matching endpoint's
writer was stalled allocating a label map; the perf-reader goroutine was
also stalled allocating an event. The dump identifies real observer and
perf-reader blockages, but not the underlying runtime/kernel reason for the
allocation stalls. Increasing the history ring cannot repair either loss
source. Moving/reducing CI system-disk writes and comparing I/O pressure,
observer/perf loss, and probe failures is the next causal test; no such
placement or workload change was made during this investigation.

Six nodes were Ready. Stale Cilium pods on NotReady nodes and missing/down
scrape targets must not be counted as current fleet coverage. Host-namespace
egress remains a separate coverage boundary.

An unresolved high-volume source in the metrics was **not proven host traffic**:
raw events identified the naked `flux-system/infra-drift-tf-runner` pod on
wyrm2, IP `10.244.5.120`. Workload-name labels lost its identity because it had
no workload owner. Its connection to a GitHub-range address does not establish
GraphQL use. This root plans metal infrastructure rather than the GitHub
provider roots. GitHub `/meta` address ranges also include overlapping web/git
destinations; range membership is not API classification.

### Desktop and request capture

The installed Desktop 1.40609.1 uses Electron `net.fetch` for `GhRestClient`,
not a Node HTTP client guaranteed to honor the wrapper's proxy environment.
Its startup guard **accepts `--proxy-server`**. A bounded test opened a window
with that flag; the old note's claim that it was forbidden was false.

Chromium rejected the interception certificate (`ERR_CERT_AUTHORITY_INVALID`).
The successful test used a separate Desktop user-data directory and a private
NSS trust database mounted only into that process tree. It did not disable
certificate verification or modify the ordinary browser's trust database.
The trusted profile launched at **17:23:49**, after the observed exhaustion.

Its initial login callback was misrouted: the desktop's existing `claude://`
handler still launched ordinary Claude. Opening a window is not successful
authenticated request capture; callback routing needs to target the same
isolated profile. The permanent wrapper fix is being validated separately.

The operator subsequently requested **always proxying normal Claude Desktop
on wyrm2**, rather than maintaining competing normal/diagnostic launchers.
That supersedes the temporary URI-handler experiment as the implementation
direction: preserve the normal app profile, use app-private certificate trust,
and make the command, desktop actions, and OAuth handler resolve to the same
proxied package. Do not change the other hosts' defaults.

There is an additional mechanism behind the failed temporary handler: Desktop
registers itself as the default protocol client at startup, using the normal
`com.anthropic.Claude.desktop` identity. That overwrites a competing temporary
handler. Wrapping the normal package entry addresses that mechanism directly.

The generated normal package was installed at 17:49:40 through wyrm2's
previously empty user Nix profile, plus its generated normal desktop entry.
The command and all three desktop actions resolve to the wrapped package;
the normal URI handler remains `com.anthropic.Claude.desktop`. This is a
scoped runtime bridge to the declarative host change, not a full NixOS
activation. Other hosts and the dirty primary checkout were not changed.
Desktop booted with the original profile, no fresh TLS errors, and verified
loopback proxy sockets. Desktop and URI launches reused the same main PID.
The operator subsequently opened a GitHub-backed view and confirmed GitHub
data in the app. The captured requests below supply authenticated traffic
proof beyond successful startup.

The raw capture is now owner-only (directory `0700`, file `0600`).
[PR #5670](https://github.com/agentydragon/ducktape/pull/5670) makes those
permissions the service default. Always-on proxying also makes raw-capture
retention a standing operational concern: the existing flow file includes
unrelated application traffic and has no configured rotation. No existing
capture was deleted or published.

A sanitized read of the existing flow file found 37 GraphQL requests from
`claude-code/2.1.245`, spanning September 4 12:41:35–20:23:23 UTC. All 37 lacked
explicit `rateLimit.cost`; the capture contained no Desktop GraphQL requests.
Missing cost is **unknown**, not zero or automatically one. An account-wide
header increase on one response cannot be assigned to that request.

## Live cloud-route evidence

After the operator activated GitHub data in the normal proxied Desktop, large
debits recurred even though the phone app was still reported force-stopped.
The app's main process was `3398172`, network child `3398260`; local sockets
confirmed the latter talking to the loopback proxy. A report limited to direct
`api.github.com` traffic would have missed the principal new candidate.

All-host metadata from the private flow capture showed **289 POST requests**
to `https://claude.ai/v1/code/github/batch-branch-status` from
00:51:27.584–00:55:14.090 UTC. Of these, 223 completed with HTTP 200 and 66
were incomplete/aborted in the capture. There were also seven cloud
`/v1/code/github/compare-refs` requests and two installation-status requests.
These are cloud-mediated GitHub operations, not direct local GraphQL requests.

| Cloud batch request group (UTC) | Request count | Nearby observed personal debit |
| ------------------------------- | ------------: | ------------------------------ |
| 00:52:30                        |            56 | +644 by 00:52:35               |
| 00:52:50                        |            35 | +470 by 00:52:50               |
| 00:53:13–00:53:16               |            58 | +575 by 00:53:17               |
| 00:53:32                        |            56 | +356 at 00:53:32               |

Payload **shape**, without publishing its private values: 267 requests
contained one repository branch and one session; 19 contained no branches
and one session; just three contained 25 branches and 25 sessions. Identical
bodies repeated three to eight times. Flags include PR discovery, CI status,
and review-decision inclusion. This demonstrates per-row fan-out and repeated
cloud work, but does not measure each backend operation's GraphQL charge.

In contrast, the same window contained four direct Desktop GraphQL requests
(user agent `Claude-Desktop/1.40609.1`), starting at 00:52:41.706,
00:52:43.575, 00:52:44.305, and 00:52:51.265 UTC. All completed successfully
in 0.31–0.40 seconds, with no GraphQL errors and no explicit cost field.
They were simple issue/PR link-preview metadata queries, with no connection
pagination. One bounded replay of that query shape against public PR #5669,
adding `rateLimit`, reported **cost 1**, no errors, and `used:2274`.
The response reporting `used:1263` must not be assigned the intervening large
account delta as its own cost. The first +644 burst preceded all four calls.

The five-second sampler encountered a TLS handshake timeout after 00:53:49
and stopped; this is a real observation gap, not zero usage. A bounded probe
at 00:55:58 read `used:2220`. The resumed sampler uses a per-probe timeout
and explicit failure markers. Usage was 2277 at 01:04:15 UTC.

### Completed two-minute intervention and reversal

All times in this subsection are September 6 UTC. Desktop stayed open with
the same profile and main PID. A one-time proxy restart at 01:10:02 loaded an
initially pass-through addon and opened a separate private experimental flow
stream, preserving the original capture. A pass-through baseline showed
`used:3004` at 01:11:32, 3654 at 01:12:19, and 3667 by 01:13:30.

The exact match was POST, host `claude.ai`, and pathname
`/v1/code/github/batch-branch-status`, **excluding the query string**. Matching
the entire path including `?caller=...` would miss most observed traffic.
Thirty-six scope/expiry test cases passed before the live test.

| Phase (UTC)               | Endpoint observation                               | Independent account counter  |
| ------------------------- | -------------------------------------------------- | ---------------------------- |
| 01:14:32.476–01:16:32.476 | 76 natural Desktop requests blocked with 429       | 3667 → 3668                  |
| 01:16:36.438              | First natural batch success after automatic expiry | 3668 at 01:16:35             |
| 01:16:40                  | Batch traffic passing again                        | 3860                         |
| 01:16:45                  | Continued batch fan-out                            | 4319: +651 after restoration |
| 01:17:12                  | Continued batch fan-out                            | 5051, remaining 0            |
| 01:18:31                  | Before cleanup                                     | 6380                         |

One additional unauthenticated synthetic probe returned 429 at 01:15:05;
it is excluded from the 76 natural requests. The operator refreshed Desktop
during the block: 58 blocked requests arrived at 01:16:24, then two at
01:16:27. Other app traffic remained live: 67 other `claude.ai` HTTP 200s,
three 304s, and two successful calls each to GitHub `compare-refs` and
installation-status. There were no direct `api.github.com` flows during the
block. Neither transition required another proxy or Desktop restart.

The observed debit during the block was **one**, not literally zero. Direct
samples at 01:15:27–01:16:19 read 3667, then 01:16:24/30 read 3668.
The independent Mimir series retained two samples inside the block, both
3667, at Unix times 1788657298.191 and 1788657358.191. Its actual scrape cadence
is once per minute, not fifteen seconds. The direct sampler has gaps between
bounded runs, including 01:13:56–01:15:27; missing data is not a quiet period.

After expiry and before cleanup the capture contained 309 batch attempts:
290 HTTP 200 and 19 incomplete. The first groups were 31 successes at
01:16:36, 30 at 01:16:51, and 58 at 01:17:08. This intervention/reversal is
stronger evidence than timestamp correlation, but still does not allocate
account-wide charges to individual cloud POSTs or exclude other consumers.

At 01:18:39 the experimental addon/marker were removed and the proxy resumed
the original capture with verified append mode. The experimental file remains
private and intact. A narrowly owned runtime systemd append override protects
the original capture until declarative host activation. The source append
fix is merged as [#5673](https://github.com/agentydragon/ducktape/pull/5673).

### Candidate frontend mechanism and remaining visibility boundary

The fixed 289-request window splits into 149 `epitaxy-repopr`, 118
`epitaxy-cichecks`, 15 `epitaxy-discover-repos`, four untagged, two `ccd-sidebar`,
and one `sessions-provider` call. The dominant Epitaxy helper directly posts
singletons, bypassing the shared batch-query hook. Its 120-second stale time,
deduplication, and 25-item batching therefore do not coalesce these callers.

The inspected PR hooks poll at thirty seconds and also refresh on focus or
visibility changes. Cloud CI hooks use five seconds while checks are initially
absent, then thirty seconds. All 65 successful captured CI replies already
had nonempty checks, so that empty-check warmup does not explain the observed
successful-repeat cadence. There were 100 PR repeats and 33 CI repeats
10–20 seconds after the previous successful reply; median successful request
latencies were 0.916 and 1.462 seconds respectively.

The cloud CI mapper drops PR state, while its consumer reads `prState` to
stop terminal polling and propagate terminal state. Its active observer can
also suppress companion PR polling. Of 65 successful CI replies, 56 already
reported merged/closed PRs; 29 were followed by an identical request, 26 within
10–20 seconds. Those 29 requests are a measured candidate suppression
opportunity, **not a measured fraction of GraphQL points saved**.

Candidate vendor fix: preserve normalized `prState`, keep PR-number-safe
state propagation, suppress terminal automatic interval/focus/unhide refresh,
and retain explicit manual refresh. Shared scheduling/caching across singleton
observers is a separate improvement. Focus/unhide refresh can persist even
when an interval stops, so missing state alone is not proven to explain every
repeat. No remote frontend bundle was modified.

The installed native `app.asar` lacks the endpoint string. Public frontend
source reproduces the singleton mechanism and state omission across inspected
variants, but cache URLs and imports do not prove an executing asset hash.
Renderer initiator stacks, query-client/key lifecycle, and focus/visibility
timelines remain the visibility boundary for the precise 10–20-second trigger.
Reproducible source: [singleton helper](https://assets-proxy.anthropic.com/claude-ai/v2/assets/v1/c0547825d-DBs1L6EK.js)
(SHA256 `8ca4ebb100f806d1295d8106f0ef90bd42e9a60ec100f5f9c9e8420e13c86dd9`),
[older CI hook](https://assets-proxy.anthropic.com/claude-ai/v2/assets/v1/c360a9e1c-DacXJRFm.js),
and [visibility-gated CI hook](https://assets-proxy.anthropic.com/claude-ai/v2/assets/v1/ca80fca8d-C3-_3TMY.js).

## Temporary mitigation and acceptance window

[PR #5675](https://github.com/agentydragon/ducktape/pull/5675) adds a default-off
exact-route block with explicit wyrm2/rugged opt-ins and routes rugged's normal
Desktop through the same wrapper. It retains private append-only startup,
thirty-second heartbeat, blocked-count, and shutdown events. Other hosts and
other paths are unchanged. The expected tradeoff is stale/unavailable automatic
Desktop cloud GitHub status, not a block of all Claude or GitHub traffic.

The operator explicitly approved persistence across reboots until the temporary
mitigation is disabled. Configuration publication is not host activation;
record the verified live start separately. Do not claim rugged is protected
merely because its source configuration opted in.

The scoped **wyrm2 runtime mitigation started at 01:35:27.490 UTC**, before
the reset observed at 01:35:46. The normal Desktop PID remained unchanged.
A synthetic caller-tagged POST returned 429 at 01:35:28.189; this is one known
non-application increment in the block counter. Private mode-0600 heartbeats
arrived at 01:35:57 and 01:36:27. No natural batch attempt had arrived by those
heartbeats, so synthetic success alone was not representative app-use proof.
By **01:40:59.208**, the cumulative count reached 84: **83 natural app attempts
plus the known synthetic probe**. Desktop's original network child retained
nine established loopback-proxy connections. The account was zero used through
01:40:35, then one used at 01:40:41 and still one at 01:41:18. This verifies
natural requests reaching the standing block, not a completed healthy hour.
Rugged has source configuration only; no live activation was performed there.

Start the acceptance clock at a clean hourly reset after verified mitigation,
with independent account-counter and mitigation coverage. Check after 48 hours;
prefer seven days without any exhaustion before declaring resolved. Coverage
gaps, exporter failures, a missing proxy, or no representative Desktop use are
not evidence of success. Record minimum remaining, last exhaustion, reset
boundaries, passed/blocked route counts, and coverage, not just a single
post-reset reading. A healthy minute-scraped counter can still miss a brief
end-of-window exhaustion; retain the stronger direct/error evidence when present.

## Next experiments and independent follow-ups

1. Verify standing mitigation, capture heartbeats and the next clean reset;
   maintain the covered multi-day acceptance window above. The normal-profile
   proxy, authenticated traffic proof, and two-minute reversal are complete.
2. Capture bounded renderer initiator/focus/visibility evidence to distinguish
   refresh amplifiers without deleting sessions or changing the normal profile.
   A selective `epitaxy-*` caller block versus allowed sidebar/provider batches
   is a possible later experiment, not an already tested fix.
3. Keep phone-control limits explicit: recurrence while the app remained
   force-stopped shows active phone use is not necessary for this recurrence;
   it does not exclude all previously triggered server-side activity.
4. [#5666](https://github.com/agentydragon/ducktape/issues/5666): pilot attributed
   HTTP proxying for the three GitHub-provider Terraform runners, then assess
   node-level interception. Require keep-alive, bypass, loss, and runtime trust
   tests. A CONNECT relay alone does not supply GraphQL operation/cost metrics.
5. [#5663](https://github.com/agentydragon/ducktape/issues/5663): retain host
   process/resource history using established tools, initially an explicit
   wyrm2 `atop` pilot. Process-exporter is complementary grouped resource
   monitoring, not a complete short-lived process ledger.

GitHub documents a shared user budget across PAT/OAuth/user tokens, a separate
installation-token budget, and additional charges for timed-out GraphQL
operations. Moving controlled workloads to installation authentication can
isolate their reliability, but is not identification or correction of the
personal-bucket burn. See [GitHub's quota documentation](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).

Source ordering for Hubble: [v1.19.6 monitor consumer](https://github.com/cilium/cilium/blob/v1.19.6/pkg/hubble/monitor/consumer.go),
[flow-metric callback](https://github.com/cilium/cilium/blob/v1.19.6/pkg/hubble/cell/hubbleintegration.go#L261-L265),
then [observer history ring](https://github.com/cilium/cilium/blob/v1.19.6/pkg/hubble/observer/local_observer.go#L175-L214).

**No claim of resolution:** endpoint attribution is strong, the exact upstream
work and renderer refresh trigger remain partly unobserved, and the multi-day
acceptance window is not complete. The Hubble/socket instrumentation PRs were
published for review, not deployed during these controls.
