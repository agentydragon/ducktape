# Shared BuildBuddy CAS proxy trial on KVM

**Decision. Keep required KVM CI on Cloud direct.** In the healthy trial, the shared proxy took longer to stage the same input tree even after it was warm. Warm blob hits reduced upstream payload reads, while action preparation still depended on Cloud metadata and authentication. A restarted proxy remained unready during the recovery check. [Issue #18365](https://github.com/agentydragon/ducktape/issues/18365) · [CI latency report](https://agentydragon.github.io/ducktape/latency/#kvm-cas)

## Setup

The trial ran on 21 November 2026 UTC (20 November Pacific time). [PR #18367](https://github.com/agentydragon/ducktape/pull/18367) added the manual pool and shared proxy at [`6c48e88eaed6959cf67e4538faf9782bfb142709`](https://github.com/agentydragon/ducktape/tree/6c48e88eaed6959cf67e4538faf9782bfb142709). The two disposable KubeVirt workers ran separately on `ovh-ns1001419` and `ovh-ns104952` in `hil-ovh`. One BuildBuddy Enterprise Cache Proxy `enterprise-v2.323.0` ran on `ovh-ns1001419` with an 8 GiB Pebble cache on a 12 GiB local SSD claim. The workers kept Cloud as their scheduler. Changing the cache target recreated the worker disks and preserved the proxy pod and disk.

The historical manual target was `//agentplane/system_tests/x/firecracker:test_firecracker_shared_cache_canary`. Collection-only probes staged the full Firecracker input tree and excluded scenario runtime from the comparison. All rows used native TestRunner input-fetch timestamps and the same Action input root `63315318484c77e468323024384515199ac7c95e35a28223ca43bda379ae6932`. Fresh workers fetched 7,467 files and 2,056,368,162 logical bytes each.

## Healthy input staging

| Cache path and state                      | Receiving host  | Input fetch | BuildBuddy invocation                                                                    |
| ----------------------------------------- | --------------- | ----------: | ---------------------------------------------------------------------------------------- |
| Cloud direct, fresh worker                | `ovh-ns1001419` |     8.710 s | [`f0959a56…`](https://app.buildbuddy.io/invocation/f0959a56-a935-4eb9-940d-0fe01339322b) |
| Cloud direct, fresh worker                | `ovh-ns104952`  |     8.628 s | [`72abc591…`](https://app.buildbuddy.io/invocation/72abc591-ddde-45e2-9a16-63402f768198) |
| Retained executor file cache              | `ovh-ns1001419` |     0.333 s | [`281f2bb3…`](https://app.buildbuddy.io/invocation/281f2bb3-012c-43af-abf7-96149ee812f6) |
| Cold shared proxy, fresh worker           | `ovh-ns104952`  |    19.045 s | [`8fc3c7ca…`](https://app.buildbuddy.io/invocation/8fc3c7ca-d465-462d-b12a-d08ce6b0cc64) |
| Warm shared proxy, different fresh worker | `ovh-ns1001419` |     9.292 s | [`beef781b…`](https://app.buildbuddy.io/invocation/beef781b-c4dd-4e18-abd3-70f366b03a78) |
| Cloud-direct rollback, fresh worker       | `ovh-ns104952`  |     8.726 s | [`a06b8219…`](https://app.buildbuddy.io/invocation/a06b8219-c37d-4386-8e41-35d31832fed0) |

The cold fill crossed hosts and made 1,376 upstream ByteStream reads and 58 `BatchReadBlobs` calls. The warm run used a different worker on the proxy host. The same proxy pod served 1,388 ByteStream hits and 816,514,268 compressed bytes. The measured window had zero upstream ByteStream reads and zero upstream `BatchReadBlobs` calls. It still included upstream `GetTree`, `SplitBlob`, and authentication RPCs. The retained executor-file-cache control fetched zero files.

Both full Firecracker runs passed both cases. Test artifacts were retrieved from Cloud after the runs. The executions used warm executor caches on different hosts, so their process times are unsuitable for a causal estimate of proxy impact. [Full direct run](https://app.buildbuddy.io/invocation/dd56d3f5-de39-438a-8a7f-59c572548213) · [Full shared run](https://app.buildbuddy.io/invocation/12aeefce-1f4c-462d-8b06-2241d421da3a)

## Fault and recovery

The temporary Cilium rule denied outbound TCP 443 to the world only for the proxy. Hubble recorded `POLICY_DENY` drops in both fault windows. During the warm probe, some upstream calls still succeeded, so this was an observed egress failure with mixed RPC outcomes.

- **Cached blob.** A standalone ByteStream client read and checksum-verified the seeded 5,242,910-byte blob during the deny. The real warm guest invocation still saw upstream `GetTree` and authentication failures and was canceled at the fault deadline. A later physical execution timestamp followed removal of the deny. [Healthy seed](https://app.buildbuddy.io/invocation/6656e026-986c-4d73-bca0-769c2c1f8d34) · [Warm guest fault](https://app.buildbuddy.io/invocation/9ef620f6-62bf-4127-ac46-594353e7719e)
- **Cold miss.** A checksum control retrieved the new blob from Cloud during the deny. The proxy returned zero bytes for that blob and the cold guest invocation was canceled at the fault deadline. [Cold guest fault](https://app.buildbuddy.io/invocation/78d8ce37-709b-4446-a231-ff17f6d2d43e)
- **Fresh process.** A restarted proxy exited with `DeadlineExceeded` while fetching its initial JWT ES256 keys. After the rule was removed, it still logged TLS handshake resets and remained unready through one additional restart and its 90-second readiness check. The source of the resets was unresolved.
- **Rollback and cleanup.** The effective guest cache target was verified as direct Cloud on both replacement workers. The fresh rollback action passed with the same input root and bytes. The trial pool, proxy, policies, claims, and backing volumes were verified absent at teardown.

## What the trial establishes

The proxy removed the bulk upstream reads for a warm input tree. At healthy Cloud speeds, its 9.292-second warm staging result was close to the 8.710-second direct control on the same host. The cold fill took more than twice the 8.628-second direct control on the other host. This is one fresh action per route and cache state. The warm full-tree sample stayed on the proxy host after another worker seeded it.

The earlier incident and loopback replay are documented separately. The two PR #18106 actions spent about 9m42s each in input fetch, and a recovered cold action spent 213 seconds fetching 55 KiB. This canary exercised an injected proxy fault, so those hosted retry incidents and their recurrence remain separate from the healthy timing comparison. [Earlier investigation](https://github.com/agentydragon/ducktape/pull/18335) · [First PR attempt](https://app.buildbuddy.io/invocation/3a0fd467-3e5f-5d50-b572-cf88338b1a09) · [Recovered cold execution](https://app.buildbuddy.io/invocation/03f68bb6-38ed-47be-b373-f1d7692d2d99)

The prior 62.5 MiB pod benchmark saved 0.37–0.40 seconds with a warm cross-node proxy and another 30–47 ms over loopback. It provides little reason to duplicate caches per host. The main open mechanism is preparing an action and recovering the proxy when Cloud metadata and authentication requests stall.
