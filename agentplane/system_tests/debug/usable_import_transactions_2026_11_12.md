# Usable OCI import transactions

This candidate addresses [#16028](https://github.com/agentydragon/ducktape/issues/16028).
It skips a transaction only when containerd's exact tag and manifest digest match the declared
single-image OCI layout, its native platform content check and overlayfs `IsUnpacked` check pass,
and CRI reports the tag pinned on the layout's config digest. Other images take the existing import
path. A failed check command remains an error. Fresh workload/namespace/database/PKI cleanup is unchanged.

The native check uses `ctr images check --quiet` with server-side name, target digest and pin-label
filters. This command prints only complete, unpacked references and can return success with an empty
list. The implementation consumes that list, rather than treating exit status zero as a hit or parsing
the human-readable table. See [containerd's implementation](https://github.com/containerd/containerd/blob/v2.1.5/cmd/ctr/commands/images/images.go)
and [image filters](https://github.com/containerd/containerd/blob/v2.1.5/core/metadata/adaptors.go).

## Acceptance

[Initial acceptance](https://app.buildbuddy.io/invocation/42b6e5d6-b0e7-4a61-98dc-06a6ab1ee828)
passed cold import, a clean live-baseline attach and repeat import. The later
[expanded acceptance](https://app.buildbuddy.io/invocation/5bcb50a3-9acf-4723-b3c8-725077587c94)
passed same-tag/new-manifest detection, a complete control-plane restart retaining the image store,
missing pin, missing committed root snapshot, and CRI RemoveImage followed by reimport. CRI RemoveImage
is the deletion boundary used by kubelet GC; it is not an induced disk-pressure GC cycle. These runs
used base `4415ac2544aef190cce5596d5ee2e73ba627b56f` plus the candidate patch. Subsequent
assertions also reject a stale CRI config ID and fail if a usable image opens a transaction.

The first expanded attempt failed at the experiment's `ctr images label --replace-all` invocation:
containerd tried to validate an empty target. It had already passed retag and restart assertions.
Using an explicit empty pin label exercises the intended missing-pin state and passed. This was a
probe correction, not importer performance evidence.

## Measurement

The manual `//agentplane/system_tests/suites:test_import_transactions` target runs warmup, control,
fast, fast, control on one physical action. Controls disable only the usable-image shortcut. Every arm
uses ordinary probe settings and recreates workload namespaces, PostgreSQL, credentials and issued
roots, then requires normal cleanup. Existing spans and persistence artifacts retain phase durations,
image skip decisions and identity evidence. The warmup is excluded. This tests warm setup, not a full
CI saving; host/provider load can still vary within one action.

The complete selected Pod/MCP Actions/VM graph is also required to check real later Sandbox usage.
Its queue, input fetch, setup, case, cleanup, CPU, peak memory and requested CU-time must be reported
separately. A causal comparison needs matching source/runfiles CAS identities and comparable baseline
warmth plus counterbalanced physical workers. Different-worker observations remain validation only.

Within seven days after merge, or at the next naturally selected backend head, collect the full graph's
physical actions and baseline outcomes. Compare matched warm and cold cohorts; report last required-gate
feedback and resource cost. Keep #16028 open until this post-merge benefit is measured. Do not add the
warm-setup difference to overlapping import/readiness savings.

### Same-worker warm setup result

[Invocation 5015ac95](https://app.buildbuddy.io/invocation/5015ac95-5e20-403c-b7f7-ef0c0278bd8f)
passed all five lifecycles on worker `2ec56040-be51-4aed-ae52-f597754a4152`, execution
03:04:48–03:11:59 UTC on 13 November. The source is base `4415ac2544` plus the candidate.
The remote runner checked out `c1b7639098e261cb37a1c4ae822a87bfbf64a85c` and applied its
cumulative reverse/local patch. The actual TestRunner input root was
`79f899c08190f6e3fce2cc34e0f1c8a3b2911e967e0fa7bcf664f9448742e56c/165`.
Its `baseline.py` SHA256 `8da7a528e8171bd9043baceaf1e2375f34ec0fe123061e216a5c7354c048c91c`
and experiment SHA256 `0dd9e068203c26c32678b0105656461c444ad05a5efc02aa21bbc537ffe71538`
match published commit `d943d9700b8bf3e92c2e861c3f731612f28ac021`.

| Arm              |   Setup | Cleanup | Whole lifecycle | Image transactions skipped |
| ---------------- | ------: | ------: | --------------: | -------------------------: |
| Warmup, excluded | 96.096s | 27.426s |        126.735s |                     0 / 18 |
| Control A        | 43.585s | 27.005s |         73.484s |                     0 / 14 |
| Fast A           | 31.438s | 27.860s |         62.329s |                    14 / 14 |
| Fast B           | 32.994s | 27.782s |         64.171s |                    14 / 14 |
| Control B        | 50.398s | 26.467s |         80.346s |                     0 / 14 |

Mean warm setup is 46.992s control versus 32.216s candidate, a 14.776s difference.
Both adjacent comparisons favor the candidate. Its native checks total 0.525s and 0.541s;
control import spans sum to 29.503s and 29.601s. Those spans overlap other setup work, so
these values cannot be added to the setup difference. This is one small warm experiment,
not a cold-start effect, fleet distribution or full-CI saving.

All five persistence artifacts certify complete cleanup, the same k3s identity, fingerprint
and retained PKI controller UIDs. Warmup is cold and four comparison arms are warm. Every
namespace, PostgreSQL Pod and root Secret UID is distinct. All original/tested fixture JSONs
share SHA256 `f1e36e3ca4bed687b6e158b17b621e25b1e4820a04aaa07df3f1f19056df8697`.
Queue was 0.062s, action input fetch 0.541s, execution 431.279s, observed CPU 961.040s and
peak memory 4,844,777,472 bytes. The action downloaded 768,205,815 bytes. These resource
measurements cover the entire experiment; no per-arm CPU saving was measured. The action
overlapped independent experiments and CI on the provider, even though worker identity was held.

### Selected graph validation

[Candidate graph 972fdd73](https://app.buildbuddy.io/invocation/972fdd73-b418-43ca-85a0-0b0756cb8966)
passed 21 Pod, 5 MCP Actions and 1 VM cases; Bazel elapsed was 458.570s. Each suite certified
fresh workload identity and complete cleanup. All three infrastructure baselines were cold, while
retained content varied: Pod skipped 16/18 image transactions, MCP 11/22, VM 23/26. A cold
infrastructure baseline therefore does not imply an empty image store.

| Suite       |  Queue | Input fetch |  Process |    Setup | Cleanup | Observed CPU |     Peak memory |
| ----------- | -----: | ----------: | -------: | -------: | ------: | -----------: | --------------: |
| Pod         | 0.061s |      0.161s | 419.276s |  67.332s | 37.210s |    1083.117s | 5,315,170,304 B |
| MCP Actions | 0.061s |      0.164s | 265.177s |  92.331s | 33.781s |     664.209s | 5,784,641,536 B |
| VM          | 0.059s |      0.143s | 329.223s | 102.122s | 30.436s |    1061.237s | 5,878,689,792 B |

This proves later real Sandbox use under the candidate; it does not measure a causal graph
speedup. The matched-source control disables only the shortcut. Worker/cache exposure and
competing provider load must be compared before assigning a timing difference to this code.
The selected graph excludes other required suites and GitHub gate clocks.

[Final native acceptance ce2d8aa8](https://app.buildbuddy.io/invocation/ce2d8aa8-d977-4521-8485-d10ae4765764)
passed the published stale-CRI-ID and no-import-transaction assertions as well as the earlier
retag, restart, missing-pin, missing-unpack and CRI deletion/reimport boundaries.

### Complete control and actual input comparison

[Control 1a2d1791](https://app.buildbuddy.io/invocation/1a2d1791-4334-441f-8af6-a79d253aafb9)
also passed all 27 cases, with Bazel elapsed 472.642s versus candidate 458.570s. The observed
14.072s difference is not a causal graph result. Each suite used the same worker across the
candidate-then-control pair, but order was fixed, competing load varied, and cache contents
were not counterbalanced. All six baselines were cold and cleanup completed.

| Suite       | Control setup / process / case envelope | Candidate setup / process / case envelope |
| ----------- | --------------------------------------: | ----------------------------------------: |
| Pod         |          128.241s / 433.721s / 241.362s |             67.332s / 419.276s / 283.187s |
| MCP Actions |          137.603s / 318.640s / 120.009s |             92.331s / 265.177s / 115.504s |
| VM          |          143.390s / 355.327s / 155.434s |            102.122s / 329.223s / 166.667s |

Native Action/Command decoding and recursive input-tree comparison found identical commands,
platforms and timeouts for every pair. Pod and MCP have no unique input paths; only `baseline.py`
differs, with 126 and 127 identical CAS subtrees respectively. VM additionally differs in generated
`root_disk.tar`, `root_image/index.json` and six OCI blob paths, so its actual tested image inputs
are not matched. The cause of that generated-image difference is unproven; the EROFS rule already
pins UUID and time. Do not attribute VM or whole-graph differences to the importer. A follow-up
VM comparison must first establish identical actual guest inputs.

Observed CPU sums are 3389.973s control versus 2808.563s candidate. Weighting each TestRunner
process span by its unchanged requested capacity gives 97.534 versus 89.527 requested CU-minutes
(Pod/MCP 4 CU each, VM 8). These are separate resource observations, not billing or a causal cost
saving. Pod's case envelope was 41.825s longer in the candidate and offset much of its observed
setup difference. This reinforces the need to measure full behavior rather than sum import spans.

Native Action digests, including their byte sizes:

```text
candidate //agentplane/system_tests/suites:test_vm: f8aa107ec389a42f8904d5c50e583ed7175ae3291f1e073e2c6f96bd8b794c7d/1852
candidate //agentplane/system_tests/suites/mcp:test_actions: 563d68d5986409223219dfc47b3107056d6ef7fbb523c41a0d4ad95b390622d5/1218
candidate //agentplane/system_tests/suites:test_pod: 726647d3df65c70cb1be05b6b0b3f4c8eb6b0fd8a07cfa5cb26c37608740a88b/1218
control //agentplane/system_tests/suites/mcp:test_actions: c6adb6b870ef0894b04fa7c7c0e446aa2bf06e0361d3d95f1e241af674ce8d81/1218
control //agentplane/system_tests/suites:test_vm: aaf7bb1d42666523017df42cdbecb8da9882a4f8ddfdb0749ecc7cf1da87ed7b/1852
control //agentplane/system_tests/suites:test_pod: 639bc752dc38282b3489602ee339e9166888a3ce54690107e51d505f197811b8/1218
```
