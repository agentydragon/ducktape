# Evaluate BuildBuddy cache proxies for KVM workers

Deferred on 20 November 2026. [Issue #18365](https://github.com/agentydragon/ducktape/issues/18365) tracks the experiment and placement decision. The first trial uses an isolated pool with manual test actions. Required CI keeps its current route during the trial.

## Evidence and decision

On PR #18106, the VM and Firecracker actions on two workers each spent about 9m42s fetching the same four CAS blobs. A 62.5 MiB Action Service layer accounted for 99.989% of the bytes. A separate recovered cold attempt spent 213 seconds fetching 55 KiB. Executor logs recorded ByteStream read timeouts and successful retries. The receipts identify which files were fetched, and the retry messages have no per-blob timing. [CI latency report](https://agentydragon.github.io/ducktape/latency/#kvm-cas) · [First Bazel attempt](https://app.buildbuddy.io/invocation/3a0fd467-3e5f-5d50-b572-cf88338b1a09) · [Cold attempt](https://app.buildbuddy.io/invocation/03f68bb6-38ed-47be-b373-f1d7692d2d99)

The 20 November follow-up timed the same 62.5 MiB blob from pods pinned to the two KVM hosts in `hil-ovh`. Each median is from five checksum-verified pulls per route using the same `bb download` client and BuildBuddy Enterprise Cache Proxy `enterprise-v2.323.0`.

| Receiving node  | Direct Cloud | Warm proxy on other node | Warm proxy in same pod |
| --------------- | -----------: | -----------------------: | ---------------------: |
| `ovh-ns1001419` |      0.924 s |                  0.558 s |                0.511 s |
| `ovh-ns104952`  |      1.357 s |                  0.962 s |                0.932 s |

Both network routes received about 19 MiB per pull after compression. Each proxy recorded ten Zstd cache hits and zero upstream ByteStream reads during the timed phase. The healthy-window cross-node improvement was 0.37–0.40 seconds per blob. Loopback saved another 30–47 ms. The samples measured pods, with loopback as a lower bound for same-host delivery. Actual KVM guest latency and behavior during the retry tail remain open.

The decision is whether a shared cache can prevent repeated upstream reads often enough to matter, and whether node-local placement improves that result enough to justify additional storage, fills and maintenance. Compare direct Cloud first, then use one shared proxy as the control for the per-node design.

## Routing and storage

- Keep Cloud scheduling and cache authority. Change only `executor.cache_target` on canary workers. Their `app_target` stays on `grpcs://remote.buildbuddy.io:443`. Keep the existing 40 GB per-executor file cache and the OCI mirror. [Worker config](../kvm/worker/executor.yaml) · [BuildBuddy proxy docs](https://www.buildbuddy.io/docs/enterprise-proxy/)
- For the node-local variant, place one standalone Pebble proxy per eligible physical KVM host with a DaemonSet. Share an explicit host eligibility label with the canary VM pool alongside its KVM and storage constraints. Include the control-plane toleration where applicable. A ClusterIP Service on TCP 1985 selects the proxy pods and sets `internalTrafficPolicy: Local`.
- KubeVirt masquerades guest traffic through its `virt-launcher` pod. Cilium's kube-proxy replacement selects ready proxy endpoints on that pod's node. A healthy but empty local proxy can read through to Cloud. A host with no ready local proxy has no usable endpoint through this Service. [KVM VM pool](../../../cluster/cdk8s/buildbuddy_kvm_worker.py) · [Cluster network config](../../../cluster/terraform/main/cilium-values.yaml) · [Cilium internal traffic policy](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/#internal-traffic-policy)
- Extend worker egress to the proxy endpoints on TCP 1985. Restrict proxy ingress to the intended executor pods and allow the proxy to reach Cloud for cache RPCs and authentication. Each proxy owns an independent cache. Leave peer routing disabled in the node-local variant so local backend selection also means local data service on a hit.
- Start with a bounded disposable cache volume for the routing test. It survives worker VM turnover while the proxy pod lives. If keeping data through proxy pod replacement matters, test a dedicated host SSD directory and record capacity, ownership, eviction and recovery before choosing persistent storage.

## Tests before choosing a route

1. **Confirm the route from real guests.** Put one canary KVM guest on each of two eligible hosts. Resolve and contact the Service from inside each guest. Compare its host with the selected backend's EndpointSlice `nodeName` and corroborate the hit using that proxy's counters. Mark one proxy unready and verify its host loses the local endpoint while the other host still serves reads. Restore readiness and verify recovery.
2. **Measure matched direct, miss and hit paths.** Run the same source revision, target, image and input root through direct Cloud, a shared proxy and each node-local proxy. Force fresh TestRunner actions. Control the worker file-cache state separately from the proxy state. Fill the proxy from one guest, then try a fresh guest or replacement worker with an empty file cache. Run both cross-host directions for the shared control. Repeat with the large layer and a small-file-heavy input tree.
3. **Verify cache correctness and recovery.** Verify digests and counts after hits and misses. Exercise ByteStream, BatchReadBlobs, GetTree and the upload path used by the executor. Confirm completed action outputs and test artifacts can still be retrieved through Cloud. Test authentication on a newly started proxy as well as a warm one. Inject an upstream read failure on the isolated proxy path to check how hits, misses and authentication behave. Rehearse switching the canary's cache target back to Cloud and verify it can run a fresh action.
4. **Compare the action-level tail and cost.** Capture native input-fetch clocks, retries and failed preparations. Record file-cache state, proxy hits, upstream RPCs and bytes, plus disk, memory and network cost. Report sample counts and median and tail times with failures visible. Use checksum-verified single-blob results to explain transport, and actual TestRunner actions to judge CI relevance.

A shared proxy is the first candidate if it reduces the KVM retry tail at an acceptable operating cost. Prefer per-node proxies only if the guest comparison shows an additional routing, availability or cross-worker reuse benefit that justifies independent fills and storage. Keep direct Cloud if the canary shows little benefit under representative inputs. Any rollout would be a separate change after the failure and rollback tests.
