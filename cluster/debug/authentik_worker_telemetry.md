# Authentik worker telemetry and imported dashboard

On 2026-11-16, the imported Authentik Grafana dashboard had empty task and
policy panels. Live Mimir queries showed `up{namespace="authentik"}` for the
server and two database pods, but none for the worker; no
`authentik_tasks_total` or `authentik_policies_execution_time_count` series
were stored. The existing `authentik/server` PodMonitor selects only server
pods.

The live worker exposes task totals and duration histograms on port 9300, and
its labels include `app.kubernetes.io/component=worker` and
`app.kubernetes.io/name=authentik`. A pod in `monitoring` reached the worker's
`10.244.4.120:9300/metrics` endpoint. No ingress policy selects the worker,
so collecting it requires a worker PodMonitor but no new network-policy rule.

The imported Grafana.com dashboard revision 2 has a separate schema problem:
its Task status and System task duration panels query the absent
`authentik_system_tasks` family, and policy panels use labels
`binding_target_type` and `object_type`. Current Authentik instead exports
`authentik_tasks_total{actor_name,queue_name}` and
`authentik_tasks_duration_milliseconds_*` on the worker, and
`authentik_policies_engine_time_total_seconds_*{obj_pk,obj_type}` on the
server. A separate dashboard change must use the current metric semantics;
adding the worker scrape alone does not repair the old expressions. Verify
the worker's `up` and task series after the PodMonitor rolls out, and retire
this note once the dashboard migration is complete.
