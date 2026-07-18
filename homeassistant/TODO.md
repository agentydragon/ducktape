# Home Assistant TODO

- Complete operational hardening:
  - Configure the OptiPlex BIOS `AC Recovery` setting to `Power On`, then verify recovery
    with an actual power-loss test.
  - Perform and document a restore drill from the SeaweedFS VolSync copy, including the
    achieved recovery time and data-loss window.
  - Add application-consistent Home Assistant backups and replicate their artifacts off the
    OptiPlex; retain VolSync as the crash-consistent PVC copy rather than treating it as the
    only restore point.
  - Make ConfigMap-driven Deployment restarts explicit and deterministic. Reloader restarted
    Home Assistant for the onboarding rollout, but the earlier OIDC-only update required a
    manual restart.
  - Enroll the Companion app and verify its OIDC callback and persistent session behavior.

- Migrate useful configuration and devices from the legacy 15 Leroy instance. Do not revive
  the retired proxy; Home Assistant now owns OIDC.
  - Inventory existing integrations, automations, dashboards, and device identifiers before
    moving configuration so stale state is not copied blindly.
  - Wire in the remaining devices for Rai's room:
    - Lights and light-switch relay
    - Air-quality sensor
    - Presence detector
    - Desk power switch (Wi-Fi)
    - Window open/closed detectors

- Add observability and long-term history:
  - Verify the existing ServiceMonitor target is healthy, then build an initial Grafana
    dashboard for availability, request behavior, entity counts, and automation failures.
  - Define the queries and retention requirements before choosing storage. Keep operational
    metrics in Prometheus and avoid exporting high-cardinality entity history by default.
  - Evaluate external PostgreSQL as the primary recorder database for durable Home Assistant
    history and easier recovery from loss of the local PVC.
  - Add InfluxDB or another time-series store only if concrete long-retention entity-state
    analysis or Grafana queries are not served well by the recorder database.

- Add companion workloads after the basic deployment is stable:
  - Deploy Matter Server as a separately reviewed workload.
  - Deploy ESPHome Device Builder as a separately reviewed workload.

- Revisit deployment packaging:
  - Consider baking the pinned OIDC component into a derived Home Assistant image instead
    of downloading it during first-volume initialization.
  - Re-evaluate `przemekhys/homeassistant-operator` after its APIs leave alpha and it can
    express node placement, pod customization, and custom-component lifecycle.
