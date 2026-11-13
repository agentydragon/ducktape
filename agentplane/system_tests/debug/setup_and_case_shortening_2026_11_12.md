# Shortening system-test setup and cases

The balanced Pod experiment and two remaining reliability investigations are
dispatched separately. Image split [#15214](https://github.com/agentydragon/ducktape/pull/15214)
remains unmerged: fewer OCI bytes have not demonstrated a CI saving. The
[three-suite image-overlap trial](../../../devinfra/ci/debug/fixture_overlap_three_suite_graph_2026_11_12.md)
also did not establish a faster gate. The selected graph excludes other required
suites, build/lint work and GitHub check clocks.

## What the retained spans permit

The [candidate graph](https://app.buildbuddy.io/invocation/9aa93480-4b18-4073-bbe8-1d1afce70fb9)
has matching suite inputs except for the image-overlap change, as documented in
the linked comparison. Its Pod baseline was cold and its VM baseline was warm.
These are observations of individual runs, not expected savings.

| Milestone from setup start   | Cold Pod | Warm VM |
| ---------------------------- | -------: | ------: |
| Node Ready                   |  18.830s |  1.675s |
| PKI controllers ready        |  50.446s |  7.003s |
| Service images imported      |  73.865s | 27.371s |
| Manifests applied            |  88.936s | 40.355s |
| Certificates and trust ready |  90.968s | 41.548s |
| All images imported          | 149.523s | 48.090s |
| Deployments Available        | 117.952s | 66.912s |
| Stack ready                  | 149.724s | 67.736s |

The Pod environment-tools import alone took 73.030s and finished 31.571s
after Deployments became Available. Improving readiness cannot remove that
tail in this run. In the warm VM run, LLM ingress's readiness wait occupied
16.501s; service imports still occupied about 20s despite the retained node.
The importer omits blobs already present but still opens an import transaction
for every image. Neither a complete import span nor a deployment wait is wholly
avoidable work: imports include unpacking, and readiness includes real startup.

The longest Pod case took 86.423s. Its idle suspension uses the product's
minimum one-minute duration; the controller's clock starts when it observes the
runner idle. Reducing that minute would change the exercised contract. Other
cases can overlap it. Messaging took 53.329s and Pod mTLS replacement 47.006s.
Those cases create their own runners; protocol-only cases already reuse pairs
while opening fresh Sessions. Two retained pairs consume four of the fixture's
five Sandbox slots, so increasing concurrency or moving cases requires an
ownership/capacity analysis.

## Readiness experiment

`//agentplane/system_tests/suites:test_probe_cadence` is manual. It runs a
warmup followed by normal / one-second / one-second / normal probe settings
within one RBE action. Every arm calls the ordinary stack lifecycle, recreating
PostgreSQL, namespaces and certificates and requiring certified cleanup.
Application images, probe handlers, TLS verification, timeout values and
liveness settings stay the same. Startup failure thresholds increase to retain
their existing period-times-threshold budget. Readiness can detect a failed
service sooner with the shorter period; this is an experimental fixture policy.

The existing OTel exporter records setup and cleanup beneath an arm span.
Each arm retains its original/tested fixture, native Pod conditions, and normal
persistence evidence. Check fresh namespace, PostgreSQL Pod and root Secret
UIDs, warm baseline identity, restart counts and the fixture diff before using
the times. The warmup is excluded from the comparison. This controls worker,
source and baseline state; A-B-B-A reduces simple order bias but cannot remove
all changing host load. It measures setup, not a full CI saving.

Kubelet can manually trigger readiness probes between periodic ticks
([v1.34 prober manager](https://github.com/kubernetes/kubernetes/blob/v1.34.0/pkg/kubelet/prober/prober_manager.go)).
Consequently the configured period alone does not establish a ten-second tax.
The experiment is necessary to establish whether changing cadence is useful.

### Observed warm setup comparison

[Invocation 1235175c](https://app.buildbuddy.io/invocation/1235175c-a8c4-431d-acb6-13fc3283847b)
passed all five arms, with an uncached TestRunner execution of 410.373s on
worker `e90acdf4-cb2c-4eb7-882f-3f80b631e1ea`. Queue was 0.060s and input
fetch 2.160s. The local source was `5354b836af88ee17fbabc65c20666f90a0e6216c`
plus this manual experiment; the remote runner checked out `67c2514f87` and
applied the cumulative reverse/local patch. The tested Python file's SHA256 is
`67a23bf5e54f71f1336f4f47ad5026bd8b04a2b2a6eb8559daac8e8145c3fe5d`.

| Arm, in execution order |   Setup | Cleanup | Whole stack lifecycle |
| ----------------------- | ------: | ------: | --------------------: |
| Warmup, excluded        | 73.721s | 27.454s |              106.295s |
| Normal A                | 49.207s | 27.102s |               79.215s |
| One-second A            | 28.869s | 27.631s |               59.416s |
| One-second B            | 37.196s | 27.274s |               68.246s |
| Normal B                | 35.259s | 28.370s |               68.005s |

Mean measured setup was 42.233s normal versus 33.032s one-second, a 9.201s
difference. Whole lifecycle means were 73.610s versus 63.831s. The second
one-second setup was slower than the second normal setup, so the improvement
is not consistent across both adjacent comparisons. This is a small warm
setup experiment, not a latency distribution, cold-start result, case-execution
result or full-CI saving. It does not justify changing required CI yet.

All five persistence records had identical fingerprints, k3s identities and PKI
controller UIDs, with `outcome=warm` and `cleanup=complete`. Each had distinct
namespace, PostgreSQL Pod and root Secret UIDs. All eleven fixture Pods were
Ready with zero container restarts in every arm. Normal fixture JSONs were
unchanged; each one-second fixture differed only in 36 probe period, initial
delay and startup failure-threshold fields. The first one-second arm reached
all Deployments Available 5.813s after manifest application, while its image
import still had another 4.533s to run. PostgreSQL initialization and migrations
therefore do not yet support a large database-snapshot project: that entire
readiness interval was under six seconds in this sample.

Evidence is under `test.outputs/{arm}/` and `test.outputs/test-spans.jsonl` in
the invocation; the span artifact SHA256 is
`e1c7194e85824dd3735a8e9d98346ff21949ddaa038f0e57dcc054b8da2abf7f`.
The experiment overlapped the independent balanced-Pod control and other CI.
Worker identity was held within this experiment, while competing host/provider
load and source page-cache effects were not eliminated. Per-arm CPU/probe cost
was not measured; one-second exec probes can add work throughout a long suite.
Before adopting this, compare the complete selected graph, including case time
and resource use, under both settings.

## Larger candidates

1. **Skip an unchanged import only after proving it is usable.** Compare the
   declared OCI manifest digest with the current containerd target, verify the
   platform's unpacked root filesystem and CRI pin, then skip the transaction.
   Blob presence alone is insufficient. A retained warm stack offers a useful
   target because it still spends time reopening transactions. The experiment
   must cover same tag/new digest, missing unpacked state, missing pin, GC,
   restart and a later Sandbox using the image. Existing draft
   [#15126](https://github.com/agentydragon/ducktape/pull/15126) batches imports;
   it has no matched speed ranking and must not be counted as landed savings.
2. **Mount pinned harness artifacts directly in the fixture.** Claude is already
   one declared executable and Codex a declared file tree before their OCI tars
   are built. A read-only fixture mount could avoid containerd unpacking them
   again. It must preserve paths, executable permissions and each Sandbox's
   fresh writable home/state. Measure RBE input-fetch cost as well as setup;
   source files are not free to download. This changes the packaging boundary:
   retain a required real image packaging/boot check rather than silently
   dropping that coverage. No implementation or saving is established.
3. **Make baseline reuse dependable before retaining more mutable state.** The
   existing baseline already retains k3s, PKI and, for VM suites, virtualization
   controllers and compatible image storage. First measure cold/warm frequency
   and causes of rebuilds on natural backend heads. A pool of whole ready app
   stacks has a higher isolation burden: databases, credentials, issued roots,
   workspaces, leases and crashed clients must be fresh or demonstrably reset.
   It also needs a versioned image/CRD contract and idle-capacity accounting.
4. **Clone an empty migrated database, with migration coverage separate.** A
   versioned empty template keyed by exact PostgreSQL and migration inputs
   could avoid repeated initialization in every independent lane. It needs
   fresh per-run data/identity and a required path exercising production's normal
   startup migrations. First isolate PostgreSQL initialization and migration time
   from probe delay; the retained spans do not yet justify this larger change.

For case execution, the immediate experiment remains balanced independent Pod
lanes with the same 21 cases. A result has to include duplicated setup/cleanup
and concurrency beside MCP and VM. Later hardware-accelerated VM experiments
must include actual compatible worker capacity and queueing; the existing manual
KVM target is not evidence that the required graph can move there unchanged.
