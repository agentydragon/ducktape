@README.md

- No runtime instructions here; they belong in `haku-state`'s root cards and hubs, which Haku owns.
- Edit `agent_shared.yaml` first, then **both** <anthropic_hosted/terraform/main.tf> and
  <self_hosted/haku.agent.yaml>, or `//haku/runtime/x/managed_agent:test_agent_config_ssot` fails.
  Per-surface by design (do not hoist): each agent's `name`, `system` prose, `environment`, and
  vault wiring.
- Resuming provisioning or Flux reconciliation needs an explicit reactivation decision and fresh
  Anthropic resources.
