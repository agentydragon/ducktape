# Platform sessions Flux handover

The cdk8s-generated `ducktape-flux/platform-sessions` Kustomization starts
suspended. The live `flux-system/platform-sessions` Kustomization and the
`gaffer-private` bridge still own the existing objects. Do not enable both
workload owners at once.

1. Merge gaffer-private PR #822 and wait for the private Flux source and its
   Kustomizations to apply it. Verify `flux-system/platform-sessions` has
   `spec.prune: false` and `spec.deletionPolicy: Orphan`; verify the namespace
   and both `flux-system/platform-sessions` image objects carry
   `kustomize.toolkit.fluxcd.io/prune: disabled`.
2. Record the UIDs of both PVCs, the platform-sessions namespace, the three
   bearer ExternalSecrets and their generated Secrets, the deployment and
   service, and the image policy and repository. Record the current service
   EndpointSlice and readiness. Do not record bearer values.
3. Suspend the old workload Kustomization and recheck its prune and deletion
   policies. Remove the protected namespace and image objects from the private
   bridge and image-automation inventories, respectively, and wait for that
   revision to reconcile. Confirm the protected objects and UIDs still exist.
4. In a follow-up Ducktape change, enable the new Kustomization. Reconcile it
   and verify the recorded resource UIDs are unchanged, the bearer Secrets
   still match their reflected copies, the Deployment is ready, and the
   EndpointSlice has a ready backend. Check owner and route-limited access and
   read-only provider requests. Do not issue a Quest mutation to test the move.
5. Remove the old suspended private Kustomization and declarations only after
   the new owner is healthy. Verify the old inventory cannot prune the adopted
   resources. Remove the temporary prune-disabled annotations after the old
   owners no longer manage those objects.

The image policy selects the private repository's `main-*` image tags; the
Ducktape image automation updates the `platform-sessions-image-pins` Component.
The unused old read-token generator is not added to the new chart.
