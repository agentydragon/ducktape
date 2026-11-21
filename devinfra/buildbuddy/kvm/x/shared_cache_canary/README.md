# Shared BuildBuddy cache trial on KVM

[Issue #18365](https://github.com/agentydragon/ducktape/issues/18365) · [Decision plan](../../../plans/node_local_cache.md)

This is a manual trial. The renderer takes the current worker image, guest config, KVM affinity, mirror and existing Secret references from the checked-in production manifest. It emits an independent VMPool at zero replicas, one shared proxy on the first chosen host, a ClusterIP Service and scoped Cilium policies. The pool registers as `kvm-vm-cache-canary`. Its labels exclude it from production routing and metrics, and its work disks have canary-specific names. Each 80 GiB work disk goes with its VM. The proxy uses a disposable 12 GiB local SSD claim owned by its pod, with an 8 GiB Pebble limit. It stays warm across canary VM replacement if the proxy pod keeps running.

## Prepare the two hosts

Choose two distinct KVM-capable hosts in the worker's placement region, with spare CPU, RAM and local SSD capacity for both workers and the proxy. Prebuild the manual target before measuring. Use the same revision for both routes. The first hostname is where the shared proxy runs.

```bash
scratch="${TMPDIR:-/tmp}/agents/YYYY_MM_DD/kvm-shared-cache"
mkdir -p "$scratch"
proxy_host=ovh-ns1001419
second_host=ovh-ns104952

for route in direct shared; do
  bb run //devinfra/buildbuddy/kvm/x/shared_cache_canary:render -- \
    --hosts "$proxy_host" "$second_host" --route "$route" \
    --output "$scratch/$route.yaml"
  kubectl apply --dry-run=server -f "$scratch/$route.yaml"
done

target=//agentplane/system_tests/x/firecracker:test_firecracker_shared_cache_canary
bbr build "$target" --remote_build_event_upload=all
```

## Compare direct and shared cache routes

Apply `direct.yaml` first, then scale only the trial pool to two. Confirm one ready worker on each requested host, both registered against Cloud, and a ready proxy Service. Capture the VM and executor IDs and the chosen proxy pod UID.

```bash
namespace=buildbuddy-executor
pool=buildbuddy-kvm-cache-canary
proxy=buildbuddy-kvm-cache-proxy
kubectl apply -f "$scratch/direct.yaml"
kubectl -n "$namespace" scale vmpool/"$pool" --replicas=2
kubectl -n "$namespace" rollout status deploy/"$proxy" --timeout=5m
kubectl -n "$namespace" get vm,vmi,pod -l app.kubernetes.io/part-of="$pool" -o wide
```

Run the same manual test label for each route and verify a fresh physical TestRunner, exact worker and input root in the native receipt. Run from both hosts. If needed, pin the registered executor ID with `--remote_exec_header=x-buildbuddy-platform.debug-executor-id=ID` after prebuilding. The header affects all remote actions, so a run with intervening build work is a poor control. Download the test XML and an undeclared output through BuildBuddy after the run to exercise the upload path.

```bash
bbr test "$target" --nocache_test_results --flaky_test_attempts=1 \
  --remote_build_event_upload=all --test_output=errors
```

Wait for both trial workers to go idle before changing routes. Scale to zero, wait for the trial VMIs and work claims to disappear, apply `shared.yaml`, and scale back to two. The guest reads its cache target at boot. Use the same action to fill the shared proxy from one worker, then consume from the other worker's fresh file cache. Keep the proxy pod through the switch and verify its identity and counters. If the unintended worker was selected or its cache was already warm, record that run separately. A repeat on a worker's own file cache is a different condition.

```bash
kubectl -n "$namespace" scale vmpool/"$pool" --replicas=0
kubectl -n "$namespace" wait --for=delete vmi \
  -l app.kubernetes.io/part-of="$pool" --timeout=5m
kubectl -n "$namespace" get pvc
# Verify the trial work claims are gone before starting fresh guests.
kubectl apply -f "$scratch/shared.yaml"
kubectl -n "$namespace" scale vmpool/"$pool" --replicas=2
```

Capture proxy metrics by port-forwarding the trial deployment's port 9090. Compare cache hits, misses and upstream RPCs before and after each action. Record native input-fetch clocks, retries, bytes, errors, and result and artifact checks. Keep cold proxy fills, cross-worker warm hits and worker file-cache hits distinct.

## Exercise upstream failure and restore direct Cloud

On the shared route, prime the proxy and save its counters. Repeat the scale-to-zero cycle with `shared.yaml` to start fresh worker disks while keeping the warm proxy pod. In an idle trial window, block outbound TCP 443 to the world **only for the proxy**. The worker's Cloud scheduler connection uses its own policy. Run the same target to exercise warm proxy reads from a fresh guest, and verify the denial and the proxy hit counters. Try a prebuilt action with a different input that the proxy has never fetched to exercise a cold miss. Restarting the trial proxy under the deny gives a separate cold-start check with an empty store and no cached authentication. Keep the fault window bounded and cancel stalled trial invocations with `bbapi invocation cancel` before draining the pool. Remove the deny after each check.

```bash
kubectl -n "$namespace" patch cnp "$proxy" --type=merge \
  -p '{"spec":{"egressDeny":[{"toEntities":["world"],"toPorts":[{"ports":[{"port":"443","protocol":"TCP"}]}]}]}}'
# Run the bounded probes and capture counters.
kubectl -n "$namespace" patch cnp "$proxy" --type=merge -p '{"spec":{"egressDeny":null}}'
```

For rollback, scale the trial pool to zero and wait for its VMIs and work claims to disappear again. Apply the original `direct.yaml`, scale to two, and verify a fresh action passes with no corresponding proxy reads. Once idle, scale to zero and delete the resources from `direct.yaml`. Confirm the trial work claims and ephemeral proxy claim disappear. The rendered set only references the existing worker Secrets, so deleting it leaves their owners in place.
