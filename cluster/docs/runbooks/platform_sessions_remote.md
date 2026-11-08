# Platform Sessions remote deployment

The `platform-sessions-remote` Flux Kustomization runs the gateway and the OpenAI
Platform, Anthropic, ChatGPT, Blue Shield, Quest, and Thrive providers in separate
Deployments. The existing `platform-sessions` Service and public route point to the
gateway; existing caller bearer Secrets remain in place. OpenAI Platform, Anthropic,
and Thrive share a private image but run as separate processes. Every provider has
its own TLS identity, internal bearers, PVC, and metrics scrape. The Go gateway does
not expose gateway-wide Prometheus metrics.

The provider PVCs use `seaweedfs-ovh-ssd` and stay unbound until a pod consumes them.
The remote Kustomization does not wait for resource health, so its readiness only
means the manifests were applied. The PVCs are protected from Flux pruning, and the
Kustomization uses `deletionPolicy: Orphan`. Providers run in zone `hil-ovh`.

## Cutover from the legacy service

The new providers start with empty state. Existing upstream logins are not transferred;
after cutover, import or reauthenticate each provider through the owner frontend.
The legacy PVC is retained but no longer mounted by a workload. The cutover removes
the old Deployment and redirects its Service, so requests can fail until the gateway
and providers are ready and sessions have been reestablished.

Before merging the cutover, verify that all five gateway/provider image pins in
`cluster/k8s/platform-sessions-remote-image-pins/kustomization.yaml` name published,
compatible images. The `unset` values are placeholders, not runnable tags. The
gateway image and provider images are published by different repositories.

After reconciliation, verify that the old Deployment has terminated, the existing
Service has ready gateway EndpointSlices, the gateway and provider Deployments are
ready, their Secrets and certificates exist, and provider metrics are scraped. Sign in
through the public owner frontend, import new sessions, and check owner and scoped
bearers with a safe read for each provider. Verify the affected consumers, including
the Agentplane egress credential service. Do not issue a Quest mutation to test the
cutover.

Keep the original PVC intact. Do not start the legacy service against a session that a
new provider is using: two processes can otherwise renew or use the same upstream
credentials concurrently.
