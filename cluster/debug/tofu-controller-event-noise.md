# tofu-controller Event Noise

**Date**: 2026-04-01
**Status**: still live, smaller. Re-measured 2026-08-16: 3,611 events cluster-wide,
1,065 of them (~29%) `tf-runner`. All 15 `Terraform` CRs now run at `interval: 15m`
rather than the 10m default, and `haku/cloud-agent-tf` is suspended — the first two
options below, partly taken. tf-runner is no longer the majority of the event store,
so the remaining question is whether to push low-churn roots (`dns-records`,
`sso-providers`) out to 1h+.

## Observation

`kubectl get events -A` shows ~3,200 events. Random sampling reveals the vast
majority are tofu-controller `tf-runner` pod lifecycle events (Scheduled, Pulled,
Created, Started, Killing) — not actual errors.

With ~24 Terraform resources, each reconciliation cycle spawns an ephemeral runner
pod. At the default 10-minute interval, that's ~24 pods × 5 lifecycle events ×
6 cycles/hour = ~720 events/hour just from tf-runner churn. Over the default 1-hour
event TTL, this dominates the event store.

## Impact

- Makes `kubectl get events` nearly useless for spotting real issues
- Inflates the `apiserver_storage_objects{resource="events"}` metric (3,200+)
- Warning events from actual failures get buried

## Options

- **Increase `spec.interval`** on low-churn Terraform resources (e.g., `dns-records`,
  `sso-providers` that rarely change) from 10m to 1h+
- **Suspend idle Terraform resources** that only need to run during bootstrap
- **Upstream**: tofu-controller has no option to suppress runner pod events
