# Remote platform sessions staging and handoff

The `platform-sessions-remote` Flux Kustomization prepares a gateway and a Blue Shield
provider in separate Deployments. Both have zero replicas and the gateway Service has no
public route. The running `platform-sessions` service still owns every real session. The
remote gateway is configured for Blue Shield only, so routing the existing public Service
to it now would break every other integration.

The Blue Shield PVC uses `seaweedfs-ovh-ssd` and stays unbound until a pod consumes it.
The remote Kustomization does not wait for resource health for this reason; its readiness
only means the manifests were applied. The PVC is protected from Flux pruning, and the
Kustomization uses `deletionPolicy: Orphan`. The provider runs in zone `hil-ovh`; before
moving a live session, establish whether its upstream will accept the change from the
current `wyrm2` network origin.

Before starting either Deployment, verify that the private provider image was published
from gaffer-private, both remote image pins were filled by Flux with compatible gateway
and provider revisions, and both certificates and all three internal bearer Secrets are
ready. The gateway image and provider image are published by different repositories. The
`unset` image tags are placeholders, not runnable versions.

For an isolated smoke test, start the provider with the new, empty PVC and the gateway
without changing the public Service. Check HTTPS and gRPC authentication, readiness, the
provider's OpenAPI document, and Prometheus scraping. An empty provider must not receive a
copy of the live session for this test. Stop it and verify its process has exited before
placing real session data on its volume.

A live session handoff needs a separate reviewed change and a tested transfer procedure:

1. Preserve the legacy PVC and disable Blue Shield in the legacy configuration. Verify the
   previous process has stopped, the replacement legacy pod has Blue Shield disabled, and
   no process can probe or use the old session. The PVC's `ReadWriteOnce` mode does not
   fence another process on the same node.
2. Copy the entire Blue Shield state directory, including uncertainty markers and metadata,
   to the new PVC without logging session contents. The old local PVC and new OVH PVC have
   different placement constraints; test the transfer and its failure recovery before using
   it on live data. Preserve the original until the handoff is verified.
3. Ensure the transfer pod has exited, then start exactly one provider. Verify session state,
   logs, scrape results and a safe read through the private gateway. Treat an interrupted
   credential exchange as an unknown outcome, not permission to retry it.
4. Keep the public Service on the legacy deployment until **every enabled integration** has
   a remote provider and has completed an ownership handoff. Then update the gateway's
   configuration, image and Service routing together; verify owner login, existing scoped
   bearers and a safe read for each provider. Verify EndpointSlices and consumer access.

Do not run both session owners against the same upstream session, even when they use separate
copies of its files. A successful probe does not prove that Blue Shield will allow the session
to remain active indefinitely.
