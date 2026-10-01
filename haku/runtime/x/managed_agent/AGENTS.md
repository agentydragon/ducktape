@README.md

Do not add runtime instructions here; they belong in `haku-state`'s root cards and hubs, which
Haku owns and writes.

## Editing rules

- Edit `agent_shared.yaml` first, then update **both** surfaces it governs
  (<anthropic_hosted/terraform/main.tf> and <self_hosted/haku.agent.yaml>), or
  `//haku/runtime/x/managed_agent:test_agent_config_ssot` fails.
- What deliberately stays per-surface: each agent's `name`, its `system` bootstrap prose, its
  `environment`, and how it reaches a newly provisioned vault. Do not hoist those here.
- Both runtimes are parked. Changes that would resume provisioning or Flux reconciliation need
  an explicit reactivation decision and fresh Anthropic resources.
