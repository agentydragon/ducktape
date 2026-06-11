# Home Assistant Notes

Instance: `https://home.e621.co.uk` (local: `10.0.0.3`) — location name: Howleroi (SF, America/Los_Angeles)
Version: 2026.3.4

## Access

- **Web UI**: `https://home.e621.co.uk`
- **API token**: SOPS secret `ha-token.sops.yaml` in `homeassistant-proxy` namespace
  - `kubectl get secret homeassistant-proxy-ha-token -n homeassistant-proxy -o jsonpath='{.data.token}' | base64 -d`
- **SSH**: `ssh homeassistant` → `root@10.0.0.3:22` (LAN only — Cloudflare blocks TCP/22 externally; use WireGuard when away)
  - Key: `~/.ssh/15leroy` (ED25519), SOPS-encrypted at `secrets/15leroy-homeassistant-ssh.yaml`
  - Auto-deployed on wyrm2 + rugged via `nix/home/modules/15leroy-ssh.nix` (home-manager sops)
  - SSH host alias defined in `nix/home/home.nix` (`programs.ssh.matchBlocks`)

## Household

| Person        | State (2026-04-01) |
| ------------- | ------------------ |
| Rai           | home               |
| dangered wolf | home               |
| Niko          | home               |
| Tesla         | home               |
| Victor        | home               |
| Auragon       | unknown/away       |

## Rai's Room

### Lights

- `light.rai_s_room_ceiling_lights` — ceiling group
- 9 individual LED rows (`light.left_row_*`, `light.middle_row_*`, `light.right_row_*`):
  left: closet, shelving, nook · middle: doorway, room center, nightstand · right: above desk, bed walkway, corner nightstand
- Govee: `light.room_led_strip`, `light.side_leds` (unavailable — Govee to MQTT Bridge addon in error)
- Adaptive Lighting: `switch.adaptive_lighting_rai_room_lights`

### Scenes & Automations

- Scenes: lights off, work lights, sleep mode + nook light
- Automations: remote on/off, switch toggle, sleep lights @ midnight, off @ 6 AM
- 3x webhook inbox automations (air quality/CO₂/state) — currently unavailable

### Sensors

- **Airthings**: CO₂, humidity, temp, VOCs, radon, pressure (`sensor.rai_s_room_*`)
- **Ecobee sensor**: motion, occupancy, temperature
- **Window sensors**: nook, room, hallway
- **Water leak detector**: hallway (`binary_sensor.rai_hallway_window_leak_detector_water_leak`)
- **Power switch**: `switch.rai_room_switch` (decoupled mode, measuring draw)
- **Pixel 6**: location + battery tracking

## House Infrastructure

- **Network**: UniFi (U7 Pro APs throughout), 694 tracked devices
- **Thermostat**: Ecobee, heat mode
- **Climate**: Sensibo in dangered's room; dehumidifier
- **Security**: Reolink video doorbell; window/door contact sensors throughout; Zigbee via ZHA + Zigbee2MQTT Edge
- **Other**: 3D printer (Moonraker/Klipper), TP-Link fans, Govee LEDs, ESPHome devices, Local Tuya, Tesla
- **Media**: bedroom display, den TV, room speaker, Google Home, Spotify, Chromecast
- **AI/Voice**: OpenAI + local Whisper/Piper/OpenWakeWord, LLM Vision
- **Data**: InfluxDB export, Opower energy

## Config-as-Code

SSH access is set up. See <config-as-code.md> for research notes on options.

## Addons

| Addon                       | Slug                        | Version        | State                                     |
| --------------------------- | --------------------------- | -------------- | ----------------------------------------- |
| Advanced SSH & Web Terminal | `a0d7b954_ssh`              | 23.0.4         | started                                   |
| Cloudflared                 | `9074a9fa_cloudflared`      | 7.0.5          | started                                   |
| ESPHome Device Builder      | `5c53de3b_esphome`          | 2026.3.1       | started                                   |
| File editor                 | `core_configurator`         | 5.8.0          | **error**                                 |
| Govee to MQTT Bridge        | `b9845f46_govee2mqtt`       | 2026.03.25     | **error** (explains offline Govee lights) |
| Grafana                     | `a0d7b954_grafana`          | 12.1.0         | started                                   |
| InfluxDB v2                 | `47c55538_influxdbv2`       | v0.0.4         | started                                   |
| Matter Server               | `core_matter_server`        | 8.3.0          | started                                   |
| Mosquitto broker            | `core_mosquitto`            | 6.5.2          | started                                   |
| MQTT Explorer               | `9cf1ea8f_mqtt_explorer`    | browser-1.0.3  | started                                   |
| Piper                       | `core_piper`                | 2.2.2          | started                                   |
| Terminal & SSH (core)       | `core_ssh`                  | 10.0.2         | **error** (superseded by Advanced SSH)    |
| Whisper                     | `core_whisper`              | 3.1.0          | started                                   |
| WireGuard (HOST NETWORK)    | `5e22ee3a_wireguard`        | dev-2025-05-23 | started                                   |
| Zigbee2MQTT                 | `45df7312_zigbee2mqtt`      | 2.9.1-1        | **error**                                 |
| Zigbee2MQTT Edge            | `45df7312_zigbee2mqtt_edge` | edge           | started (active)                          |
| openWakeWord                | `core_openwakeword`         | 2.1.0          | started                                   |

## Related

- `cluster/k8s/agents/homeassistant-proxy/` — MCP agent proxy (policy-controlled HA API access)
- `homeassistant/proxy/` — proxy source code
- `nix/home/modules/15leroy-ssh.nix` — SSH key deployment
