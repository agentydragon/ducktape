# k8s manifests — agent instructions

## Network troubleshooting: always check CiliumNetworkPolicies

CiliumNetworkPolicies in this cluster restrict some traffic.
If pods can't talk to each other (memberlist won't form, gRPC clients timeout, intra-app probes fail, ...)
check CiliumNetworkPolicies - they may need widening for required traffic.
