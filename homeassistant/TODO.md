# Home Assistant TODO

- **15 Leroy household left (2026-06-27).** This directory is dormant. The proxy
  (`cluster/k8s/agents/homeassistant-proxy/` + `homeassistant/proxy/`) is parked: manifests
  kept + Flux Kustomization suspended, live objects deleted from cluster. Either revive on a
  new HA host in the new place — re-add the `~/.ssh/15leroy` key + `nix/home/modules/15leroy-ssh.nix`
  module + `homeassistant` SSH alias in `nix/home/home.nix`, re-point `deploy.sh` /
  `iaqi/deploy_iaqi.sh`, and unsuspend the proxy Kustomization — or delete this directory
  and the proxy.

- Wire in remaining devices for Rai's room:
  - Lights, light switch with relay
  - Air quality sensor
  - Presence detector
  - Desk power switch (WiFi)
  - Window open/closed detectors
