# Kyverno GeneratingPolicy: partial record generation

## Problem

A `GeneratingPolicy` (policies.kyverno.io/v1) generates 6 resources from a
single policy (4 ClusterRRsets + 1 ConfigMap). After a node trigger event:

- `wildcard` ClusterRRset: **persists**
- `ns2` ClusterRRset: **persists**
- `cluster-info` ConfigMap: **persists**
- `apex` ClusterRRset: **created then deleted within seconds**
- `ns1` ClusterRRset: **created then deleted within seconds**

Background controller logs confirm all resources are processed:

```
processing generated resource gvr="dns.cav.enablers.ob/v1alpha2, Resource=clusterrrsets" name=wildcard
processing generated resource gvr="dns.cav.enablers.ob/v1alpha2, Resource=clusterrrsets" name=apex
Resource added name=apex
processing generated resource gvr="dns.cav.enablers.ob/v1alpha2, Resource=clusterrrsets" name=ns1
processing generated resource gvr="dns.cav.enablers.ob/v1alpha2, Resource=clusterrrsets" name=ns2
processing generated resource gvr="/v1, Resource=configmaps" name=cluster-info
```

No errors in any controller log. The resources appear briefly then vanish.

## Environment

- Kyverno 3.7.1 (Helm chart 3.7.1)
- GeneratingPolicy API: `policies.kyverno.io/v1`
- `evaluation.synchronize.enabled: true`
- `evaluation.generateExisting.enabled: true`
- Target resources: `ClusterRRset` (cluster-scoped CRD from powerdns-operator)

## Policy structure

One `generate` expression creates all resources:

```yaml
generate:
  - expression: >-
      generator.Apply("", [
        variables.wildcard, variables.apex,
        variables.ns1, variables.ns2
      ])
  - expression: >-
      generator.Apply("kube-system", [variables.clusterInfo])
```

Each variable builds a resource using `dyn()` wrappers (required because
Kyverno's CEL environment doesn't register CRD types).

## What survives vs what doesn't

| Resource     | Name in metadata | Survives | Notes                             |
| ------------ | ---------------- | -------- | --------------------------------- |
| ClusterRRset | `wildcard`       | Yes      |                                   |
| ClusterRRset | `ns2`            | Yes      |                                   |
| ConfigMap    | `cluster-info`   | Yes      | Different namespace (kube-system) |
| ClusterRRset | `apex`           | No       | Created then deleted              |
| ClusterRRset | `ns1`            | No       | Created then deleted              |

No obvious pattern — all 4 ClusterRRsets use identical `dyn()` construction,
same API group, same kind. The variable names, resource names, and content
differ but the structure is the same.

## Observations

- Manually created `apex` and `ns1` (via `kubectl apply`) persist indefinitely
  — Kyverno does not delete them.
- The issue only occurs during Kyverno's own generation cycle.
- No `UpdateRequest` resources are created for the failing records.
- No errors in admission-controller or background-controller logs.
- The `synchronize` watcher logs show the resources being processed but doesn't
  explain why they disappear.

## Workaround

Manually create `apex` and `ns1` ClusterRRsets via `kubectl apply`. They persist
because they lack Kyverno ownership labels, so the synchronize logic ignores them.
This defeats the auto-update purpose of the policy for those 2 records.

## Possible causes

1. **Race condition in multi-resource generation**: The 4 ClusterRRsets are created
   in a single `generator.Apply()` call. If the synchronize watcher reconciles
   mid-creation, it might see an incomplete set and prune "extra" resources.

2. **Trigger-resource association**: Each generated resource is tagged with the
   trigger node that caused it. With `synchronize: true`, if a different node
   event re-triggers the policy, the watcher might delete resources associated
   with the previous trigger before creating new ones — and if creation of some
   resources fails in the new cycle, they're lost.

3. **CEL evaluation non-determinism**: The `cpIPsSorted` variable might evaluate
   differently on different triggers (e.g., sort order changes), causing Kyverno
   to see the `ns1`/`apex` resources as "changed" and delete-then-recreate them,
   with the recreation failing silently.

## Next steps

- Try splitting into separate GeneratingPolicies (one per record) to isolate
  which resources are problematic.
- Try without `synchronize: true` to see if the deletion is from the sync watcher.
- Check if this is a known Kyverno issue with cluster-scoped generate targets.
- Consider filing upstream: <https://github.com/kyverno/kyverno/issues>
