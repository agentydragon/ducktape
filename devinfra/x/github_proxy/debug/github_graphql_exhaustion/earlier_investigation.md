# GitHub GraphQL quota: historical evidence ledger

Current status is in the [investigation README](README.md), with the controlled
[September 5 attribution](attribution_2026_09_05.md) and its
[acceptance checklist](acceptance_monitoring.md). The Desktop cloud batch-status
route is causally implicated in the large burn: blocking its POST endpoint for
two minutes intercepted 76 natural requests while `used` rose by one point;
after pass-through resumed, `used` rose by 651 in thirteen seconds and exhausted
again. This does not establish that the route was the sole consumer or caused
every historical exhaustion. The covered acceptance window remains required;
elapsed calendar time is not evidence that it passed.

This ledger keeps measurements that explain how attribution changed. It is not a
current diagnosis. In particular, the older universal 35–40-second cadence,
Desktop proxy-switch, Hubble keep-alive, and zero-query-cost claims were
superseded by later controls and coverage findings below and in the canonical
notes.

## Quota signal and first episodes

The REST `GET /rate_limit` view was misleading during GraphQL exhaustion. At one
sample, a GraphQL `rateLimit` response reported `limit=5000`, `remaining=0`,
`used=10783`, while REST `.resources.graphql` said `remaining=5000`, `used=0`.
`search` was similarly stale; `core` was truthful. The GraphQL-header exporter
was added and deployed for both accounts ([PRs #5490, #5492, #5494](https://github.com/agentydragon/ducktape/pulls?q=is%3Apr+5490+OR+5492+OR+5494)); it reports the enforced bucket from real GraphQL response headers. Later direct probes returned body `cost: 1`; three probes left `used` unchanged at 565. Describe that as no observed debit, not a zero-cost query. Alerts now live in `cluster/cdk8s/monitoring/rules.py`.

For the first seven sampled hours, the personal bucket exhausted each hour. Peak
`used` and minutes at zero remaining were:

| UTC hour | Peak used | Minutes at zero |
| -------- | --------: | --------------: |
| Sep 3 23 |    10,426 |           55/58 |
| Sep 4 00 |    10,000 |           19/60 |
| Sep 4 01 |    10,110 |           43/60 |
| Sep 4 02 |     8,394 |           37/60 |
| Sep 4 03 |    10,808 |           17/60 |
| Sep 4 04 |    10,080 |           58/60 |
| Sep 4 05 |    10,305 |           56/60 |

At 05:00 UTC, `used` fell from 4,967 to 2,940 to 887, then reached zero by
05:03 and kept climbing to about 10,300 while calls failed. The exporter had
only 9.1 hours of coverage, all overnight; no daytime period had been observed.
The similar peaks did not prove the counter saturated or that one caller kept
retrying. The first burn also stopped around 06:00 without a known intervention. `agentydragon-agent` stayed at 5,000/5,000 on both buckets during that window, although PRs opened; that account was not spending then, but the PR activity does not identify which other credential was used.

A second episode rose from `used=139` at 07:10 to 10,698 at 07:22. The wyrm2
connection recorder started at 07:20:52 and covered only the final 90 seconds
(5–11 API connections/minute). The timing is too late to identify a caller.

Several candidates were ruled out only for that measured window:

- Claude Code transcripts showed mostly REST `gh api` polling and five
  `gh api graphql` calls in 24 hours. `workspace-gc` would issue about five
  aliased queries, roughly 15 points, for 209 branches; about 700 full sweeps
  would be needed to spend 10,500 points in twelve minutes.
- Chrome's two observed sockets carried 27 KB/33 segments and 3.3 KB/7 segments
  in that capture. Those totals do not decode API requests or query costs; a
  connection count alone cannot establish request volume over HTTP/2.
- Deleting the Refined GitHub extension's PAT and several stale authorizations
  before the 08:00 reset did not prevent another burn. The symptom was
  intermittent, so this did not test each credential independently.

At the 08:00 reset, 30-second samples showed `used=10`, then no change for 30
seconds, then +2,046 and +2,017 in successive intervals. `used` plateaued at
4,073 briefly, but the hour later peaked at 10,561. Calling the initial plateau
the end of the burn was incorrect. Five connections/minute continued throughout;
about 15 connections carried roughly 4,000 points. Two `claude` CLI processes
connected during one burst, but connection timing did not measure their request
costs.

The earlier candidate roster included user OAuth/PAT callers, GitHub Apps using
user-to-server tokens, cluster consumers, and CI. GitHub App installation
requests have their own bucket; user-to-server and OAuth traffic draws on the
user bucket. A same-minute probe measured an installation token at 0/5,000 used
and a personal token at 354/5,000, with different reset times. This establishes
the bucket distinction by token type, not App ownership. CI ran 79, 75, 124, 151, 134, 52, 113, 52, 61, 72, 211, and 76 workflows/hour
from 19:00–06:00 UTC. That activity was uncorrelated with the burn; most runs used
installation credentials.

## Workstation and cluster hypotheses

The desktop app's own `main.log` recorded `GhRestClient` GraphQL
`RATE_LIMIT` errors for user 714892. Anthropic issue
[#88320](https://github.com/anthropics/claude-code/issues/88320) described about
569–640 points per sidebar session switch and about 1,970 per turn start; its
reporter observed zero use while Desktop was quit and about two points/minute
while idle. Issue [#63222](https://github.com/anthropics/claude-code/issues/63222)
described a CLI PR-status exhaustion bug and was closed as stale, not fixed.

Quitting Desktop at 08:47 left seven points over seven minutes, but the next
samples showed +2,337 at 09:11–09:15 while the app was down, then +3,560 in 69
seconds after version 1.40609.1 started. Desktop was a heavy consumer, but that
first quiet interval did not establish it as the only one. The claim that the
CLI was the second burner came from connection counts during the +2,337 interval;
subsequent request capture did not confirm that attribution. Preserve both
results as a warning against assigning quota from process timing alone.

A later three-condition comparison measured 70 points while wyrm2 was offline,
2 points in 15 minutes with the machine up and no Claude sessions, and about
5,700 points in four minutes with three CLI sessions. Yet a fully proxied
capture of two sessions over seven minutes saw 22 requests, including eight
copies of one one-node `pullRequest(number) { reviewDecision }` query (one point
each, about 50 points/hour per session). During another burn the account delta
was about 6,758 points while the proxy saw 14 such GraphQL requests. That
residual did not clear the CLI: the proxy and recorder filters covered a narrow
set of GitHub API hosts, not every route. Later evidence found the large
Desktop request on `claude.ai/v1/code/github/batch-branch-status`, outside
`api.github.com`; see the controlled attribution above. The earlier
proxy/environment-switch conclusions are superseded.

Short Hubble captures also missed periodic Terraform reconciles. Three GitHub
provider roots (`github-branch-protection`, `github-secrets-sync`, and
`flux-webhook-token`) were measured at 537 GraphQL points in 57 minutes, about
560/hour. Their reconcile interval was 15 minutes and API traffic lasted
seconds. Suspending all three roots did not stop a later five-minute drain, so
they could not explain that episode's volume; their measured contribution was
real. Other tofu traffic to GitHub addresses included git transport, which does
not use the API quota.

Hubble sees pod flows, not host-to-world traffic when `enable-host-firewall` is
false. Six host `curl` requests produced no GitHub flows while 259 pod flows
were recorded in the same window. The wyrm2 host resolved `api.github.com` to
`172.182.252.137`; pods used `140.82.116.5/.6`. Earlier filters watched only
those addresses, while GitHub `/meta` also listed other API ranges. Therefore
“no observed connection” was scoped to the watched hostnames/IPs and telemetry.
The later Hubble/socket coverage defects and controls are detailed in
[September 5 attribution](attribution_2026_09_05.md), its linked
[kernel incident](../../../../../cluster/docs/lessons_learned/2026_07_16_cilium_set_retval_probe_kernel_7_2.md), and
[recorder coverage](recorder_coverage_2026_09_06.md). Flows, SYN events, and
requests are different measurements; neither flow counts nor one keep-alive
socket can be converted to GraphQL query volume.

The node-off experiment also had a confound. When kubelet and containerd were
stopped for six hours, the roughly 2,200-points/minute bursts disappeared but a
60-points/minute floor remained. The operator was asleep during the same period,
so the test removed pod activity and operator activity together. It did not
separate workloads from user-driven clients. The `github-exporter` pod moved
from wyrm2 to optiplex before a separate quiet-hour comparison and continued its
one-minute REST scrape there; that scraper costs about 60 REST points/hour, not
GraphQL points. The old conclusion that powering off wyrm2 cleared all
off-machine callers was too broad.

## Burst shapes and telemetry limits

Five-second samples on September 5 showed three bursts: +158 points in 5.3
seconds, +146, and +149, separated by 35–40 seconds and a +3 trickle. That
sample averaged about 235 points/minute (14,000/hour), but it was one interval,
not a universal cadence. A later controlled attribution observed roughly
minute-scale bursts instead. Request batching and timeout-related charges were
not distinguished by those counter deltas. See the [current counter controls](attribution_2026_09_05.md#counter-evidence-and-controls).

One 106-second Hubble scan reported about 22.7 GitHub-range flows/minute from
an unresolved source, 13.1 from a public-repository git clone, and lower rates
from exporters and controllers. The unresolved source was later identified in
raw events as the ownerless
`flux-system/infra-drift-tf-runner` pod on wyrm2 (`10.244.5.120`), whose root
plans metal infrastructure. Neither the flow rate nor GitHub-range destination
establishes GraphQL use or host traffic. Two Cilium agents were also outside
coverage during a later scan (`iguana` was `NotReady`; `rugged` had an agent
failure, subsequently repaired). Current loss and coverage findings are in the
canonical attribution and linked kernel incident,
[#6825](https://github.com/agentydragon/ducktape/issues/6825).

A Cilium destination label experiment changed one workload from about 12
unique destinations to 75, including 45 changing DNS-name combinations. Cilium
attached every cached hostname for a shared external IP, with a 30-second FQDN
cache lifetime; the names resolved to five node ExternalIPs. The label chain
was changed to `workload-name|ip` to avoid that cardinality growth while
retaining destination identity. See
[`cilium_network_policy.md`](../../../../../cluster/docs/cilium_network_policy.md).

The personal PAT was bound to seven in-cluster workloads in four namespaces:
three Flux Terraform roots, `attic-jwt-rotation`, `authentik-jwt-rotation`, and
two monitoring exporters. This bounded the proposed rotation blast radius; the
ledger contains no result showing that rotation occurred. Moving those callers
to an installation token was proposed, but the GitHub provider's permissions
for branch protection were not verified.

## Questions that remain

- The exact Desktop renderer refresh trigger and the complete upstream request
  mix remain partly unobserved. The route control implicates a large consumer;
  it does not prove sole causation or account for every earlier exhaustion.
- The README's covered 1–2 week acceptance review is still the closure gate;
  rugged has a source opt-in but no verified rollout.
  Keep [its query and coverage checklist](acceptance_monitoring.md) authoritative;
  do not infer a pass from the planned dates having elapsed.
- The five-minute idle test for GraphQL calls made by the measuring session was
  proposed but not recorded here. Those tools can themselves paginate through
  GraphQL.
- A one-minute scrape cannot resolve the 30/minute `search` or 10/minute
  `code_search` buckets. A separate approximately eight-hourly `core` job was
  observed at 150–180 requests; it was not a GraphQL explanation. The old
  exporter note also recorded `/metrics` returning 502 on probe failure, which
  conflates measurement failure with account exhaustion; verify current behavior
  before relying on it.
- Host connection events do not report HTTP query cost. The recorder emits
  connection events rather than per-request events; bounded socket snapshots
  improve byte attribution but do not cover pod namespaces or provide GraphQL
  costs. Hubble's current loss and scope limits are recorded in the canonical
  notes.

Historical source references: tracking issue [#5213](https://github.com/agentydragon/ducktape/issues/5213); Desktop and CLI reports [#88320](https://github.com/anthropics/claude-code/issues/88320), [#63222](https://github.com/anthropics/claude-code/issues/63222), [#81959](https://github.com/anthropics/claude-code/issues/81959), and [#65985](https://github.com/anthropics/claude-code/issues/65985); recorder and Hubble follow-ups [#5665](https://github.com/agentydragon/ducktape/pull/5665), [#5664](https://github.com/agentydragon/ducktape/pull/5664), [#5663](https://github.com/agentydragon/ducktape/issues/5663), and [#5666](https://github.com/agentydragon/ducktape/issues/5666).
