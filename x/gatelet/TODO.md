# Gatelet TODO

## Home Assistant Integration

- [ ] Continuous sensor history views: regularly spaced samples with units, space-efficient tables
- [ ] Group entities by HA area; display by friendly name
- [ ] Combined continuous sensor table on dashboard (avoid repeating datetime columns)
- [ ] Dashboard action buttons for whitelisted HA automations (from JSON config), with logging

## LLM Dashboard

- [ ] Hub/landing page showing recent HA entities + webhooks (last hour), with drill-down links
- [ ] Purpose: expose data most likely useful _right now_ for reacting to real-time events

## ActivityWatch Integration

- [ ] Expose ActivityWatch data via dashboard (API server pointer in config, aggregates from multiple hosts)
- [ ] Show current activity (last 10 min), with clickable aggregation options (bucket size, app/window level)

## Reporter

- [ ] Reporter scripts/daemons for device events (do not duplicate ActivityWatch)

## Cleanup

- [ ] Resolve remaining TODO comments in code (e.g., redirect after login)
