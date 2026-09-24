"""The cluster-wide alert PrometheusRules in the monitoring namespace."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from cdk8s import App, Chart
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRule,
    PrometheusRuleSpec,
    PrometheusRuleSpecGroups,
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "monitoring-rules"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/rules"

_CONTROL_PLANE_IO = [
    PrometheusRuleSpecGroupsRules(
        alert="ControlPlaneSystemDiskBusy",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  rate(node_disk_io_time_seconds_total{device="sda"}[10m])
                  * on(namespace, pod) group_left(node)
                    kube_pod_info{namespace="monitoring", pod=~"prometheus-node-exporter-.+"}
                  * on(node) group_left()
                    kube_node_role{role="control-plane"}
                ) > 0.50
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Control-plane system disk is busy on {{ $labels.node }}",
            "description": (
                "/dev/sda on control-plane node {{ $labels.node }} has been busy for more than 50% of wall time over 15 "
                "minutes. This disk also carries Talos EPHEMERAL and etcd."
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="ControlPlanePodSystemDiskWriterHigh",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  sum by (namespace, pod, node) (
                    rate(container_fs_writes_bytes_total{device="/dev/sda", container!="", pod!="", namespace!="kube-system"}[10m])
                  )
                  * on(node) group_left()
                    kube_node_role{role="control-plane"}
                ) > 1048576
                """
            )
        ),
        for_="10m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Pod {{ $labels.namespace }}/{{ $labels.pod }} is writing heavily to control-plane /dev/sda",
            "description": (
                "Pod {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.node }} is writing "
                "{{ $value | humanize1024 }}B/s to /dev/sda for more than 10 minutes, which can contend with etcd."
            ),
        },
    ),
    # Both tiers share one alertname and are separated by severity alone. The cluster's stock
    # inhibit rule is `source severity=critical, target severity=~warning|info, equal:
    # [namespace, alertname]` -- distinct names (…High/…Critical) never satisfy that `equal`,
    # so critical could not suppress warning and a single slow apiserver notified twice.
    # Keep these two `alert:` values identical or the suppression silently stops working.
    #
    # These series carry no `namespace` (the expr aggregates `by (instance, le)`), so the
    # `equal` matches on absent-equals-absent. That also means the suppression is cluster-wide
    # rather than per-instance: while any apiserver is critical, the warning is suppressed for
    # all of them. Accepted — a critical here already means you're looking at the whole control
    # plane, and `instance` cannot be added to `equal` without diverging from the stock rule.
    PrometheusRuleSpecGroupsRules(
        alert="ControlPlaneLeasePutLatency",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                histogram_quantile(
                  0.99,
                  sum by (instance, le) (
                    rate(apiserver_request_duration_seconds_bucket{resource="leases", verb="PUT", scope="resource"}[10m])
                  )
                ) > 0.5
                """
            )
        ),
        for_="10m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Apiserver lease PUT latency is high on {{ $labels.instance }}",
            "description": (
                "p99 apiserver lease PUT latency on {{ $labels.instance }} has exceeded 500ms for 10 minutes. This is an "
                "early signal for leader-election and node-lease write path contention."
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="ControlPlaneLeasePutLatency",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                histogram_quantile(
                  0.99,
                  sum by (instance, le) (
                    rate(apiserver_request_duration_seconds_bucket{resource="leases", verb="PUT", scope="resource"}[10m])
                  )
                ) > 2
                """
            )
        ),
        for_="5m",
        labels={"severity": "critical"},
        annotations={
            "summary": "Apiserver lease PUT latency is critical on {{ $labels.instance }}",
            "description": (
                "p99 apiserver lease PUT latency on {{ $labels.instance }} has exceeded 2s for 5 minutes. Leader election "
                "and node heartbeats may time out."
            ),
        },
    ),
]

_EXTERNAL_SECRETS = [
    PrometheusRuleSpecGroupsRules(
        alert="ExternalSecretNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'externalsecret_status_condition{condition="Ready",status="False"} == 1'
        ),
        for_="10m",
        labels={"severity": "warning"},
        annotations={
            "summary": "ExternalSecret {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
            "description": (
                "ExternalSecret {{ $labels.namespace }}/{{ $labels.name }} has reported Ready=False for more than 10 "
                "minutes. For Kubernetes-provider mirrors, this usually means the source Secret/key is missing or "
                "unreadable.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="ClusterExternalSecretNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'clusterexternalsecret_status_condition{condition="Ready",status="False"} == 1'
        ),
        for_="10m",
        labels={"severity": "warning"},
        annotations={
            "summary": "ClusterExternalSecret {{ $labels.name }} is not Ready",
            "description": (
                "ClusterExternalSecret {{ $labels.name }} has reported Ready=False for more than 10 minutes. Check failed "
                "namespaces and generated ExternalSecret children.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="ExternalSecretsControllerBacklogged",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                sum by (controller) (
                  workqueue_depth{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"}
                ) > 0
                and on (controller)
                controller_runtime_active_workers{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"}
                  >= controller_runtime_max_concurrent_reconciles{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"}
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "External Secrets {{ $labels.controller }} controller is saturated with queued work",
            "description": (
                "External Secrets {{ $labels.controller }} has queued work while all workers are busy for more than 15 "
                "minutes. Short-refresh mirrors may stop propagating source Secret updates even while Ready=True remains "
                "stale.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="ExternalSecretsControllerLongRunningWorker",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'workqueue_longest_running_processor_seconds{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"} > 120'
        ),
        for_="5m",
        labels={"severity": "warning"},
        annotations={
            "summary": (
                "External Secrets {{ $labels.controller }} worker has been running for {{ $value | humanizeDuration }}"
            ),
            "description": (
                "A single External Secrets {{ $labels.controller }} worker has been processing one item for more than two "
                "minutes. With low concurrency, this can starve unrelated mirrors and leave target Secrets stale.\n"
            ),
        },
    ),
]

_FLUX = [
    # Suspending a Flux object does not clear its Ready condition, so anything
    # deliberately disabled reports Ready=False indefinitely. Before this exclusion
    # 13 of the 18 firing FluxKustomizationNotReady alerts were suspended services
    # (harbor/*, openhands/*, tandoor, sdr, ...) -- a permanent floor of ~156 ntfy
    # messages/day that exhausted the provider's daily quota and took *all* paging
    # down for 23h on 2026-08-05, criticals included.
    #
    # Gotcha: `unless` matches on label sets, NOT values, so the `== 1` inside the
    # parens is load-bearing. Flux omits spec.suspend when unset (no series, which
    # excludes nothing -- correct), but an object carrying an explicit
    # `suspend: false` produces the series with value 0, and a bare
    # `unless on (...) kube_..._suspended` would silently suppress a genuinely
    # broken service.
    PrometheusRuleSpecGroupsRules(
        alert="FluxKustomizationNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                kube_customresource_flux_kustomization_ready{status!="True"} == 1
                unless on (namespace, name) (kube_customresource_flux_kustomization_suspended == 1)
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Flux Kustomization {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
            "description": (
                "Flux Kustomization {{ $labels.namespace }}/{{ $labels.name }} has reported Ready={{ $labels.status }} "
                "for more than 15 minutes."
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="FluxHelmReleaseNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                kube_customresource_flux_helmrelease_ready{status!="True"} == 1
                unless on (namespace, name) (kube_customresource_flux_helmrelease_suspended == 1)
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Flux HelmRelease {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
            "description": (
                "Flux HelmRelease {{ $labels.namespace }}/{{ $labels.name }} has reported Ready={{ $labels.status }} for "
                "more than 15 minutes."
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="FluxGitRepositoryNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                kube_customresource_flux_gitrepository_ready{status!="True"} == 1
                unless on (namespace, name) (kube_customresource_flux_gitrepository_suspended == 1)
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Flux GitRepository {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
            "description": (
                "Flux GitRepository {{ $labels.namespace }}/{{ $labels.name }} has reported Ready={{ $labels.status }} "
                "for more than 15 minutes."
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="FluxMainGitRepositoryArtifactLarge",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'kube_customresource_flux_gitrepository_artifact_size_bytes{namespace="flux-system",name="flux-system"} > 16777216'
        ),
        for_="30m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Flux main GitRepository artifact is large",
            "description": (
                "GitRepository flux-system/flux-system artifact is {{ $value | humanize1024 }}B, increasing source "
                "unpack/write pressure for every dependent Kustomization."
            ),
        },
    ),
]

# Alerts on the GitHub GraphQL bucket, read from real response headers by
# `github-graphql-rate-exporter` (REST `/rate_limit` reports this bucket as
# permanently full, so it cannot be used for this).
#
# The burn-rate alert is the one that matters. The account's GraphQL consumption
# is intermittent and unattributed, and every diagnostic for it needs the burn to
# be *happening* — once the hour resets, there is nothing left to observe but a
# counter. Firing on rate rather than on exhaustion is what makes the window
# actionable instead of forensic.
_GITHUB_QUOTA = [
    PrometheusRuleSpecGroupsRules(
        alert="GitHubGraphQLQuotaBurnRateHigh",
        # 5000 points/hour is 1.389/s; `used` resets hourly, and `deriv` on the
        # reset goes sharply negative rather than spiking, so it does not fire.
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string("deriv(github_graphql_rate_used[5m]) > 1.4"),
        for_="2m",
        labels={"severity": "warning"},
        annotations={
            "summary": "GitHub GraphQL points for {{ $labels.github_account }} burning at {{ $value | humanize }}/s",
            "description": (
                "Sustained above the 5000/hour budget, so this hour's bucket will exhaust before it resets. Attribution "
                "is only possible while this is firing: sample the bucket at 5s resolution to get the shape, and check "
                "the connection recorder and pod flow metrics against the same window.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="GitHubGraphQLQuotaExhausted",
        # Latch a sampled zero across the hourly reset and Alertmanager's 30s
        # group_wait. Aggregate replacement/overlapping targets by account.
        # A zero entirely between the unchanged 1m scrapes is not observable.
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            "min by (github_account) (min_over_time(github_graphql_rate_remaining[5m])) == 0"
        ),
        labels={"severity": "warning"},
        annotations={
            "summary": "GitHub GraphQL quota for {{ $labels.github_account }} was exhausted in the last 5 minutes",
            "description": (
                "A retained observation reported zero remaining points. Ordinary GraphQL operations can fail until reset; "
                "rate-only probes may still work, and HTTP status varies. This alert remains active briefly after reset. "
                "Exhaustion entirely between the one-minute scrapes cannot be detected. REST `/rate_limit` can disagree "
                "with this bucket.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="GitHubGraphQLQuotaObservationsMissing",
        # up has no github_account label. Keep the two expected account/job
        # mappings explicit so disappearance of discovery itself is detected.
        # TargetDown owns explicit scrape failures (including upstream fetch
        # errors, which the exporter exposes as HTTP 502). Only suppress when
        # ALL current targets are down: a failed old pod must not hide a healthy
        # replacement that returns no quota gauge.
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  absent_over_time(github_graphql_rate_remaining{github_account="agentydragon"}[5m])
                  unless on ()
                  (max(up{job="github-graphql-rate-exporter-agentydragon",namespace="monitoring"}) == 0)
                )
                or
                (
                  absent_over_time(github_graphql_rate_remaining{github_account="agentydragon-agent"}[5m])
                  unless on ()
                  (max(up{job="github-graphql-rate-exporter-agentydragon-agent",namespace="monitoring"}) == 0)
                )
                """
            )
        ),
        labels={"severity": "warning"},
        annotations={
            "summary": "GitHub GraphQL quota observations for {{ $labels.github_account }} are missing",
            "description": (
                "No remaining-quota sample has been retained for five minutes. Check discovery, Alloy collection/remote "
                "write, and the exporter for this account. Missing observations cannot establish that quota stayed "
                "nonzero. Explicitly failed scrapes are covered separately by TargetDown after ten minutes.\n"
            ),
        },
    ),
]

_GROCY_MCP = [
    PrometheusRuleSpecGroupsRules(
        alert="GrocyMcpNoTools",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string('grocy_mcp_tools{namespace=~"grocy-.+"} == 0'),
        for_="5m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Grocy MCP {{ $labels.namespace }}/{{ $labels.pod }} is advertising zero tools",
            "description": (
                "The Grocy MCP server is reachable on its metrics port, but its local FastMCP registry reports zero "
                "advertised tools. Clients can authenticate successfully and still see an empty tool list. Check the pod "
                "logs for OpenAPI generation, TOOL_OVERRIDES, or batch tool registration failures.\n"
            ),
        },
    )
]

_HAKU_CONSOLE_CONNECTIONS = [
    # A linked provider connection (Google Mail/Calendar/Drive today) whose OAuth refresh has
    # been failing continuously. Distinct from McpUpstreamTokenRefreshFailed, which watches
    # OIDCProxy-fronted MCP servers refreshing against *Authentik* — this is haku-console
    # refreshing the operator's *Google* grant, a different credential against a different
    # issuer on a different code path. That rule existing is why this gap went unnoticed.
    #
    # Why it needs alerting at all: the failure is silent by construction. The console keeps
    # serving, the connector still appears in Settings, and scheduled Haku runs record the
    # affected watches as "skipped" and complete normally. Three outages (Jun, Jul, Aug 2026)
    # were each found by an agent happening to call get_mcp_server_status mid-run; the
    # 2026-08-04 one ran ~40h and 160 failed refreshes before anyone looked.
    #
    # 2h, not minutes: refreshes run on a schedule and transient Google 5xx are routine, so the
    # signal is persistence rather than any single failure. Every real instance ran 24h+.
    #
    # The `max by (...)` drops the per-pod labels deliberately. Every console replica exports
    # the gauge for every connection, all reading the same Postgres row, so the raw series fan
    # out one-per-replica and an unaggregated expr pages once per pod for a single broken
    # connection. `max` rather than `avg`/`min` so that if replicas ever do disagree, the alert
    # takes the pessimistic reading. `namespace` stays in the `by` list because the inhibit
    # rule below matches on it — aggregating it away silently breaks that suppression.
    #
    # Both tiers share the alertname on purpose, and severity alone separates them: the
    # cluster's stock inhibit rule is `source severity=critical, target severity=~warning|info,
    # equal: [namespace, alertname]`. Distinct names (…Stale/…StaleCritical) never match that
    # `equal`, so critical could not suppress warning and both fired together. Keep these two
    # `alert:` values identical or the suppression silently stops working.
    PrometheusRuleSpecGroupsRules(
        alert="HakuConsoleConnectionRefreshStale",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            "max by (namespace, connection, provider) (haku_console_connection_refresh_failure_age_seconds) > 7200"
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": (
                "haku-console connection {{ $labels.connection }} has been failing to refresh for "
                "{{ $value | humanizeDuration }}"
            ),
            "description": (
                "The OAuth refresh for provider connection {{ $labels.connection }} ({{ $labels.provider }}) has been "
                "failing continuously for {{ $value | humanizeDuration }}. Agents reading this connection are blind and "
                "scheduled runs will report their watches as skipped rather than failing. Fix: re-authorize the "
                "connection in the console's Settings → Connections. If it re-breaks immediately, the grant was revoked "
                "upstream rather than expired.\n"
            ),
        },
    ),
    # By this point at least one scheduled run has certainly gone blind on it.
    PrometheusRuleSpecGroupsRules(
        alert="HakuConsoleConnectionRefreshStale",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            "max by (namespace, connection, provider) (haku_console_connection_refresh_failure_age_seconds) > 86400"
        ),
        for_="15m",
        labels={"severity": "critical"},
        annotations={
            "summary": "haku-console connection {{ $labels.connection }} dead for over a day",
            "description": (
                "{{ $labels.connection }} has not refreshed successfully in over 24 hours "
                "({{ $value | humanizeDuration }}). Re-authorize it in the console's Settings → Connections.\n"
            ),
        },
    ),
]

_MCP_AUTH = [
    # An OIDCProxy-fronted MCP server (oauth facades, grocy-mcp) failed to
    # refresh a client's token against Authentik or persist refreshed OAuth
    # state. Before the
    # RetryableRefreshOIDCProxy fix this instantly killed the claude.ai connector
    # (invalid_grant is terminal); now transient failures answer 503, but
    # each one still means claude.ai saw an error and the connector may
    # need a manual Reconnect. RCA:
    # debug/2026_06_claude_ai_connector_deauth.md
    PrometheusRuleSpecGroupsRules(
        alert="McpUpstreamTokenRefreshFailed",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            "increase(mcp_auth_upstream_refresh_failures_total[1h]) > 0"
        ),
        labels={"severity": "warning"},
        annotations={
            "summary": (
                "MCP server {{ $labels.namespace }}/{{ $labels.pod }} failed a token refresh (outcome: "
                "{{ $labels.outcome }})"
            ),
            "description": (
                '{{ $value | printf "%.0f" }} token refresh failure(s) in the last hour on '
                "{{ $labels.namespace }}/{{ $labels.pod }} (outcome={{ $labels.outcome }}). outcome=transient means "
                "Authentik was unreachable/5xx (client got 503 and may recover on its own); outcome=oauth means Authentik "
                "genuinely rejected a grant — the claude.ai connector is likely dead until manually reconnected; "
                "outcome=storage means the MCP server could not persist OAuth state to its local store such as Valkey "
                "(client got 503 and may retry). Check Settings → Connectors in claude.ai, authentik-server health, and "
                "the MCP namespace's state-store pods.\n"
            ),
        },
    )
]

# Forks of kube-prometheus-stack rules, switched off via `defaultRules.disabled`
# in cluster/cdk8s/monitoring/stack.py. The only change is excluding roaming nodes;
# the existing node rules are copied verbatim from chart version 82.3.0, and
# the workload/kubelet rules below are copied from chart version 91.3.0.
#
# Why fork rather than suppress by alertname in the Alertmanager route: the
# alertname denylist is all-or-nothing, so denying an alert here also silenced
# it for the *control plane* -- a node going NotReady paged nobody. Excluding
# by taint keeps the alert for every non-roaming node.
#
# Why the taint and not the node names: `iguana` and `rugged` are defined in
# several places at once and hardcoding a third would be one more thing to
# forget. All of these describe the same set today:
#   - node taint `node-role.kubernetes.io/roaming=true:NoSchedule`  <- keyed on here
#   - node label `topology.kubernetes.io/region=roaming`
#   - `terraform/main/home-nodes.tf` (the declarations)
#   - `nebula-mesh.json` at the repo root (mesh roster)
#   - the Node Types table in <cluster/README.md>
# The taint wins because it is the *operational* statement -- a node tainted
# roaming is one the scheduler is told may vanish, which is exactly the
# property that makes NotReady uninteresting. Taint a new laptop and it is
# covered here with no edit. (Note README currently says only `rugged` carries
# the taint; both do, verified 2026-08-06.)
#
# CLEANUP(added 2026-08-06): re-diff against the chart on every
# kube-prometheus-stack bump -- a forked rule silently keeps the old expression
# when upstream fixes theirs. Sources: `kubernetes-system-kubelet` for
# KubeNodeNotReady/KubeNodeUnreachable, `general.rules` for TargetDown.
_ROAMING_NODE = [
    PrometheusRuleSpecGroupsRules(
        alert="KubeNodeNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  kube_node_status_condition{condition="Ready",job="kube-state-metrics",status="true"} == 0
                  and on (cluster, node)
                  kube_node_spec_unschedulable{job="kube-state-metrics"} == 0
                )
                unless on (node) (kube_node_spec_taint{job="kube-state-metrics",key="node-role.kubernetes.io/roaming"} == 1)
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Node is not ready.",
            "description": (
                "{{ $labels.node }} has been unready for more than 15 minutes. Roaming nodes are excluded, so this is a "
                "node that was expected to stay up."
            ),
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubenodenotready",
        },
    ),
    # Upstream already excludes nodes carrying a "this node is going away"
    # taint (cluster-autoscaler, spot termination). Roaming laptops are the
    # same category, so this adds one key to that existing regex rather than
    # bolting on a second mechanism.
    PrometheusRuleSpecGroupsRules(
        alert="KubeNodeUnreachable",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  kube_node_spec_taint{effect="NoSchedule",job="kube-state-metrics",key="node.kubernetes.io/unreachable"}
                  unless ignoring (key, value)
                  kube_node_spec_taint{job="kube-state-metrics",key=~"ToBeDeletedByClusterAutoscaler|cloud.google.com/impending-node-termination|aws-node-termination-handler/spot-itn|node-role.kubernetes.io/roaming"}
                ) == 1
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Node is unreachable.",
            "description": (
                "{{ $labels.node }} is unreachable and some workloads may be rescheduled. Roaming nodes are excluded, so "
                "this is a node that was expected to stay up."
            ),
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubenodeunreachable",
        },
    ),
]

_ROAMING_NODE_WORKLOAD = [
    # The stock rule has no node label. Join through kube_pod_info so a pod
    # on a tainted roaming node disappears from the alert, while the same
    # pod condition remains alertable on ordinary nodes.
    PrometheusRuleSpecGroupsRules(
        alert="KubePodNotReady",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  sum by (namespace, pod, job, cluster) (
                    max by (namespace, pod, job, cluster) (
                      kube_pod_status_phase{job="kube-state-metrics", namespace=~".*", phase=~"Pending|Unknown"}
                      or
                      (
                        kube_pod_status_phase{job="kube-state-metrics", namespace=~".*", phase="Running"} == 1
                        and on (namespace, pod, cluster)
                        kube_pod_status_ready{job="kube-state-metrics", namespace=~".*", condition="true"} == 0
                      )
                    ) * on (namespace, pod, cluster) group_left() topk by (namespace, pod, cluster) (
                      1, max by (namespace, pod, owner_kind, cluster) (kube_pod_owner{owner_kind!="Job"})
                    )
                  ) > 0
                  unless on (namespace, pod, cluster)
                  kube_pod_status_reason{job="kube-state-metrics", namespace=~".*", reason="SchedulingGated"} == 1
                )
                unless on (namespace, pod)
                (
                  kube_pod_info{job="kube-state-metrics", node!=""}
                  and on (node)
                  kube_node_spec_taint{job="kube-state-metrics", key="node-role.kubernetes.io/roaming"} == 1
                )"""
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "description": (
                "Pod {{ $labels.namespace }}/{{ $labels.pod }} has been in a non-ready state for longer than 15 minutes "
                "on cluster {{ $labels.cluster }}."
            ),
            "summary": "Pod has been in a non-ready state for more than 15 minutes.",
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodnotready",
        },
    ),
    # The kubelet target already carries the node label, so the same taint
    # exclusion used by KubeNodeNotReady applies directly here.
    PrometheusRuleSpecGroupsRules(
        alert="KubeletInstanceUnreachable",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                (
                  up{job="kubelet", metrics_path="/metrics"} == 0
                )
                unless on (node)
                kube_node_spec_taint{job="kube-state-metrics", key="node-role.kubernetes.io/roaming"} == 1
                """
            )
        ),
        for_="15m",
        labels={"severity": "warning"},
        annotations={
            "description": "A Kubelet instance has been unreachable for more than 15 minutes.",
            "summary": "Kubelet instance is unreachable.",
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubeletinstanceunreachable",
        },
    ),
    # Same exclusion, applied to the scrape-target ratio. The `unless` reaches
    # only the kubelet job by construction: `up` carries `node` for kubelet but
    # not for node-exporter or app targets, and a series without `node` matches
    # on the empty string, which never equals a real node name -- so everything
    # else passes through with stock behaviour. Verified 2026-08-06: stock fired
    # for kubelet + node-exporter + seaweedfs-volume-peer, this fires for the
    # latter two only, and the kubelet down-ratio goes 22.2% -> 0%.
    #
    # Excluded from BOTH sides of the ratio on purpose. Filtering only the
    # numerator would leave roaming targets padding the denominator, diluting
    # the percentage and making a real outage LESS likely to cross the 10%
    # threshold -- a silent desensitisation, which is worse than the noise.
    #
    # TargetDown stays on the Alertmanager denylist for now: node-exporter has
    # no `node` label to filter on (it would need an instance->IP->node join via
    # kube_node_status_addresses) and seaweedfs-volume-peer is genuinely down.
    # This removes one of the three reasons it cannot be re-enabled.
    PrometheusRuleSpecGroupsRules(
        alert="TargetDown",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                100 * (
                  count by (cluster, job, namespace, service) (
                    (up == 0) unless on (node) (kube_node_spec_taint{key="node-role.kubernetes.io/roaming"} == 1)
                  )
                  /
                  count by (cluster, job, namespace, service) (
                    up unless on (node) (kube_node_spec_taint{key="node-role.kubernetes.io/roaming"} == 1)
                  )
                ) > 10
                """
            )
        ),
        for_="10m",
        labels={"severity": "warning"},
        annotations={
            "summary": "One or more targets are unreachable.",
            "description": (
                '{{ printf "%.4g" $value }}% of the {{ $labels.job }}/{{ $labels.service }} targets in '
                "{{ $labels.namespace }} namespace are down. Roaming nodes are excluded from both sides of the ratio."
            ),
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/general/targetdown",
        },
    ),
]


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    PrometheusRule(
        chart,
        "control-plane-io-alerts",
        metadata=metadata("control-plane-io-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name="control-plane-io", rules=_CONTROL_PLANE_IO)]),
    )
    PrometheusRule(
        chart,
        "external-secrets-alerts",
        metadata=metadata("external-secrets-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name="external-secrets", rules=_EXTERNAL_SECRETS)]),
    )
    PrometheusRule(
        chart,
        "flux-alerts",
        metadata=metadata("flux-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name="flux", rules=_FLUX)]),
    )
    PrometheusRule(
        chart,
        "github-quota-alerts",
        metadata=metadata("github-quota-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name="github-quota", rules=_GITHUB_QUOTA)]),
    )
    PrometheusRule(
        chart,
        "grocy-mcp-alerts",
        metadata=metadata("grocy-mcp-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name="grocy-mcp", rules=_GROCY_MCP)]),
    )
    PrometheusRule(
        chart,
        "haku-console-connection-alerts",
        metadata=metadata("haku-console-connection-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(
            groups=[PrometheusRuleSpecGroups(name="haku-console-connections", rules=_HAKU_CONSOLE_CONNECTIONS)]
        ),
    )
    PrometheusRule(
        chart,
        "mcp-auth-alerts",
        metadata=metadata("mcp-auth-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name="mcp-auth", rules=_MCP_AUTH)]),
    )
    PrometheusRule(
        chart,
        "roaming-node-alerts",
        metadata=metadata("roaming-node-alerts", NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(
            groups=[
                PrometheusRuleSpecGroups(name="roaming-node-alerts", rules=_ROAMING_NODE),
                PrometheusRuleSpecGroups(name="roaming-node-workload-alerts", rules=_ROAMING_NODE_WORKLOAD),
            ]
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def monitoring_rules(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        retry_interval=None,
        wait=None,
        depends_on=[
            # PrometheusRule
            flux_kustomization_depends_on(monitoring_crds)
        ],
    )
