# Home Assistant TODO

- Migrate useful configuration from the legacy 15 Leroy instance into the Kubernetes Home
  Assistant deployment. Do not revive the retired proxy; Home Assistant now owns OIDC.

- Finish the basic Home Assistant deployment:
  - Make generated ConfigMap changes trigger a declarative Deployment rollout. The current
    stable ConfigMap name plus `subPath` mount requires a manual restart after config-only
    changes.
  - Confirm `agentydragon` receives the Home Assistant admin role through the
    `home-assistant-admins` Authentik group, including Companion app enrollment.
  - Configure the OptiPlex BIOS `AC Recovery` setting to `Power On`, then verify recovery
    with an actual power-loss test.
  - Exercise a restore from the SeaweedFS VolSync copy and add application-consistent Home
    Assistant backups rather than relying only on crash-consistent PVC copies.

- Add observability and long-term history:
  - Verify the Home Assistant ServiceMonitor scrape and build an initial Grafana dashboard.
  - Decide which entity metrics belong in Prometheus and their retention requirements.
  - Evaluate an external PostgreSQL recorder database for Home Assistant history.
  - Evaluate InfluxDB or another time-series store for richer, long-retention entity-state
    analysis and Grafana queries; avoid duplicating data without a defined use case.

- Add companion workloads after the basic deployment is stable:
  - Deploy Matter Server as a separately reviewed workload.
  - Deploy ESPHome Device Builder as a separately reviewed workload.

- Revisit deployment packaging:
  - Consider baking the pinned OIDC component into a derived Home Assistant image instead
    of downloading it during first-volume initialization.
  - Re-evaluate `przemekhys/homeassistant-operator` after its APIs leave alpha and it can
    express node placement, pod customization, and custom-component lifecycle.

- Wire in remaining devices for Rai's room:
  - Lights, light switch with relay
  - Air quality sensor
  - Presence detector
  - Desk power switch (WiFi)
  - Window open/closed detectors
