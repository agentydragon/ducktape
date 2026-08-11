@README.md

This directory is deploy config, not Haku's runtime manual — that moved to `haku-state`. Do not
add runtime instructions here; they belong in `haku-state`'s root cards and hubs, which Haku owns
and writes.

## Editing rules

- Edit `agent_shared.yaml` first, then update **both** surfaces it governs
  (<../../tf/gitops/haku-cloud-agent/main.tf> and
  <../runtime/managed_agent/self_hosted/haku.agent.yaml>), or `test_agent_config_ssot.py` fails.
- What deliberately stays per-surface: each agent's `name`, its `system` bootstrap prose, its
  `environment`, and how it reaches the shared vault. Do not hoist those here.
