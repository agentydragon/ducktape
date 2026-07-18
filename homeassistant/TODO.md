# Home Assistant TODO

- Migrate useful configuration from the legacy 15 Leroy instance into the Kubernetes Home
  Assistant deployment. Do not revive the retired proxy; Home Assistant now owns OIDC.

- After the basic Home Assistant deployment is stable:
  - Deploy Matter Server as a separately reviewed workload.
  - Deploy ESPHome Device Builder as a separately reviewed workload.
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
