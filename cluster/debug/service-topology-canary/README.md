# Service topology canary

Temporary, explicitly applied diagnostic manifests for #9605. These are outside
Flux's paths. `node-config.yaml` enables topology only on `ovh-ns103711` (`hil-ovh`)
and `optiplex` (`home-lan`). The initial canary ran with the global setting
disabled; the subsequent cluster-wide Helm setting is managed in
`cluster/terraform/main/cilium-values.yaml`. `sample.yaml` creates three disposable
HTTP backends/clients; `ovh-ns102453` is the unmodified control.
No application Service or CoreDNS placement changes.

Cilium 1.19.8 recognizes `trafficDistribution: PreferClose`. Node-config changes
require recreating the selected Cilium agent Pods; apply alone does not activate
or undo the override. See [Cilium's versioned per-node configuration](https://github.com/cilium/cilium/blob/v1.19.8/Documentation/configuration/per-node-config.rst).

## Procedure

1. Audit existing `CiliumNodeConfig` objects and Services with topology annotations
   or `trafficDistribution`; check node readiness and zone labels.
2. Apply `sample.yaml`; wait for all three Pods Ready. Use the sample Service's
   ClusterIP to avoid making DNS health part of the routing measurement. Make
   40 fresh HTTP connections from each sample Pod and count the response labels.
3. Apply `node-config.yaml`. Recreate only the Cilium agent on `ovh-ns103711`, wait
   for its replacement Ready, verify its effective config and node health; repeat
   for `optiplex`. Never delete both agents simultaneously.
4. Inspect the sample's EndpointSlices and Cilium's backend selection. Repeat the
   HTTP counts: OVH should select OVH backends, home should select `home`, and the
   control should still select across zones.
5. Change only the sample's `backend` Pod labels to remove all same-zone backends
   from its selector. Wait for EndpointSlice propagation and verify cross-zone
   fallback using fresh connections. Restore labels; verify locality returns.
   Check both directions. Treat the known home WAN flakiness (#9604) separately
   from the backend eligibility shown by Cilium.

## Rollback and cleanup

Before global enablement, delete `node-config.yaml`, recreate each canary agent
sequentially and confirm `enable-service-topology` returns to false. Once the
global setting is enabled, follow the rollback procedure in
`cluster/docs/network.md` instead. Delete `sample.yaml` after probes; the sample
namespace has no persistent storage or application data.

After canary acceptance, apply the Terraform-owned Helm values and follow
`cluster/docs/network.md` to roll remaining connected agents. Remove this node
override once they all use the global value. Opt production Services in
individually. Retain the OVH CoreDNS restriction until a separate DNS design
satisfies OVH-only resilience. Remove these diagnostic manifests when that rollout
is complete; retain durable operating instructions in `cluster/docs/network.md`.
