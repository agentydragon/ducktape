# The GitHub GraphQL quota, and who is burning it

Status: the monitoring blind spot is closed. The open question is which consumer.
First 7h of real data: 2026-09-03 23:00 – 2026-09-04 06:00 UTC.

## The blind spot, and why it existed

`github-exporter` scrapes REST `/rate_limit`, which reports the GraphQL bucket as
permanently full. Measured at the same instant, while every GraphQL call returned 403:

```text
GraphQL `rateLimit` field:  limit=5000 remaining=0    used=10783  resetAt=21:56:58Z
REST /rate_limit .graphql:  limit=5000 remaining=5000 used=0
```

`search` lies the same way (403 while `/rate_limit` showed 30/30); only `core` is
truthful. `cluster/exporters/github_graphql_rate_limit` reads the `x-ratelimit-*`
headers off a real GraphQL response instead, and is now deployed for both accounts
(PRs #5490, #5492, #5494 — the last two were a missing Forgejo pull secret and a
read-only root filesystem the `aspect_rules_py` launcher cannot tolerate). Its probe
costs 0 points and GitHub keeps serving it during exhaustion, so it reports precisely
when the account is blocked.

## What the first 7 hours show

The bucket resets hourly at ~:58, and `agentydragon` exhausts it every single hour:

| hour (UTC) | peak `used` | minutes at `remaining=0` |
| ---------- | ----------- | ------------------------ |
| 09-03 23   | 10426       | 55/58                    |
| 09-04 00   | 10000       | 19/60                    |
| 09-04 01   | 10110       | 43/60                    |
| 09-04 02   | 8394        | 37/60                    |
| 09-04 03   | 10808       | 17/60                    |
| 09-04 04   | 10080       | 58/60                    |
| 09-04 05   | 10305       | 56/60                    |

Roughly 2x the hourly budget, every hour, with no idle hour in the window. The shape of
one hour, per minute:

```text
04:57  0     04:58  0     04:59  0
05:00  4967  05:01  2940  05:02  887  05:03  0   ... 0 through 05:59
```

Two things follow from that shape:

- **The burn is synchronized to the reset, not to the clock.** The full 5000 points go
  in under three minutes, starting the instant the window rolls. Nothing here is a
  scheduled job: the hourly CronJobs land at :15, :30, :35, :40, :45 and none of them
  touches GraphQL. This is a client that is blocked, retrying continuously, and taking
  the quota the moment it reappears.
- **The consumer does not back off on 403.** `used` climbs from 5000 to ~10300 during
  the 55 minutes when every call is already failing.

`core` runs 600–1450 requests/h over the same window — real, concurrent, and well
inside its budget, but an order of magnitude above the ~100/h median measured a few days
earlier. So the consumer is not GraphQL-only; whatever it is also does REST.

`agentydragon-agent` is flat at 5000/5000 on both buckets for the entire window. Every
consumer wired to that account is exonerated.

## What consumes a GitHub credential in this repo

Rate limits are per **account**, so anything authenticating as `agentydragon` — PAT,
fine-grained PAT, or user OAuth — draws on the same exhausted bucket. Not all of these
speak GraphQL; the third column is what could plausibly land in the GraphQL bucket.

### Authenticates as `agentydragon` (shares the bucket)

| Consumer                                                        | Credential                                                         | GraphQL?                                    |
| --------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------- |
| Workstation `gh` / agent sessions                               | personal PAT from home-manager + `gh auth` OAuth                   | **yes, heavily** — `gh pr`/`gh issue`       |
| `devinfra/gc/workspace_gc.py`                                   | `GITHUB_TOKEN` or `gh auth token`                                  | **yes** — explicit `api.github.com/graphql` |
| haku-console GitHub MCP (`api.githubcopilot.com/mcp`)           | user OAuth, `haku-console-github-mcp-client-credentials`           | **yes** — several tools are GraphQL         |
| Claude Code web sessions (cihealth etc.)                        | `DUCKTAPE_CI_READ_GITHUB_TOKEN`, `secrets/github-ci-read-pat.yaml` | possible via `gh`                           |
| `tf/github` Terraform root                                      | `secrets/shared/github-pat-ssh-keys.yaml` via `.envrc`             | some provider resources; manual runs only   |
| `github-secrets-sync` (flux-system)                             | `external-creds/github-agentydragon`                               | REST                                        |
| `attic-jwt-rotation` CronJob :30 (nix-cache)                    | same                                                               | REST                                        |
| `authentik-jwt-rotation` CronJob :15 (agents-infra)             | same                                                               | REST                                        |
| `github-exporter` + `github-graphql-rate-exporter` (monitoring) | same                                                               | 0-point probe by design                     |
| `release.yml` / `release-artifact`                              | `GH_RELEASE_PAT`, `secrets/ci/gh-release-pat.sops.yaml`            | REST                                        |
| `devinfra/nixos_bazel_test/run.sh`                              | `GITHUB_TOKEN` into nix `access-tokens`                            | REST                                        |

### Separate bucket

- **`agentydragon-agent` PAT** (`secrets/github-pat-agentydragon-agent.yaml`,
  `external-creds/github-agentydragon-agent`): Claude Code web `GITHUB_TOKEN`,
  public-coder-agent, openclaw-spike, haku-egress-proxy, agentplane-egress, and the
  second exporter pair. Different user, measured idle.
- **`ducktape-automation` GitHub App**: `sync-pins.yml` (\*/30), `prune-releases.yml`,
  `.github/actions/mint-automation-token`. Bills the App installation.
- **`secrets.GITHUB_TOKEN` / `github.token` in Actions** (pr-visuals, GHCR pushes):
  per-run installation token.
- **Git transport, not the API**: Flux `GitRepository` (public HTTPS clone),
  `flux-deploy-key`, `gaffer-private-fetch-pat`.

## The episode ended at 06:00 UTC

The burn was bounded: 23:00–06:00 UTC. From the 06:00 reset onward the account used 121
points in 24 minutes (~300/h) against ~10000/h for each of the preceding seven hours.
Nothing was changed to cause that; it simply stopped.

That end time is the sharpest clue available. In local time the burn ran 16:00–23:00
PDT, and `atuin` puts the last interactive command on wyrm2 at 22:57 PDT — two minutes
before the last hour's window closed. Long-lived `claude` and `codex` sessions were open
across that span (`claude` from 15:06, `codex` at 14:12 and 22:57 PDT), and
`workspace-gc`, which queries `api.github.com/graphql` directly, ran at 22:45 PDT.

**What that does and does not say.** It says the burner runs only while the operator is
driving work, and that it is off-cluster. It does **not** say the burner runs on the
operator's hardware — cloud agent sessions start and stop on the same schedule, run on
neither wyrm2 nor this cluster, and are equally invisible to Hubble.

A cloud session can reach the personal PAT — `devinfra/secrets/web_env.sh` gives it
`GITHUB_TOKEN` from the **agent** PAT but `DUCKTAPE_CI_READ_GITHUB_TOKEN` from the
personal one — but the `cihealth` path that substitutes the read PAT into `GITHUB_TOKEN`
runs about weekly (operator, 2026-09-04), nowhere near often enough to account for seven
hours at 10000 points/h. Treat the mechanism as unknown, not as identified.

One unexplained observation to carry forward: the agent bucket recorded exactly 0 `used`
across all seven hours, yet PRs were opened during the window. Opening a PR is an API
call, so whatever opened them was not using the agent PAT — it was the App or the
personal token.

So there are two live candidates, both off-cluster and both correlated with operator
activity: processes on the operator's own machines, and cloud agent sessions the
operator drives. Nothing measured so far separates them, and no proposed mechanism yet
fits the observed volume.

## What has been ruled out

**Cluster pods — not ruled out, and the earlier claim here was wrong.** Four minutes of
Hubble capture found only the four exporters talking to `api.github.com`
(140.82.116.5/.6), and nothing reaching `api.githubcopilot.com`. That was a quiet
window, not an exoneration. Within ten seconds of first running the connection recorder
on wyrm2, `terraform-provi` (uid 65532, the tofu-controller runners) opened three
connections to 140.82.116.5. The GitHub Terraform provider authenticates with the
personal PAT via `github-secrets-sync-pat`.

Hubble is not blind to these pods. Repeating the capture across a reconcile confirmed
that afterwards — `github-{secrets-sync,branch-protection}` and `flux-webhook-token`
tf-runners all appear, connecting to 140.82.116.5/.6. The roots reconcile on a
15-minute interval and their API traffic lasts seconds, so the duty cycle is a few
seconds in 900; the earlier two- and four-minute captures simply fell between
reconciles.

Measured contribution: over an hour when the runners were visibly working, GraphQL
`used` reached 537 in 57 minutes (~560/h) — real, previously unaccounted for, roughly a
tenth of budget, and nowhere near the 8400–10300/h episode.

The general lesson: a short capture taken while the symptom is absent proves nothing
about the symptom. Sample under load or not at all.

Hubble reports `rugged` and `iguana` unavailable, but both nodes are `NotReady` and run
only DaemonSets.

**Gotcha: Hubble sees pods, not the host namespace.** `enable-host-firewall` is `false`,
so there is no host endpoint and the datapath emits no trace notifications for
host-to-world traffic. Verified 2026-09-04: six `curl https://api.github.com/` calls from
the wyrm2 host, with the filter covering the address the host resolved to, produced zero
Hubble flows while 259 lines of pod traffic to GitHub were captured in the same window.
The `(host)`-sourced flows that do appear are host-to-_pod_, where the pod's endpoint
generates the event. So the capture above exonerates cluster **pods** and says nothing
about host-namespace processes on any node — wyrm2 included, since it is itself a node.
Hubble cannot substitute for a host-local recorder.

**Gotcha: the DNS views differ.** Cluster pods resolve `api.github.com` to
140.82.116.5/.6; the wyrm2 host resolves it to 172.182.252.137 (Azure). An IP-based
filter that covers only one range silently records nothing.

A fleet of ~15 tofu-controller tf-runners, `haku-indexer-chunk-ducktape-public`,
`source-controller` and `image-automation-controller` do hit 140.82.116.3/.4
continuously — that is `github.com` over git HTTPS, which draws on neither API bucket.
Noisy, not guilty.

**The Haku GitHub MCP.** `list_tool_calls` over the whole burn window returns zero calls
to the `github` server — only `sandbox` and `kubectl-passthrough-mcp`. The one in-cluster
consumer that authenticates as the user over OAuth made no calls at all.

**CI volume.** GitHub Actions runs per hour across 19:00–06:00 UTC: 79, 75, 124, 151,
134, 52, 113, 52, 61, 72, 211, 76. Flat-ish throughout, and the current hour is running
at ~190/h with the GraphQL bucket almost untouched. CI activity and the burn are
uncorrelated. (Most CI authenticates as the App or the per-run installation token
anyway; only `GH_RELEASE_PAT` in `release.yml` is the user.)

That leaves the workstation and GitHub-hosted runners — the two places no local
telemetry reaches.

## Catching it next time

The burn is off, so nothing can be attributed live right now, and every remaining
candidate sits where no telemetry currently reaches. The gap to close is a recorder on
the operator's machines that answers one question: while the bucket is draining, is any
local process talking to `api.github.com`?

1. **Record locally**, and note that Hubble cannot do this job (see the host-namespace
   gotcha above). An eBPF probe on `tracepoint:sock:inet_sock_set_state` or
   `kprobe:tcp_v4_connect` yields one event per connection with `pid`, `comm`, `uid` and
   destination — event-driven, so it catches sub-second connections that any sampler
   would miss. Log `ppid` or the cgroup too: an agent's `gh` subprocess reports as `gh`,
   and the cgroup is what names the session that spawned it. The request path is inside
   TLS and is not needed to identify the caller. Absence of local hits during a burn is
   as informative as presence: it moves the whole weight onto the cloud sessions.
2. **Alert on the condition.** `github_graphql_rate_remaining < 500`. The exporter
   samples every minute, so the next episode announces itself within a minute rather
   than being noticed hours later.
3. **Re-run Hubble under load.** The capture that cleared the cluster ran while the
   bucket was quiet. Repeating it during a burn closes that gap.
4. **Then split the two candidates.** If local recording is empty during a burn, pause
   cloud agent sessions for five minutes and watch the metric — the exporter's
   resolution makes that decisive, and it is far cheaper than rotating the token and
   re-adding consumers one at a time.

Only if all of that comes back empty does GitHub-hosted CI become the leading candidate,
and `GH_RELEASE_PAT` is then the single user-authenticated credential to audit.

## Still open

- `/metrics` raises 502 on probe failure, so a genuine outage and an exhausted account
  are indistinguishable from the scrape's perspective.
- No alert yet on `github_graphql_rate_remaining` reaching 0 — the condition is chronic,
  not hypothetical.
- `search` (30/min) and `code_search` (10/min) are per-minute buckets that a 1-minute
  ServiceMonitor interval cannot resolve. Accept them as unobservable or scrape faster.
- The unidentified 8-hourly `core` job at ~01:58 / 09:58 / 17:58 UTC costing 150–180
  requests. Not a k8s CronJob, not a GitHub Actions cron. At 3.4% of budget, not urgent.
