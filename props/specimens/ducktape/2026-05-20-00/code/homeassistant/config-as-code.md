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

## Sync Tools

### Official git_pull addon (`core_git_pull`)

Built into HAOS, no extra install. Clones/pulls a git repo into `/config` on
a schedule or when triggered. Supports SSH deploy keys.

- No built-in webhook — trigger via HA automation + `hassio.addon_restart`
- `auto_restart: true` does a full HA restart after pull (no selective reload)
- **Known data loss risk**: if misconfigured (e.g., empty repo), can wipe
  `/config`. Issue [#1690](https://github.com/home-assistant/addons/issues/1690)
  was closed stale without conclusive fix. dfederm.com explicitly recommends
  against it.

### git-ha-ppens (HACS custom integration)

[github.com/manuveli/git-ha-ppens](https://github.com/manuveli/git-ha-ppens).
Custom integration (not an addon), installed via HACS. Exposes services:
`pull`, `push`, `commit`, `sync`, `diff`. Auto-generates `.gitignore`.
Optional AI commit messages via HA conversation agent.

- Operates on `/config` as a whole git repo — no include-paths or subtree support
- No support for pointing at a subdirectory of a remote (no monorepo support)
- Partial sync possible only via `.gitignore` hacks (`*` + `!path/to/keep`)
- Lower risk than the official addon; newer (2024); 55 stars
- Config is UI-only (no YAML)

### frenck/action-home-assistant (GitHub Action)

[github.com/frenck/action-home-assistant](https://github.com/frenck/action-home-assistant).
Runs `hass --script check_config` in CI to validate YAML before it reaches HA.
Use with a `fakesecrets.yaml` (dummy values for all `!secret` references).
Cannot validate config_flow integrations (UI-only) or custom components with
external deps.

### Roll-your-own (shell_command + webhook automation)

The pattern most commonly used when you want git as source of truth:

```yaml
shell_command:
  git_pull: git -C /root/ducktape pull origin devel

automation:
  - trigger:
      platform: webhook
      webhook_id: !secret deploy_webhook_id
    action:
      - service: shell_command.git_pull
      - service: homeassistant.reload_all
```

Trigger from CI/CD: `curl -X POST https://home.e621.co.uk/api/webhook/<id>`
after `git push`. No extra addons needed.

## Monorepo / Subtree Considerations

git-ha-ppens and the official addon both treat `/config` as the git root.
Neither supports syncing from a subdirectory of an external repo.

Options for keeping HA packages inside a monorepo (e.g., ducktape):

**A. Clone + symlink (simplest)**

```
/root/ducktape          ← clone of this repo
/config/packages/rai   → symlink → /root/ducktape/homeassistant/packages/rai
```

Shell command pulls ducktape, symlink keeps HA pointed at the right files.
No extra tooling. Easy to reason about.

**B. Git sparse checkout**

Clone ducktape with `--sparse`, then `git sparse-checkout set homeassistant/packages/`.
Only that subtree is on disk. More complex, same end result as A.

**C. Separate repo for HA packages**

Split `homeassistant/packages/` into its own repo. Then git-ha-ppens or the
official addon work naturally. Downside: HA config lives separate from the
rest of infrastructure.

## What Would Go in Rai's Room Package

Based on current room state, a `packages/rai/` structure:

| File               | Contents                                                                             |
| ------------------ | ------------------------------------------------------------------------------------ |
| `lights.yaml`      | Light groups (YAML-defined), adaptive lighting config                                |
| `scenes.yaml`      | Work lights, sleep mode, nook light, lights off                                      |
| `automations.yaml` | Sleep lights @ midnight, off @ 6 AM, remote/switch toggle, webhook inbox automations |
| `alerts.yaml`      | Water leak detector alert, low battery notifications                                 |
| `dashboard.yaml`   | Lovelace panel for the room (if migrated to YAML mode)                               |

Physical device entities (Airthings, Ecobee sensor, Zigbee devices, Pixel 6)
are integration-managed and cannot go in packages.

## Open Questions

- Use this monorepo (ducktape) or a separate HA-only repo?
- Which sync mechanism: clone+symlink, git-ha-ppens, or official addon?
- Migrate Lovelace dashboard to YAML mode or leave it UI-managed?
- Pull existing automations/scenes out of HA first to seed the package files?
