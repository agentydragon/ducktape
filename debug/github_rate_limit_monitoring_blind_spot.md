# The GitHub GraphQL quota, and who is burning it

Status: **every candidate anyone proposed has been eliminated by measurement, and the
burn continues.** It requires wyrm2 to be up, yet nothing observed on wyrm2 accounts for
it. The single untested lead is that both instruments filtered on a subset of GitHub's
API addresses, so the traffic was invisible to each of them for the same reason. Fix the
filters before trusting any elimination below.

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

## Every known GitHub API consumer

Rate limits are per **account**, so anything authenticating as `agentydragon` — PAT,
fine-grained PAT, user OAuth, or a GitHub App making _user-to-server_ requests — draws
on the same 5,000 points/hr GraphQL bucket. A GitHub App acting on an **installation**
has its own bucket, and git transport (clone, fetch, push over HTTPS or SSH) is not
API traffic at all and spends nothing.

### Workstations

| Consumer                           | Credential                                           | Bucket | Evidence                                                   |
| ---------------------------------- | ---------------------------------------------------- | ------ | ---------------------------------------------------------- |
| `claude` CLI, direct               | unclear; `GITHUB_TOKEN` is unset, so `gh auth token` | user   | recorder: 4 pids to `api.github.com`, `comm="HTTP Client"` |
| Claude Desktop `GhRestClient`      | user OAuth                                           | user   | its own `main.log` logs GraphQL 403s naming user 714892    |
| `gh` CLI                           | `gh auth login` OAuth (GitHub CLI app)               | user   | transcripts: REST polling; 5 `gh api graphql` in 24h       |
| Chrome + extensions                | per-extension PATs                                   | user   | `ss -tnpi`: tens of requests per socket                    |
| Refined GitHub extension           | classic PAT, `repo, workflow, read:project`          | user   | PAT deleted 2026-09-04; burn continued                     |
| `devinfra/gc/workspace_gc.py`      | `GITHUB_TOKEN` or `gh auth token`                    | user   | ~3 points/request, ~15 per 209-branch sweep                |
| `tf/github` Terraform root         | `secrets/shared/github-pat-ssh-keys.yaml`            | user   | 4 `github_user_ssh_key`; PAT since deleted                 |
| `devinfra/nixos_bazel_test/run.sh` | `GITHUB_TOKEN` into nix `access-tokens`              | user   | REST                                                       |

### In-cluster

| Consumer                                                | Credential                           | Bucket | Notes                                            |
| ------------------------------------------------------- | ------------------------------------ | ------ | ------------------------------------------------ |
| `github-secrets-sync` tf-runner                         | `external-creds/github-agentydragon` | user   | GitHub TF provider → `api.github.com`            |
| `github-branch-protection` tf-runner                    | same                                 | user   | branch protection is GraphQL                     |
| `flux-webhook-token` tf-runner                          | same                                 | user   | seen on `api.github.com` via Hubble              |
| `attic-jwt-rotation` CronJob :30 (nix-cache)            | same                                 | user   | REST                                             |
| `authentik-jwt-rotation` CronJob :15                    | same                                 | user   | REST                                             |
| `github-exporter` ×2 (monitoring)                       | both accounts                        | user   | REST `/rate_limit`                               |
| `github-graphql-rate-exporter` ×2                       | both accounts                        | user   | 0-point `rateLimit` probe                        |
| unidentified pod, `comm="main"` uid 65532               | unknown                              | user?  | **open** — recurs on `api.github.com` every ~60s |
| `haku-indexer-chunk-ducktape-public`                    | none                                 | —      | git clone of the public repo, not API            |
| Flux `source-controller`, `image-automation-controller` | deploy key / registry                | —      | git and registry, not API                        |

Measured contribution of the tf-runners together: ~560 points/h during an hour they were
visibly reconciling. They reconcile on a 15-minute interval, and their API traffic lasts
seconds — which is why short Hubble captures kept missing them.

### CI

| Consumer                                           | Credential                | Bucket       |
| -------------------------------------------------- | ------------------------- | ------------ |
| GitHub Actions workflows generally                 | `secrets.GITHUB_TOKEN`    | installation |
| `sync-pins` (\*/30), `prune-releases`              | `ducktape-automation` App | installation |
| `release.yml`, `release-artifact`                  | `GH_RELEASE_PAT`          | **user**     |
| gaffer-private CI image push/pull                  | Gaffer GHCR classic PATs  | user         |
| BuildBuddy runners fetching `http_archive` sources | usually unauthenticated   | anonymous    |

Only `GH_RELEASE_PAT` and the gaffer PATs put CI on the user's bucket; the rest bills the
App installation or the per-run token.

### Separate account entirely

`agentydragon-agent` (`secrets/github-pat-agentydragon-agent.yaml`,
`external-creds/github-agentydragon-agent`): Claude Code web `GITHUB_TOKEN`,
public-coder-agent, openclaw-spike, haku-egress-proxy, agentplane-egress, and the second
exporter pair. Measured flat at 5000/5000 on both buckets across the whole window.

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

## The 2026-09-04 07:10 episode, and what it eliminated

A second burst: GraphQL `used` went from 139 at 07:10 UTC to 10698 at 07:22, about
10500 points in twelve minutes. The connection recorder went live on wyrm2 at 07:20:52
and so caught only the last ninety seconds — during which GitHub API connections ran
5–11 per minute, all light. Ten minutes late.

It still eliminated three candidates by arithmetic rather than by absence:

- **`gh` is not it.** Across every Claude Code session transcript in 24h, `gh` usage is
  REST — `gh api repos/…`, `…/check-runs`, `rate_limit`, mostly in `until` polling loops
  on a 15–30s period. REST calls do not touch the GraphQL bucket. Exactly **5**
  `gh api graphql` invocations in the whole window.
- **`workspace-gc` is not it.** `pr_states` batches 50 branches per request as aliased
  `pullRequests(headRefName:, first: 5)`, so 250 nodes ≈ 3 points per request. With 209
  remote branches that is 5 requests, ~15 points for a full sweep. Reaching 10500 needs
  ~700 sweeps in twelve minutes.
- **Chrome is not it**, despite holding open connections to `api.github.com`: `ss -tnpi`
  puts 27 KB and 33 data segments on one, 3.3 KB and 7 on the other. Tens of requests,
  not thousands.

**Gotcha: connection counting cannot see request volume.** A client issuing thousands of
GraphQL calls over one keep-alive HTTP/2 socket appears in the recorder as a single
line. `ss -tnpi`'s `bytes_sent`/`data_segs_out` is the check that distinguishes a busy
socket from an idle one, and it is what cleared Chrome here.

**Unresolved: whether `used` saturates.** Last night's hourly peaks cluster tightly
around 2x the 5000 limit (10426, 10000, 10110, 10808, 10080, 10305), which looks like a
capped counter rather than seven workloads independently stopping at the same number. If
it caps, a flat `used` says nothing about whether a client is still hammering. Against
that: it is currently still creeping (+12 points in five minutes), roughly thirty times
slower than the post-exhaustion climb during a burn. `remaining` dropping 5000 → 0 in
the minutes after a reset is the signal to trust either way.

## Applications authorized on the account

Rate limits are per account, and this is the part that is easy to get wrong: a **GitHub
App acting on an installation** has its own bucket, but the same app making
**user-to-server** requests spends the _user's_ GraphQL points, and an **OAuth app**
always acts as the user. So an authorized third party can drain this bucket from
infrastructure none of our probes reach — not the workstation recorder, not Hubble.

Authorized as of 2026-09-04, with GitHub's own "last used" (its resolution is weeks, so
it separates dormant from live and nothing finer):

| Authorization                 | Kind       | Last used | Note                                    |
| ----------------------------- | ---------- | --------- | --------------------------------------- |
| GitHub CLI                    | OAuth      | 1 week    | `gh`; measured REST-only here           |
| Exist (hellocodeco)           | OAuth      | 2 weeks   | third-party life-tracking               |
| GitHub Android                | OAuth      | 1 week    | phone                                   |
| LF: EasyCLA                   | OAuth      | 6 months  | dormant                                 |
| Visual Studio Code            | OAuth      | 10 months | dormant                                 |
| ChatGPT Codex Connector       | GitHub App | 1 week    | **acts as the user, from OpenAI infra** |
| Claude (anthropics)           | GitHub App | 1 week    | **acts as the user, from Anthropic**    |
| Copilot SWE Agent             | GitHub App | 1 week    | **agent, acts as the user**             |
| Copilot Chat App              | GitHub App | 1 week    | acts as the user                        |
| Copilot Pull Request Reviewer | GitHub App | 2 weeks   | acts as the user                        |
| Haku Console                  | GitHub App | 1 week    | own app; the console's GitHub MCP       |
| GitHub Copilot Plugin         | GitHub App | 6 months  | dormant                                 |
| buildbuddy.io                 | GitHub App | never     | dormant                                 |
| Renovate (mend)               | GitHub App | never     | dormant — not the hosted service        |
| Slack                         | GitHub App | 11 months | dormant                                 |

The four bolded rows are the shape the evidence has been pointing at all along: an agent
acting as the user, running somewhere with no local footprint, which is why seven hours
of burn left no trace on either the workstation or the cluster. `nix/home/codex` wires a
GitHub connector into Codex explicitly, and the operator runs both Codex and Claude
sessions across exactly the hours the bucket drains.

Two also-installed apps owned by the account, from the installations view:
`Ducktape automation` (CI, installation-scoped, own bucket) and `ducktape-arc` — the
latter unidentified, with no reference anywhere in this repo.

An old `Rai's tests` OAuth app and several stale authorizations were deleted around
07:32 UTC on 2026-09-04, before the 08:00:15 reset. That makes the following hour a
before/after test — weak on its own, since the burn is already intermittent, but the
boundary is clean.

## Personal access tokens on the account

GitHub's "last used" resolution is weeks, which is enough to separate live from dormant.
Fine-grained, 2026-09-04:

| Token                            | Last used | Maps to                                       |
| -------------------------------- | --------- | --------------------------------------------- |
| Ducktape cluster secrets sync    | 1 week    | `external-creds/github-agentydragon`          |
| Ducktape BuildBuddy release      | 1 week    | `GH_RELEASE_PAT`, `secrets/ci/gh-release-pat` |
| gaffer binaries nix install      | 1 week    | gaffer-private fetch                          |
| Gaffer gitops configuration      | 3 months  | dormant                                       |
| Claude Code Web Ducktape CI read | 3 months  | `DUCKTAPE_CI_READ_GITHUB_TOKEN`               |
| Claude Code new-VM PAT           | expired   | deleted 2026-09-04                            |

Classic:

| Token                        | Last used | Maps to                                                   |
| ---------------------------- | --------- | --------------------------------------------------------- |
| Refined GitHub               | 1 week    | browser extension — `repo, workflow, read:project`        |
| Gaffer GHCR image-push       | 1 week    | gaffer-private CI                                         |
| GHCR pull (Gaffer private)   | 1 week    | gaffer-private image pulls                                |
| BuildBuddy GHCR package push | 5 months  | superseded by `secrets.GITHUB_TOKEN`; deleted             |
| Terraform PAT for SSH keys   | never     | `tf/github` via `secrets/shared/github-pat-ssh-keys.yaml` |

Two of these settle open questions. **The Claude Code Web CI read token was last used
three months ago**, which independently kills the cloud-session hypothesis this note
carried earlier — those sessions have not been spending the personal PAT at all. And
`Terraform PAT for SSH keys` is wired but never exercised: `tf/github/.envrc` decrypts it
for four `github_user_ssh_key` resources, so retiring it (a non-expiring
`admin:public_key` token is worth retiring) requires minting a replacement into the same
SOPS path or dropping that root.

## Refined GitHub: hypothesized, then falsified

The browser extension held a classic PAT (`repo, workflow, read:project`, used within
the last week) and drives GitHub's GraphQL API from the browser on every page load. It
fitted every observation: Chrome held keep-alive connections to `api.github.com`
throughout; PR #5526 was merged from the web UI inside the 07:10–07:22 burst; it runs
only while GitHub pages are open, which matches a burn that tracks operator activity
without being `gh`, the cluster, or cloud sessions; it would not back off on 403; and
one branches page against 209 branches is the right shape to spend thousands of points.
A connection-counting recorder structurally cannot see it, which explained the silence.

**The operator deleted that PAT before the 08:00:15 UTC reset, and the burn recurred
anyway** — see below. Unless the extension has a second credential path, it is not the
consumer. Recorded because the reasoning was sound and the elimination is what makes it
useful: the next candidate has to explain the same set of facts without it.

## The 08:00 reset, watched live

The first burn captured while instrumented. 30-second quota sampling:

```text
08:00:20  used=10710  remaining=0        (pre-reset, exhausted)
08:00:51  used=10     remaining=4990     reset landed
08:01:21  used=10     remaining=4990     ~30s of nothing
08:01:51  used=2056   remaining=2944     2046 points in 30s
08:02:21  used=4073   remaining=927      2017 more
08:02:51  used=4073   remaining=927      stopped
```

About 4060 points in the first ninety seconds. **That was a plateau, not the end**: the
08:00 hour went on to peak at 10561, 2x budget like every other. An early reading of
"burst, then stopped" was wrong, and so was the same reading of the 07:10 episode —
both were the same continuous burn seen through too short a window.

**Coverage is the binding limit on every conclusion here.** The exporter went live at
22:59 UTC on 2026-09-03, so the entire dataset is 9.1 hours, 16:00–01:00 PDT. Nine of
those ten hours burned fully; only 23:00 PDT was quiet. No daytime hour has ever been
observed. The "the burn tracks operator activity" reading elsewhere in this note is
therefore under-determined: it is equally consistent with something running
unconditionally that the exporter has not yet watched through a quiet period. One idle
night settles it and costs nothing.

What the wyrm2 recorder shows across those same minutes: **five GitHub-API connections
per minute**, steady before, during and after. So ~15 connections carried ~4000 points,
around 270 points each. Either the caller is off this machine, or it is on it and each
connection carries several expensive queries.

Connections to `api.github.com` over the whole log, by process:

| Connections | Process                                           |
| ----------- | ------------------------------------------------- |
| 51          | Chrome (pid 452999)                               |
| 44          | `claude` CLI (pid 447911) — 11 such processes run |
| 28          | `main`, uid 65532, under containerd               |
| 24          | `claude` CLI (pid 2435149)                        |
| 6           | tofu-controller `terraform-provi` runners         |
| 1           | `gh`, spawned by a `claude` session               |

Two `claude` processes connected at 01:01:46 and 01:01:52 PDT, inside the exact 30-second
window that spent 2046 points. That is the CLI itself, not `gh` and not a Bash tool call.
Suggestive, not conclusive: connection counts cannot tell how many requests each carried,
and Chrome leads the table while its measured sockets stayed light.

## Interventions, in order (2026-09-04)

Each landed before the 08:00:15 UTC reset, so the following hour was a before/after test.
The burn recurred, which positively exonerates all of them.

1. `Rai's tests` OAuth app and several stale OAuth authorizations deleted (~07:32 UTC).
2. Expired `Claude Code new-VM PAT` deleted; dormant `BuildBuddy GHCR package push` PAT
   deleted (superseded by `secrets.GITHUB_TOKEN`, see `devinfra/secrets/ci_env.sh`).
3. `Terraform PAT for SSH keys` deleted. It is wired into `tf/github` via
   `secrets/shared/github-pat-ssh-keys.yaml`; whether the deleted token is the one in
   that file is unverified (decrypt it and call `/user`: 401 means it was). Either way
   that root needs a replacement minted into the same SOPS path, or removing.
4. **The Refined GitHub browser extension's PAT deleted** — the credential the
   hypothesis above rested on.

Coverage caveat for everything measured tonight: `rugged` is down, so the connection
recorder covers wyrm2 only.

## Upstream: this is a known bug, reported twice

Two Anthropic issues describe this consumption, in two different products. Both were
found after the investigation above had independently reproduced their measurements.

**<https://github.com/anthropics/claude-code/issues/88320>** — `area:desktop`, closed
2026-08-24. The desktop app's internal `GhRestClient` spends the user's GraphQL points:
**~569–640 per sidebar session switch, ~1,970 per turn start**, authenticated as the
user. The reporter's controls line up with everything measured here: windows reaching
"10,000+ used, 2x the limit" (attributed to parallelism, not to the counter saturation
guessed at earlier in this note); flat ~2 points/min with the app running but idle; and
0 points across 4m41s with the app quit. They eliminated the same candidates — shell
commands, session hooks, CI, cron, OAuth apps.

**<https://github.com/anthropics/claude-code/issues/63222>** — `area:core`, closed
2026-06-30 **as stale, not fixed**. The CLI's inline PR-status feature exhausts the
GraphQL bucket on repos with many open PRs, hourly, while REST stays healthy. This repo
has many open PRs.

Also relevant: **#81959** reports that `gh api rate_limit` names a different bucket than
the one enforced — the blind spot this note opens with, rediscovered here from scratch —
and **#65985** covers agent-generated tight `gh run view` polling loops, which the
session transcripts here show running on a 15–30s period.

Both products run on wyrm2: `claude-desktop-1.18286.0` (pinned by hand in
<nix/packages/claude-desktop.nix>; the desktop report was against 1.32885.1) and eleven
`claude` CLI processes. The desktop app's Electron renderers are named `Chrome_ChildIOT`,
which is why the connection recorder's early tables filed them under Chrome.

**No disabling setting is known.** Neither issue names a workaround, the settings
inventory in <nix/home/claude_code/default.nix> has no PR-status toggle, and the CLI
ships as a compiled bundle whose extractable strings expose only `DISABLE_AUTOUPDATER`
and `DISABLE_INSTALLATION_CHECKS`.

**The decisive test is cheap**: quit the desktop app and watch `used`. Upstream measured
zero. That also apportions blame between the two products, which decides whether the CLI
issue needs re-reporting with the better measurements collected here.

## Claude Desktop is a heavy consumer — the control, and its limits

Quitting the desktop app at 08:47 UTC, leaving all twelve `claude` CLI sessions running
and one actively driven, changed consumption by roughly two orders of magnitude across
the 09:00 reset:

```text
09:00:33  used=0     remaining=5000   reset; desktop quit since 08:47
09:02:34  used=1
09:04:35  used=5
09:07:06  used=7     remaining=4993
```

Seven points in seven minutes, against 5000 gone in under three minutes in every
previous window and ~10,500 by end of hour. That is the same control, and the same
result, as upstream #88320.

Two things fall out of it:

- **The CLI is not a meaningful contributor here.** #63222 remains theoretically live,
  but twelve running sessions produced 7 points in 7 minutes.
- **The one unexplained quiet hour explains itself.** 23:00 PDT followed the operator's
  last command at 22:57: an idle app with no session switches, which upstream measures
  at ~2 points/min. That hour peaked at 523.

Single trial, single variable. Read at the time as identifying _the_ consumer; the next
section shows that was an overreach. The app is a heavy consumer — the strongest
evidence being its own log, below — but the quiet window it produced was partial, not
total.

## Proof from the app's own log

`~/.config/Claude/logs/main.log` on wyrm2 carries the same line as the upstream report,
naming this account:

```text
2026-09-04 01:23:22 [warn] [GhRestClient] GraphQL errors { '0': { type: 'RATE_LIMIT',
  code: 'graphql_rate_limit', message: 'API rate limit already exceeded for user ID 714892.' } }
```

Dozens of these across 2026-09-03 12:49 → 2026-09-04 01:23 local. This is not inference
from connection counts: the app logs that it is issuing GraphQL and being refused. It is
also the fastest available diagnostic — faster than any probe built here — and worth
reaching for first next time.

## The upgrade does not fix it, and something else is also burning

The confirming window, 2026-09-04, one-minute resolution. The desktop app was down from
08:47 (log shows its shutdown burst at 01:47 local, then nothing until 02:16) and the new
build ran 09:16:06–09:17:15 UTC, sixty-nine seconds:

```text
09:00–09:10Z   +12      app down            quiet, as expected
09:11–09:15Z   +2337    app down            unexplained
09:16–09:17Z   +3560    1.40609.1 running   69 seconds
```

**The bump does not fix it.** `claude-desktop` 1.40609.1 spent ~3560 points in
sixty-nine seconds, in the same quanta as 1.18286.0 (~1550 on a turn start, ~630 on a
session switch). Upstream closed #88320 with `state_reason=completed` on 2026-08-24, and
that close reason was taken here as evidence the fix had shipped — it is not. Whether the
fix never landed, never reached the Linux build, or regressed is unknown.

**A second consumer exists.** ~2337 points went in 09:11–09:15 with the app provably
down. Nothing here explains it. The earlier "seven points in seven minutes" result was
real but partial: the app dominated that particular window rather than being the whole
of the problem.

## The second consumer is the CLI

The connection recorder answers it. Across 09:11–09:15 UTC — the ~2337 points with the
desktop app down — roughly 49 connections to `api.github.com` from wyrm2:

| Source                                          | Connections |
| ----------------------------------------------- | ----------- |
| `claude` CLI direct (4 distinct pids)           | ~22         |
| `gh` / `.gh-wrapped` spawned by claude sessions | ~9          |
| Chrome                                          | ~11         |
| `main`, uid 65532, a pod under containerd       | 4           |
| `terraform-provi`                               | 3           |

The CLI processes connect **directly**, not through `gh`: `comm="HTTP Client"` is the
Node/undici thread name inside the claude binary. Four separate sessions doing it at
once, clustered in the minutes the quota drained. That is
<https://github.com/anthropics/claude-code/issues/63222>, `area:core`, closed as _stale_
rather than fixed.

This also corrects the claim made earlier in this note that the CLI was cleared. That
rested on twelve sessions producing 7 points in 7 minutes — but that window was
09:00–09:07 and the CLI's burst came at 09:11. The CLI was not clear; it had not fired
yet.

**So the answer is both Claude products, independently**, each spending the user's
personal GraphQL budget, each scaling with session count, against an operator running
eleven CLI sessions. That is why nine hours of exhaustion never matched any single
candidate costed here: it was never one thing at machine rate.

## Remaining work

1. **Report upstream.** #88320 is closed as completed but reproduces on 1.40609.1 on
   Linux. The 69-second run above is a cleaner repro than the original report's.
2. **Find the second consumer.** It spent ~2337 points across five minutes with the
   desktop app down, during a `nixos-rebuild switch` and a PR merge. The connection
   recorder covers wyrm2 and the log covers the app; neither covered this.
3. **Mitigate meanwhile**: not running the desktop app is the only measure known to work,
   and it only recovers most of the budget, not all.
4. Keep this note until the second consumer is named. The recorder module's tombstone
   condition ("once that note names the consumer") is _not_ yet satisfied.

## The partition test: it is wyrm2

Every attribution before this came from correlating a spike against whichever process
happened to be visible, and two of them were wrong. The test that partitions instead of
guessing had never been run: **take the machine away and see if the account still
drains.**

wyrm2 was offline from 09:59:58 to 10:12 UTC, across the 10:01 reset. The exporter and
Mimir are in-cluster, so the measurement survived the machine going away.

```text
09:59Z  used=10852        wyrm2 down
10:02Z  used=    0        reset
10:03Z  used=   63
10:07Z  used=   70
10:12Z  used=   70        wyrm2 back
10:27Z  used=   72        still idle
```

**70 points in that hour**, against 5000-gone-in-three-minutes in nine of the previous
ten. The off-machine candidates this note had been building a case for — the Claude,
Codex Connector and Copilot GitHub Apps acting as the user from third-party
infrastructure, `GH_RELEASE_PAT` on GitHub-hosted runners, autonomously running cloud
sessions — are all cleared: none spent anything meaningful while the machine was gone.

The lesson is method, not result. A partition test costs twelve minutes of downtime and
one bit; the instrumentation that preceded it cost a night and produced two wrong
answers. Partition before attributing.

### The confound, raised and dissolved

Taking wyrm2 offline also removed every pod scheduled on it, so "wyrm2 offline → quiet"
did not by itself isolate the operator's local processes. One pod was a live suspect:
a process named `main`, uid 65532, under containerd, connecting to `api.github.com`
**every 60 seconds**, 158 recorded connections, last seen 09:57:57 UTC — two minutes
before the node went down, and never again after the reboot.

It is `github-exporter`, this repo's own REST `/rate_limit` scraper. Its pod was
`…-qvrsw` on wyrm2 and is now `…-wp2dc` on optiplex, started 09:59:03 UTC, fifty-five
seconds before wyrm2's journal stopped. Everything matches: the deployment sets
`runAsUser: 65532`, the upstream image is a Go binary so `comm` is `main`, and the
ServiceMonitor interval is `1m`.

That **dissolves** the confound rather than confirming it. The pod did not stop; it
moved, and kept scraping from optiplex throughout the quiet hour. Had it been the
consumer, the drain would have continued after the reschedule. It did not. It also costs
~60 REST points/hour and no GraphQL at all.

So the partition result stands: the burn stopped because wyrm2 went away.

## The CLI reproduces it; Desktop is not required

Immediately afterwards, with Claude Desktop not running and nothing else changed:

| Condition                                | Consumption               |
| ---------------------------------------- | ------------------------- |
| wyrm2 offline                            | 70 points/hour            |
| wyrm2 up, no Desktop, no CLI sessions    | +2 points in 15 min       |
| wyrm2 up, no Desktop, **3 CLI sessions** | **~5700 points in 4 min** |

That retires the Claude Desktop hypothesis this note was built on, and explains why the
version bump changed nothing: the desktop app was never necessary to reproduce the burn.
Its `GhRestClient` GraphQL 403s are real and remain in its log, so it is _a_ consumer —
but not the one that matters here.

Consumption is also **bursty rather than continuous**: 540, 1688, 2066, 1555, 4144
points in single minutes, separated by minutes of nothing.

## What the proxy actually captured

Two Claude Code sessions, fully proxied, ~7 minutes, 22 requests total:

| Requests | Endpoint                                  |
| -------- | ----------------------------------------- |
| 9        | `GET /repos/agentydragon/ducktape/pulls…` |
| 8        | `POST /graphql`                           |
| 3        | `GET /repos/agentydragon/ducktape`        |
| 2        | `GET /repos/agentydragon/gaffer-private…` |

All eight GraphQL posts are the same query, sent by the CLI's own client rather than by
`gh`, and driven by the statusline's PR indicator:

```graphql
query ($o: String!, $r: String!, $n: Int!) {
  repository(owner: $o, name: $r) {
    pullRequest(number: $n) {
      reviewDecision
    }
  }
}
```

One node, so **1 point**. Roughly 50 points/hour per session. Reaching 5000 points in
four minutes this way would take on the order of 200 sessions.

So this is a real mechanism, identified at request level for the first time, and it is
**not** the mechanism: it explains about 0.1% of the burn. Finding it felt like the
answer, which is the third time tonight a plausible mechanism has proven too small. The
residual comparison — captured cost against the account-wide delta — is the only reason
that was caught rather than written up as a conclusion.

The residual to close is:

```text
account delta = captured GraphQL cost + ~560/h tf-runners + UNKNOWN
```

The tf-runner term is in-cluster and can never be captured by a user-level proxy; it is
bounded and measured, not unknown. Everything else is `UNKNOWN`, and it is currently
almost the whole of it.

## Proxying these two products: what does not work

- **Claude Desktop strips proxy and cert environment variables.** Its bundle carries a
  `Set` of `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `NODE_EXTRA_CA_CERTS`,
  `CLAUDE_CODE_CERT_STORE` and similar, alongside seven `delete process.env` sites, and
  logs `[CCD] Resolved system proxy for Code sessions`. It resolves the _system_ proxy
  itself. Env-based proxying cannot work on it.
- **It also refuses Chromium network-override switches**: `refusing to start — a
debugging or network-override switch is present on the command line`. Both doors are
  closed deliberately. The remaining route is the GNOME system proxy plus the CA in the
  NSS store, which routes the whole machine through mitmdump.
- **Electron hands off to an existing instance and exits.** Launching a proxied wrapper
  while an unproxied app runs captures nothing while appearing to work: the window
  opens, the flow file stays empty. This produced two false starts before it was noticed.
- **`gh` inside a proxied session fails TLS without `SSL_CERT_FILE`.** Go reads neither
  `NODE_EXTRA_CA_CERTS` nor the Nix cert path, so `gh` made no request and spent no
  quota — the proxy suppressing the behaviour under measurement and reporting a false
  negative. Caught by the operator before the first run, not after. Fixed by exporting a
  system-store-plus-mitm-CA bundle to `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and
  `GIT_SSL_CAINFO`.

**Verifying capture completeness by outcome, not by environment**: the connection
recorder shows every process reaching `api.github.com`. While each line reads
`.mitmdump-wrapp`, the capture is complete; a `comm="HTTP Client"` line means a session
slipped out. Checking environments finds only what you thought to look at.

## The measurement that clears the CLI, and breaks the story

At the 11:02:36 reset, with **every** Claude Code session on wyrm2 proxied and the
connection recorder confirming nothing bypassed the proxy:

```text
11:03:19  used=2076    ~2000 points in the first ~40s
11:05:49  used=6758    +4682 over the next 150s
```

The proxy's capture over that period, and indeed over its whole lifetime:

| Requests | Endpoint                                  |
| -------- | ----------------------------------------- |
| 20       | `GET /repos/agentydragon/ducktape/pulls…` |
| 14       | `POST /graphql`                           |
| 9        | `GET /repos/agentydragon/gaffer-private…` |
| 3        | `GET /repos/agentydragon/ducktape`        |

46 requests, 14 of them GraphQL, every one the same 1-point
`pullRequest(number){reviewDecision}` query.

```text
account delta     ~6758 points
captured GraphQL     14 points
residual          ~6744 points   (99.8%)
```

**The CLI sessions are cleared.** Whatever spends this quota is not a proxied session's
traffic, and it did not reach `api.github.com` from any process on wyrm2 that the
connection recorder could see — a non-mitmdump connection would have appeared, and none
did.

### Four facts that cannot all be true of the assumed mechanism

1. The burn requires wyrm2 to be up (partition test).
2. It correlates with starting CLI sessions (~5700 points in four minutes).
3. With every session proxied, the account spent 6758 points while the proxy captured 14.
4. Nothing bypassed the proxy to `api.github.com`.

No process making ordinary HTTPS requests to `api.github.com` from this machine
satisfies all four. An assumption is wrong, and the most likely one is that the traffic
goes to `api.github.com` at all: `--allow-hosts` decrypts only that hostname, and the
recorder's analysis has filtered on those addresses throughout, so traffic to any other
GitHub API host is invisible to **both** instruments simultaneously — a blind spot
built by using the same assumption twice.

**Next step**: widen both filters beyond `api.github.com` before running anything else.
Every conclusion above about "nothing else on this machine touches the API" is scoped to
that one hostname.

## Which tofu, specifically

A 150-second cluster-wide Hubble sweep of GitHub API ranges names twelve roots, all
reconciling on a 15-minute interval:

```text
16  haku-state          12  sso-providers       12  cpap-data
14  flux-webhook-token  12  augur-evidence      12  forgejo-agentydragon-repos
12  agent-machine-access 12 litellm-keys        12  budget-ledger
12  forgejo-claude      10  github-branch-protection
                        10  github-secrets-sync
```

Most of that is `tofu init` pulling modules and providers from `github.com` and
`registry.terraform.io` — each root shows `init`, `plan`, `show`, `output` in sequence,
and git transport spends no API quota. The three that use the **GitHub provider**, and
so reach the API proper, are `github-branch-protection`, `github-secrets-sync` and
`flux-webhook-token`. `github-branch-protection` is the most interesting: branch
protection resources are GraphQL, against a repo with 74 open PRs and 209 branches.

The runner pods are extremely short-lived — they exit between `pgrep` and reading
`/proc`, and the pod is gone before `kubectl` can resolve its container id. Catching
them needs a tight poll loop; Hubble's pod-level attribution is the practical route.

**Counter-evidence, not to be skipped**: `tofu` made _more_ connections during a quiet
window (237) than during a burning one (193). Connection volume does not track the burn,
and connection counting has now misled this investigation four times. These three roots
are what remains after elimination, not what the evidence positively implicates.

## The filters were wrong all along

GitHub's `/meta` lists `140.82.116.4` and `20.29.134.0/24` under `api`. Every filter used
here — the proxy's `--allow-hosts` and every recorder analysis — matched only
`140.82.116.5/.6` and `172.182.252.137`. So traffic to the rest of GitHub's API estate was
invisible to **both** instruments at once: one hand-picked IP list, used twice, producing
a blind spot with the same shape as the `/rate_limit` one this note opens with.

Derive both filters from `api.github.com/meta` before trusting any further "nothing else
touches the API" statement, including the ones already written above.

## The tf roots are cleared

`github-branch-protection`, `github-secrets-sync` and `flux-webhook-token` were
suspended at 11:37:20Z (`spec.suspend: true` on the `kind: Terraform` resources, no
runner pods). The next full window, with the suspension in force throughout:

```text
12:03Z  used=10638   previous hour, exhausted
12:05Z  used= 2181   reset
12:07Z  used= 6489   (+4308)
12:09Z  used=10468   (+3979)
```

Same shape, same 2x budget, same five-minute drain. **Not the tf roots.** The
suspension is reverted; there is no outcome in which it should persist.

Two operational notes from arming it, both of which cost a detour:

- `cluster/k8s/<root>/flux-kustomization.yaml` holds a **Flux Kustomization** whose
  `metadata.name` matches the root. Patching "the file named after the root" suspends
  the Kustomization that _deploys_ the directory, not the Terraform CR the
  tofu-controller drives — and a suspended Kustomization then blocks the correction from
  applying. The Terraform CR lives in `terraform.yaml`.
- The child Kustomizations in `ducktape-flux` apply those CRs, so reconciling
  `flux-system` alone is not enough and reading `.spec.suspend` too early shows a stale
  `<none>`.

## Where this leaves it

Eliminated by measurement, in order: Claude Desktop (burn reproduces without it), the
Claude Code CLI (6758 points spent while a full-coverage proxy captured 14), Refined
GitHub (PAT deleted, burn continued), `workspace-gc` and Chrome (arithmetic and socket
volume), every off-machine candidate (quiet whenever wyrm2 is down), and now the
GitHub-provider tf roots.

The burn requires wyrm2 up and nothing observed on wyrm2 accounts for it. Those cannot
both be true of a process making ordinary requests to the addresses being watched — so
the addresses being watched are the thing to doubt. `/meta` lists `140.82.116.4` and
`20.29.134.0/24` under `api`; the proxy decrypted only `api.github.com` and every
recorder analysis matched `140.82.116.5/.6` plus one Azure address. One hand-picked list,
used by two instruments, blinding both in the same place.

**The next step is not another candidate.** Derive `--allow-hosts` and the analysis
filter from `api.github.com/meta`, then re-run the residual measurement. Every
elimination above is scoped to the addresses that were watched, and the residual that
cleared the CLI is the only one that compared against the account-wide counter rather
than a filtered capture.

## Knobs not yet turned

Each cuts one credential or one class of caller. Turn one at a time: the burn is
intermittent enough that two simultaneous changes explain nothing.

**`gh auth logout` on wyrm2.** Verified 2026-09-04: `GITHUB_TOKEN` is unset in both an
agent session and a fresh login shell, nothing under `nix/` sets it, and
`~/.config/gh/hosts.yml` (written 2026-07-07) holds an `oauth_token`. So local `gh` runs
on an interactive `gh auth login` credential — the "GitHub CLI" OAuth app, last used
within the last week — and not on a SOPS PAT. Logging out therefore cuts more than
personal `gh` use: with no `GITHUB_TOKEN` in the environment, anything on this host that
wants GitHub has to fall back to `gh auth token`, including `workspace_gc.py`'s fallback
path and plausibly whatever the `claude` processes use when they reach
`api.github.com`. Cost: local `gh` stops working until an interactive re-login, which
also breaks the PR and CI-watching loops other sessions run.

**Idle with processes running.** Leave wyrm2 awake with its `claude` processes alive but
unattended, and start no cloud session. Splits "background behaviour of processes that
exist" from "driven by operator activity" — which plain sleep does not, since suspending
stops everything at once.

**Stop cloud sessions only**, leaving local work untouched — aimed squarely at the
`Claude` GitHub App, which acts as the user from Anthropic's infrastructure and is
invisible to every probe deployed here.

Unrelated defect found while checking the above:
<devinfra/claude/claude_hook/profiles/cli/context.mako> tells every CLI agent session
"`GITHUB_TOKEN` available (personal PAT from home-manager)". On wyrm2 that is false, and
agents are being told they hold a credential they do not.

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
