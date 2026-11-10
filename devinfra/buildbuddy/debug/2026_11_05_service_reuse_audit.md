# Slow-test service reuse audit

Inspected source: `7fc04701455bc2c47fd6961b0140dce82a94d609`.
The recommended lifecycle and upgrade workflow are in
[service reuse](../docs/service_reuse.md). No execution policy, service lifetime,
pool deployment or CI warming job is changed by this audit.

## Evidence and scope

Read the complete published latency report at gh-pages commit
`1ead186a30`, its manifest and relevant execution/prior-work evidence. Its earlier
feedback cohorts and negative experiments remain historical; this audit does
not refresh PR-feedback percentiles or resume the paused broad latency goal.

Enumerated 389 `ROLE=CI`, `command=test` invocations updated between
2026-11-04 00:55 UTC and 2026-11-06 00:55:29 UTC. Of these, 256 were completed
`devel` sweeps with pattern `//...`. Selected the newest sweep in each occupied
two-hour creation-time stratum, plus the eight longest completed sweeps: 27
distinct invocations, 21 with execution records. This deliberately oversamples
slow runs and excludes PRs lacking the CI role. It is not a fleet census.

Their execution records contain 2,261 distinct originating `NEW` TestRunner
actions across 377 labels. Excluded 109 non-NEW test links; reused target-summary
durations were not counted as new work. Process totals below are overlapping
action-seconds, not CI wall time or predicted savings.

| Workload                                            | NEW actions | Median process time | Maximum process time | Summed process time |
| --------------------------------------------------- | ----------: | ------------------: | -------------------: | ------------------: |
| `//agentplane/app:test_thread_browser`              |   96 shards |              87.8 s |              283.3 s |           9,661.2 s |
| `//agentplane/app:test_bridge`                      |   24 shards |             112.4 s |              211.7 s |           2,843.6 s |
| `//agentplane/system_tests/suites:test_vm`          |           8 |             324.5 s |              383.8 s |           2,631.6 s |
| `//agentplane/system_tests/suites:test_pod`         |           8 |             297.3 s |              416.7 s |           2,314.5 s |
| `//agentplane/system_tests/suites/mcp:test_actions` |           6 |             253.6 s |              525.4 s |           1,687.2 s |
| `//props/agents/critic_dev/optimize:test_e2e`       |           1 |             190.9 s |              190.9 s |             190.9 s |

The Props population is sparse in this sample. Target history contains many
cached repeats of old durations; those are not additional measurements.

## A. Give Props' image-loading work an appropriate family

In [mainline invocation 7d7bdf6e](https://app.buildbuddy.io/invocation/7d7bdf6e-bcf2-5a31-8e27-466047c5a465)
at `bedd5792b85e7ec1da7386030fa30032c7467814`, the recorded Props optimize
Command enabled recycling but had neither a dedicated family nor remote-snapshot
permission. Its restored snapshot's last task was an October 30
`PyWheel //finance/plaid/spend/desktop:plaid_spend_desktop_wheel`, resolved from
the snapshot's recorded origin invocation. Thus a VM restore did not imply a
useful Props image cache.

Existing OTel spans and setup logs show:

| Props target          | PostgreSQL image load | PostgreSQL readiness | Migration/recreate | Registry startup | Whole action |
| --------------------- | --------------------: | -------------------: | -----------------: | ---------------: | -----------: |
| Optimize E2E          |                23.9 s |                4.4 s |             0.31 s |           1.50 s |      190.9 s |
| Grader report failure |                36.5 s |                4.0 s |             0.48 s |           1.72 s |      158.1 s |
| Critic E2E            |                 9.4 s |                3.4 s |             0.28 s |           1.38 s |      103.8 s |

These phases are nested in fixture/test spans; do not add both parents and
children. Optimize also serially pushed three application images in 23.9 s,
then pulled them concurrently over a 61.8 s envelope, from existing INFO logs.
The 61.8 s is not the sum of three pulls. Concurrent prefetch and digest-checked
Docker image lookup already exist in `e2e_container.py` and `docker_executor.py`.

**Concrete next change:** introduce a Props container-test image family from
the authoritative PostgreSQL, registry and Ryuk pins. Keep application images
as current action inputs, with the existing digest checks and real authenticated
proxy/push path. Start with the actual container E2E consumers; a PostgreSQL-only
target should not accidentally seed a supposedly prepared registry stack.

### Bounded local-snapshot experiment

At the inspected source, temporarily added only
`test.runner-recycling-key=props-image-audit-20261105-v1` to
`//props/agents/critic:test_e2e`. Ran that target twice with
`bbr test //props/agents/critic:test_e2e --nocache_test_results --test_output=errors`.
Save policy remained unset; remote-snapshot eligibility was not added.
The temporary BUILD edit was removed after collecting evidence.

| Arm                                                                                | Action process |       CPU | `docker_load` spans | Result              |
| ---------------------------------------------------------------------------------- | -------------: | --------: | ------------------: | ------------------- |
| [Seed](https://app.buildbuddy.io/invocation/f7236ffa-da1e-4936-bbfc-eb31eee763d1)  |       86.111 s | 164.421 s |                   3 | All 3 cases passed  |
| [Reuse](https://app.buildbuddy.io/invocation/346ef4fc-a92c-4bba-815b-6df9958db156) |       78.046 s |  84.062 s |                   0 | Same 3 cases passed |

Both were NEW physical executions with the same Action digest
`8666d8cc93dfb403550d54156a9c403227875643d63c183b70887feb9ba004b3` and the
same executor. The second VM metadata names the seed's exact physical execution
as its snapshot origin. The first action reported local-save intent; no remote
opt-in was present. The second eliminated all three Docker imports and its first
application-image pull fell from 10.522 s to 0.115 s, while the real proxy/push
path still ran. PostgreSQL readiness remained fresh: 2.5 s then 3.5 s.

This proves local image reuse for these inputs and three unchanged cases.
It does not establish cross-host availability, live-daemon retention, completed
remote upload counts, all Props coverage or a fleet/gate speedup. The observed
8.065 s action reduction and 80.360 CPU-second reduction are one pair, not an
additive forecast for the larger Optimize test.

### Implementation validation

[#14514](https://github.com/agentydragon/ducktape/pull/14514) assigns all ten
container E2E targets the `props-e2e-images-v1` family, derived from the actual
PostgreSQL, registry and Ryuk pins. The registry pin moves unchanged into the
shared pin source. Application images remain action inputs; neither remote
snapshot permission nor a save-policy override is added.

The following are NEW TestRunner executions at pushed implementation head
`caa00532162a2f50c1c8bf2d22d13a4d030078c6`, using `--nocache_test_results`:

| Probe                                                                                            | Target | Process time | Image imports | Import time | Result              |
| ------------------------------------------------------------------------------------------------ | ------ | -----------: | ------------: | ----------: | ------------------- |
| [Cold](https://app.buildbuddy.io/invocation/367f2a25-63ac-4159-9a51-a12a8e6a1442)                | Critic |    153.832 s |             3 |    26.039 s | 3 cases passed      |
| [Other configuration](https://app.buildbuddy.io/invocation/3e37e81b-2629-45c2-8773-37499451cca6) | Critic |     83.076 s |             3 |     9.064 s | Same 3 cases passed |
| [Restored](https://app.buildbuddy.io/invocation/ef6d905c-9cd0-4232-a78b-5f88fcc94400)            | Grader |     50.435 s |             0 |         0 s | 1 case passed       |
| [Restored](https://app.buildbuddy.io/invocation/85e4e194-d63f-43d8-830b-dd3e026845f8)            | Critic |     64.952 s |             0 |         0 s | Same 3 cases passed |

The three critic probes share Action digest
`b7ee908c7e5cd56ebab2c9bc1d5250e4903983f2bda88d40a5894d42e95dfdf1`.
All probes use platform hash
`27371d75bff5d620993e04382740d4520115d9a70a057d8c48156c366333be2f`.
The second critic placement had a different worker/configuration from the first
and remained cold. Both restored probes name snapshots seeded by their own
respective targets in the successful
[canonical CI test invocation](https://app.buildbuddy.io/invocation/b8ef7b95-e8a5-584a-8bd9-cebfdc751b06).
This verifies both consumers attach to the shared platform; it is not a measured
critic-to-grader snapshot handoff or a cross-host availability guarantee.

Cold and warm critic traces each still contain one registry startup, one
database recreate/upgrade and three per-test registry manifest deletions.
PostgreSQL startup remains in the setup log. The real application push/proxy
path and all three cases still run. Process times include other variable
work and are not a causal estimate of CI savings; the direct result is that
restored actions performed no infrastructure image imports.

An initial implementation attempt from a linked worktree included primary-checkout
state in the runner sync. It is excluded above. Final validation used a standalone
clone, with the runner fetching the exact pushed head and applying no patches.
Check the runner's checkout and patch list when validating linked-worktree work.

## B. Reuse PostgreSQL images before retaining live databases broadly

[`util/testing/postgres_fixtures.py`](../../../util/testing/postgres_fixtures.py)
loads images through the existing digest/daemon-ID cache, starts a fresh
session container and stops it at teardown. Its migrated templates already
clone a fresh database for each case. Props uses this fixture plus a migrated
`props_test_template`; cloning/migration is not its dominant sampled cost.

[`agentplane/testing/postgres/postgres.go`](../../../agentplane/testing/postgres/postgres.go)
also loads images, starts PostgreSQL, migrates and terminates it in `Close`.
Its `test_N` clone names are scoped to that fresh server. Keeping the server
would require explicit cleanup/identity ownership across actions; preserving
the current process alone is insufficient.

Shared Python consumers include Props, Agentplane service tests, Plaid, budget,
MCP infrastructure and session export. Indexing overrides the image with
pgvector, so it needs its own compatibility profile. Use explicit profiles for
actual fixture consumers rather than changing every `requires_docker` test or
the global RBE platform. Do not blanket-enable remote snapshots for these tests.

The whole-repository reverse-dependency query hit an unrelated Debian snapshot
504 while fetching `shared-mime-info`; it produced no complete inventory.
A narrowed query hit another Debian 504 for `libacl1`. A separate static scan of
BUILD `deps`/`embed` literals found 215 declared test callers, without macro
expansion, configuration resolution or proof that every test requests a database.
Only two Go fixture consumers had NEW actions in the sampled sweeps (36.6 s and
15.1 s). Source imports establish the listed consumers; the sparse Go timings
do not establish a repo-wide PostgreSQL startup cost or expected saving.

**Next validation:** a PostgreSQL-only image family across two real consumers,
with cold/warm fallback and normal schema isolation. Only then compare retaining
an owned daemon, including roles, connections, replication slots, reset cost,
idle memory and reaper behavior. Keep migration-from-empty tests fresh.

### PostgreSQL profile implementation

[#14516](https://github.com/agentydragon/ducktape/pull/14516) introduces the
`postgres-images-v1` profile from the stock PostgreSQL 18 and Ryuk pins. It
enrolls seven finance targets, four session-export targets and the existing app
bridge target. The bridge retains its nonroot/TCP-Docker properties, so it has a
separate effective-platform variant despite using the same family key. Browser,
pgvector and Docker-only non-PostgreSQL tests retain their existing properties.
Service lifetimes and database cloning do not change.

At pushed head `387ae134dd14289e528fb57156b6b68e01cffaf7`, two NEW executions of
`//devinfra/claude/session_export:test_store` passed the same 12 cases:

| Probe                                                                                 | Process time |            Setup-log image imports | Result               |
| ------------------------------------------------------------------------------------- | -----------: | ---------------------------------: | -------------------- |
| [Cold](https://app.buildbuddy.io/invocation/73e63526-e103-4318-8a60-c1e03d7974f1)     |     37.408 s | Ryuk + PostgreSQL (0.4 s + 19.0 s) | 12 cases passed      |
| [Restored](https://app.buildbuddy.io/invocation/d7d1aade-90f1-4ffb-836e-77e5345a0198) |     13.889 s |                               None | Same 12 cases passed |

The second VM metadata names the first's exact invocation and execution as its
snapshot origin; VM identity, platform/configuration hashes and snapshot-key
version also match. The repeat still starts PostgreSQL, ready in 3.4 seconds.
This directly proves local image reuse, not a live PostgreSQL server surviving
the action or cross-profile/fleet savings.

An intermediate cache-only probe is excluded. `--noremote_accept_cached` alone
did not force a new test execution in an already warm outer runner. Use
`--nocache_test_results` for these deliberate preparation/reuse probes and keep
normal caching for routine CI.

## C. Browser tests: preserve shipped image reuse; measure remaining server cost

The existing browser family is present in actual Commands, with no remote
snapshot opt-in. In the cited mainline run, 13/16 generic PostgreSQL setup logs
had no imports; three cold placements loaded Ryuk/PostgreSQL, totaling 50.4
action-seconds. The 16 fresh generic PostgreSQL startups summed to 54.3 seconds.
These are concurrent shard costs, not a 54-second end-to-end opportunity.

The fixture also starts a distinct logical-WAL history PostgreSQL server and
fresh Electric per test. Their lifetime follows database/replication isolation.
An earlier [complete fixture profile](../../../agentplane/debug/2026_10_31_thread_browser_compute_profile.md)
established large first-case setup costs but did not attribute all backend CPU.
Do not label every setup second as removable PostgreSQL work.

**Candidate after image-family rollout:** lease the two compatible server
configurations independently, while preserving fresh database clones, restricted
roles, replication-slot cleanup, Electric state and browser contexts. Starting
from an empty server remains required where the scenario tests it. A frozen
Electric bound to the previous database is not a compatible baseline for a new
test database. Quantitative daemon-retention savings remain unmeasured.

## D. K3s: maintain and verify existing reuse

The cited Pod and MCP-services receipts both report warm, compatible K3s and
complete cleanup, with fresh application namespace/PostgreSQL/certificate
identities. Actual Commands have recycling, remote eligibility and `newest`,
with forced saves absent. Setup still took 99.4/82.7 s, including 58.3/45.7
summed image-import seconds. The longest environment-tools imports were
26.1/14.6 s. PostgreSQL deployment readiness was only 4.25/4.11 s.

These observations support preserving image/content reuse and current
health/cleanliness checks. They do not prove the paused default-policy trial or
the provider snapshot-volume issue is resolved. The earlier dead-retained-daemon
observation remains a separate unresolved diagnosis.

The historical live-PostgreSQL experiment retained a server while resetting
seven service databases. Five actions passed, but a warm pair took
243.2/230.1 s versus 234.0/220.5 s with K3s/PKI-only retention. Verification
cost about 1 s and reset 3–4 s; LLM ingress still needed 23–25 s. That scoped
negative result remains in [K3s persistence evidence](../../../agentplane/debug/k3s_persistence.md#postgresql-reuse-no-measured-benefit-removed).
It does not measure standalone PostgreSQL fixtures, but it argues against
reviving K3s PostgreSQL retention as the first optimization.

## Priority and implementation boundary

1. Adopt measured image-family reuse for Props container E2E tests. Validate
   every intended consumer and isolate image-only PostgreSQL profiles where useful.
2. Measure the remaining repeated PostgreSQL startup/reset work in browser and
   Go/Python service tests before choosing live-daemon retention. Keep immutable
   templates and mutable state as separate compatibility boundaries.
3. Apply the documented recipe/lease/fresh-state protocol to one daemon only
   after its benefit justifies the ownership, cancellation, reaper and reset work.
4. Use lazy normal-action preparation first. A dedicated warmer/publisher needs
   a measured availability or cold-burst problem and matching consumer keys.
   A read-only-consumer design additionally needs a supported write-control
   contract from BuildBuddy.

Other slow paths retain the published report's prior dispositions: cdk8s/YAML and
Rust compilation are not integration-daemon startup, costly scale tests already
have manual coverage, and fresh VM supervision/recovery scenarios cannot simply
be retained away. No claim here shortens every PR's critical path. The proposed
lifecycle keeps a cold local fixture path; current Docker/browser-on-RBE policy
and local-host validation remain separate from that portability design.

## Follow-up: coverage, normal CI and registry state

The follow-up inspected `devel` at `ed29e38cdbc91bc3b0ccc41e9d8a36546446aebc`.
Image profiles have merged: [#14514](https://github.com/agentydragon/ducktape/pull/14514)
for ten Props E2E targets and [#14516](https://github.com/agentydragon/ducktape/pull/14516)
for twelve PostgreSQL consumers. [#14524](https://github.com/agentydragon/ducktape/pull/14524)
adds four actual stock-PostgreSQL Props consumers: critic SQL integration,
reported-issue constraints, grading-edge constraints and matchable occurrences.
Browser, pgvector and native bridge platform differences remain explicit.

### Ordinary CI observations

Frozen 21 GitHub `CI` runs created 2026-11-06 01:57:22–02:25:24 UTC. Their initial
execution-list snapshot contained fifteen distinct completed `NEW` test actions
with the intended actual Action platform keys:

| Group                                                                                               | Actions | No infrastructure imports | Recorded restore lineage | Passed cases |
| --------------------------------------------------------------------------------------------------- | ------: | ------------------------: | -----------------------: | -----------: |
| [Props E2E](https://app.buildbuddy.io/invocation/8ca1606d-6baa-5d25-b621-ad7ce80116ea)              |      10 |                         9 |                        9 |           13 |
| [Session-export web](https://app.buildbuddy.io/invocation/99d11c51-65ca-58e6-a4f4-76dda36792e9)     |       1 |                         1 |                        1 |            9 |
| [New Props SQL coverage](https://app.buildbuddy.io/invocation/69dcd2d3-e5d6-5761-8863-0e6172258dd1) |       4 |                         0 |                        0 |           23 |

All 45 cases passed without skips. The clustering action's snapshot origin
resolved to `//props/agents/grader:test_e2e_sleep_wake`, proving a cross-target
handoff inside the family. The cold E2E action imported three images; the four
new SQL actions each imported Ryuk and PostgreSQL. A first placement can be cold
even with the intended key. Missing lineage alone is unknown.

These actions came from three invocations. Five cohort runs lacked an invocation;
three initially partial statuses were refreshed later, but their execution
lists were not. This is not a final census of every action in the cohort. The
other eleven original PostgreSQL labels had no selected fresh execution. This
small sample establishes reuse behavior, not a fleet hit rate or feedback saving.

### Optimize profiles and the selected state boundary

Two actual Optimize runs at `c380e6cfd87214ac1b465f285953bafb9d576463` used the
same Action digest and application manifests. Both restored the same older CI
seed, imported no infrastructure images, and passed the existing orchestration
case. [First profile](https://app.buildbuddy.io/invocation/2adc54ce-9926-422a-b47f-da2e0d264e4c):
138.3 s process, 242.3 CPU-seconds, 16.1 s serial pushes, 33.2 s concurrent-pull
envelope, PostgreSQL ready in 4.8 s.
[Repeat](https://app.buildbuddy.io/invocation/d71b44f3-5e9a-4536-982c-eb5d157d2627):
106.1 s process, 202.2 CPU-seconds, 17.6 s pushes, 16.6 s pull envelope, PostgreSQL
ready in 5.4 s. Existing OTel spans and INFO records supply these phases; nested
spans are not additive. The first Bazel profile also rebuilt application-layer
Tar prerequisites outside the test process; the repeat had one remote action.

The seed predates these application images, and the repeat restored that seed
again. Infrastructure image reuse does not imply an updated application-image
baseline under the local-only default. Do not rotate the infrastructure family
for every application revision or restore forced saves to refresh it.

The selected experiment retains registry content while creating fresh services.
Live PostgreSQL would avoid only the measured 4–5 s startup, with additional
role/slot/reaper ownership work. The registry process itself costs about 1–2 s;
repeated blob uploads are the useful state boundary.

### Registry blob prototype

[#14548](https://github.com/agentydragon/ducktape/pull/14548) is the bounded
Optimize-only prototype. A lease and registry-image/reset fingerprint own one
volume. Every attach clears manifests and unfinished uploads before starting a
new registry; release stops it and clears that metadata again. Content-addressed
blobs and repository layer links survive. Real proxy authentication, manifest
publication, image-definition recording and fresh test databases still run.

The first design, mounting blobs and repository links separately, failed real
upload commit with `invalid cross-device link`. Distribution's filesystem
[Move](https://github.com/distribution/distribution/blob/v3.0.0/registry/storage/driver/filesystem/driver.go#L242)
renames uploads into the blob store. Both paths must use one filesystem.
The corrected [lifecycle run](https://app.buildbuddy.io/invocation/1d177d6f-a668-4fb2-9153-97558304e969)
passed both pytest cases: fresh container identity, retained blob access,
manifest-by-tag and by-digest reset, stale-upload rejection, manifest repush,
foreign-volume preservation, owned cleanup and nonblocking lease collision.

Candidate source `8922a49ff99f1779119935d98a431ea9bb877db2` and strict control
`24ef5b0e6a960a1d30534dcb8ca8ed95d07f9848` have identical Action input-root
digests, arguments and command environment. The Command differs only in its
platform; platform differences are the family key and explicit cache opt-in.
Both use the same VM configuration, but different workers. All application
manifest digests match across arms.

| Warm arm                                                                               | Process |     CPU | Serial image pushes | Pull envelope |
| -------------------------------------------------------------------------------------- | ------: | ------: | ------------------: | ------------: |
| [Control 1](https://app.buildbuddy.io/invocation/73304db2-1ff7-457e-92ff-3ec4381a8be6) |  74.6 s | 106.4 s |              12.6 s |        0.16 s |
| [Control 2](https://app.buildbuddy.io/invocation/a7092257-0e02-46e3-b54b-a7fcb936743a) |  80.9 s | 118.6 s |              15.5 s |        0.17 s |
| [Cached 1](https://app.buildbuddy.io/invocation/5527b6a1-e857-4cc5-a4d6-ea742fa1ca29)  |  68.8 s |  75.8 s |               1.0 s |        0.17 s |
| [Cached 2](https://app.buildbuddy.io/invocation/75b82427-628c-41bc-a7fa-572b237632e0)  |  57.3 s |  65.2 s |               0.8 s |        0.15 s |

Both warm candidate actions name their exact cold seed
[`f0593b77`](https://app.buildbuddy.io/invocation/f0593b77-5156-42cc-8569-50684e0c7c04)
as their origin and import no infrastructure images. Both controls restore their
own seed with no imports. The same Optimize case passes in every run. Earlier
controls without the new helper ran in 72.0/72.2 s; they are retained as context,
not substituted for the strict comparison. These are bounded process/CPU and
phase observations on two workers, not fleet or PR-feedback savings.

The cache uses about 550 MiB before release; that measurement can include the
final test's manifests/uploads, which release subsequently removes. It is not
snapshot-upload bytes. Defaults remain local-only for these ordinary actions:
no remote eligibility or forced-save override was added. Keep the trial scoped
to Optimize until normal-CI reuse and storage cost justify wider enrollment.
No local-host Docker validation or shared-daemon rollout is claimed.

The earlier prototype head `dd5b32f1f3d57f7b4df377895ada9e1dccf023ef` additionally
refuses reset when another container still references the volume. The
[lifecycle check](https://app.buildbuddy.io/invocation/f9e16079-18dd-446a-9b6d-7e81204e68d9)
constructs that leftover mount and verifies it is preserved and rejected;
it does not claim an exhaustive process-kill test. A separate actual
[final-head Optimize run](https://app.buildbuddy.io/invocation/f991d7f4-4622-4571-82e6-902429343eb8)
passed in 65.8 s, with zero infrastructure imports and the same seed/volume
recipe. All four candidate receipts name distinct registry containers while
retaining the same volume and 577,041,306-byte pre-release usage sample.

### Bounded retention and a shared recipe

At `649f86ce9489a9129e8056d846f9ed052755ea44`, the pilot uses one Starlark
recipe to generate both its BuildBuddy family and runtime configuration. It
scrubs metadata across all repositories and discards the owned cache if its
allocated size exceeds 2 GiB at attach or release. Peak action disk use and total
VM storage remain outside that bound. Cleanup now refuses attached storage at
release too, including a simulated Docker stop failure.

The [final-head RBE check](https://app.buildbuddy.io/invocation/9b17bbfe-3b42-4137-b2e0-d5574a41e107)
passed five lifecycle cases and the existing Optimize case. The lifecycle cases
cover retained blobs, tagged and untagged manifest removal, stale uploads,
recipe invalidation, ownership/lease conflicts, independent attach/release size
bounds, and storage preserved after failed shutdown. Manifest checks explicitly
accept OCI media types and establish successful reads before testing removal.
The earlier bare HEAD checks could return 404 because of content negotiation,
even with a present manifest; see the
[pinned registry handler](https://github.com/distribution/distribution/blob/v3.0.0/registry/handlers/manifests.go#L151).
The [changed-library build](https://app.buildbuddy.io/invocation/4d16f064-f3a1-4a9c-ac87-2be615c02890)
also passed at that head, as did changed-file pre-commit and Gazelle drift checks.

Optimize's new family
[seeded cold](https://app.buildbuddy.io/invocation/a9c3e68a-f1a3-4978-8bd6-57156b94a5b6)
at `83900b8fa46f44bf3512ac5474cdfcd54146d3ae`, in 107.9 s with three
infrastructure imports. The final-head repeat took 62.1 s with none. Its VM
metadata identifies that seed's exact execution as the snapshot origin; recipe,
volume and the 577,041,306-byte pre-release usage match, while registry container
IDs differ. This validates cold/warm operation of the bounded recipe, not a new
controlled performance estimate or snapshot-transfer measurement. Remote-save
eligibility and save-policy defaults remain unchanged.

The refactor at `d7e0546b71e382be915ff50d5883f68af0732bcc` uses pinned
`filelock` native locking, exact Docker volume lookup and one-volume receipts
(`volume_name`). Test cleanup belongs to pytest fixtures. The runtime helper
shrinks from 255 to 194 lines, preserving ownership checks and the 10 s cleanup
wait. All five [lifecycle cases and Optimize](https://app.buildbuddy.io/invocation/61adf94b-1965-4ee0-8d1e-abdf0aeb96fe)
pass, as do the [changed-library lint/build checks](https://app.buildbuddy.io/invocation/5a0f8720-4cf2-4840-ab6a-71a1c4af02ba).
Cold-volume cases still pass; Optimize restores the same pre-refactor cold seed,
recipe and volume with a fresh registry and no infrastructure imports, in
61.5 s. This establishes compatibility with the prior baseline without changing
the recycling family or snapshot-save policy; it is not a new latency comparison.
