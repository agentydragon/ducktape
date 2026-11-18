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

## Upstream traffic recording

`RECORD_UPSTREAM_TRAFFIC` in `cluster/cdk8s/platform_sessions/config.py` enables a
local mitmproxy sidecar for every provider. It is enabled for a session-rotation
soak through at least January 2027. Each sidecar writes full, unredacted upstream
exchanges to daily `.mitm` files on its own `piraeus-hdd-local` PVC. These files
include session credentials and can include private financial, medical and
conversation data; access them as secrets. A single HDD copy is not a backup.

Disabling the toggle removes proxy routing and sidecars while retaining the capture
PVCs. The sidecar appends to the day's file after a restart. A failed capture write
stops mitmproxy and interrupts upstream requests until recording is restored.
TODO: implement retention and deletion of old capture files. There is currently
no automatic cleanup; monitor PVC capacity throughout the soak.

## Legacy service

The legacy `platform-sessions` PVC is retained, but no workload mounts it and its
logins are not transferred. Import or reauthenticate each provider through the owner
frontend.

Keep the original PVC intact. Do not start the legacy service against a session that a
new provider is using: two processes can otherwise renew or use the same upstream
credentials concurrently.
