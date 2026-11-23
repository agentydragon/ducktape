# Home Assistant Config-as-Code: Options

## The Fundamental Split

Not everything in HA can be managed as YAML. Since ADR-0010 (~2020), hardware
integrations use config_flow and store their state in `.storage/`, not in YAML.
`.storage/` contains auth tokens, device/entity registries, and integration
credentials — it should not be committed to git.

**What can live in YAML/git:**

- `automation:`, `script:`, `scene:` — fully supported, hot-reloadable
- `template:` sensors/binary sensors/switches
- Helpers defined in YAML: `input_boolean:`, `input_select:`, `input_number:`,
  `input_text:`, `input_datetime:`
- `group:`, `alert:`, `notify:` platforms
- `recorder:`/`logbook:` filters, `homeassistant:` customizations
- Lovelace dashboards (if in YAML storage mode, not the default UI storage mode)

**What must stay in `.storage/` (UI-managed only):**

- Hardware integration credentials and config (ZHA, Ecobee, Airthings, ESPHome,
  MQTT broker, Google Home, Tesla, etc.)
- Device registry and entity registry (names, area assignments done via UI)
- Helpers created via the UI (they go to `.storage/input_boolean` etc.)
- Addon configurations

Key implication: helpers must be defined in YAML from the start if you want
them git-tracked. A helper created in the UI cannot be migrated to YAML without
deleting and recreating it.

## HA Packages Feature

Built-in. Lets you split config across files by feature rather than domain:

```yaml
# configuration.yaml
homeassistant:
  packages: !include_dir_named packages/
```

Each file under `packages/` becomes a package named after the file. A single
package file can contain multiple domains — e.g., `packages/rai_room.yaml`
holds `automation:`, `scene:`, `input_boolean:`, `template:` all for that room.

This is the standard community pattern for partial config-as-code. Reference
configs to look at: [frenck/home-assistant-config](https://github.com/frenck/home-assistant-config),
[basnijholt/home-assistant-config](https://github.com/basnijholt/home-assistant-config).

Hot reload without restart: `homeassistant.reload_all` reloads automations,
scripts, scenes, groups, helpers, and templates. Full restart only needed for
`http:`, `logger:`, or new custom components.

## Sync options (surveyed March 2026, not pursued)

The Kubernetes deployment (`cluster/k8s/home-assistant`) syncs no repository into `/config`: it mounts
`configuration.yaml` from a ConfigMap and installs custom components with an init container.

- **Official `core_git_pull` addon**: clones into `/config` on a schedule or trigger. No webhook,
  a full restart after every pull (`auto_restart`), and a known risk of wiping `/config` when
  misconfigured (home-assistant/addons#1690, closed stale without a fix).
- **git-ha-ppens (HACS)**: treats `/config` as the whole repository, so it cannot follow a subdirectory
  of a monorepo.
- **`frenck/action-home-assistant`**: runs `hass --script check_config` in CI against a
  `fakesecrets.yaml`. It cannot validate UI-only integrations or custom components with external
  dependencies.
- **Roll your own**: a webhook automation that runs a `shell_command` `git pull`, then
  `homeassistant.reload_all`.
- **Monorepo layouts**: a clone with `/config/packages/<name>` symlinked into it was the simplest;
  sparse checkout or a separate repository for the packages were the alternatives.
