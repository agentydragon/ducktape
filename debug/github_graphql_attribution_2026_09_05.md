# GraphQL attribution: September 5 evening control

Parent: [#5213](https://github.com/agentydragon/ducktape/issues/5213).
Times below are September 5, 2026, Pacific daylight time unless marked UTC.
The corresponding UTC date after 17:00 is September 6.

## Current conclusion

The personal GraphQL bucket exhausted again at **17:22:42**. The responsible
caller is still unidentified. Instrumentation coverage defects are proven;
neither an IP match nor the absence of a new connection identifies or excludes
a request-level consumer. Earlier claims that Desktop, CLI, or the cluster were
conclusively cleared exceed their controls.

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
`debug/github_recorder_coverage_2026_09_06.md` for measured scope and validation.

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
Authenticated Desktop GitHub traffic still needs an active Code/PR view;
successful startup is not that proof.

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

## Next experiments and independent follow-ups

1. Implement the requested always-proxied normal Desktop on wyrm2; verify
   application operation, the actual browser callback, and captured GitHub
   requests. Correlate request/error timing with the independent quota counter.
   Preserve the normal profile so the fresh diagnostic profile's different
   sessions/workload do not become an attribution control by accident.
2. Observe the post-17:34:13 reset with the phone force-stopped. Keep the
   control's limits explicit; do not infer causation from a quiet interval.
3. For a candidate disablement, change only that caller, record process/socket
   state, and compare several burst intervals. Do not stop active operator
   sessions without approval.
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
unknown personal-bucket consumer. See [GitHub's quota documentation](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).

Source ordering for Hubble: [v1.19.6 monitor consumer](https://github.com/cilium/cilium/blob/v1.19.6/pkg/hubble/monitor/consumer.go),
[flow-metric callback](https://github.com/cilium/cilium/blob/v1.19.6/pkg/hubble/cell/hubbleintegration.go#L261-L265),
then [observer history ring](https://github.com/cilium/cilium/blob/v1.19.6/pkg/hubble/observer/local_observer.go#L175-L214).

**No claim of resolution:** the source of the recurring large debit is open.
The instrumentation PRs were published for review, not deployed during these
controls.
