# Platform Sessions remote deployment

The `platform-sessions-remote` Flux Kustomization runs the gateway and the OpenAI
Platform, Anthropic, ChatGPT, Blue Shield, Quest, and Thrive providers in separate
Deployments. Its Flux dependency, `platform-sessions`, owns the namespace, legacy
PVCs, caller bearer Secrets, and public Service and route pointing to the gateway.
Preserve the PVCs and bearer Secret identities when changing ownership. OpenAI
Platform, Anthropic, and Thrive share a private image but run as separate processes. Every provider has
its own TLS identity, internal bearers, PVC, and metrics scrape. The Go gateway does
not expose gateway-wide Prometheus metrics.

Each provider's state PVC uses `piraeus-ssd-2-autodiskful` and its capture PVC
`piraeus-hdd-local`; both stay unbound until a pod consumes them.
The remote Kustomization does not wait for resource health, so its readiness only
means the manifests were applied. The PVCs are protected from Flux pruning, and the
two Kustomizations use `deletionPolicy: Orphan`. Providers run in zone `hil-ovh`.

## Upstream traffic recording

`RECORD_UPSTREAM_TRAFFIC` in `cluster/cdk8s/platform_sessions/config.py` enables a
local mitmproxy sidecar for providers other than MyQuest. Production MyQuest
connects directly. Its comparison instance has an independent
`MYQUEST_CANARY_RECORD_UPSTREAM_TRAFFIC` switch. The general switch is enabled for a
session-rotation soak through at least January 2027. Each sidecar writes full, unredacted upstream
exchanges to daily `.mitm` files on its own `piraeus-hdd-local` PVC. These files
include session credentials and can include private financial, medical and
conversation data; access them as secrets. A single HDD copy is not a backup.

Disabling recording for a provider removes its proxy routing and sidecar and retains
its capture PVC. The sidecar appends to the day's file after a restart. A failed capture write
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
