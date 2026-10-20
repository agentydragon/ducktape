"""The cluster-wide alert and recording PrometheusRules in the monitoring namespace."""

from __future__ import annotations

from textwrap import dedent

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group

NAME = "monitoring-rules"
NAMESPACE = "monitoring"

_CONTROL_PLANE_IO = [
    Rule.alert(
        "ControlPlaneSystemDiskBusy",
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
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="Control-plane system disk is busy on {{ $labels.node }}",
        description=(
            "/dev/sda on control-plane node {{ $labels.node }} has been busy for more than 50% of wall time over 15 "
            "minutes. This disk also carries Talos EPHEMERAL and etcd."
        ),
    ),
    Rule.alert(
        "ControlPlanePodSystemDiskWriterHigh",
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
        ),
        for_="10m",
        labels={"severity": "warning"},
        summary="Pod {{ $labels.namespace }}/{{ $labels.pod }} is writing heavily to control-plane /dev/sda",
        description=(
            "Pod {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.node }} is writing "
            "{{ $value | humanize1024 }}B/s to /dev/sda for more than 10 minutes, which can contend with etcd."
        ),
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
    Rule.alert(
        "ControlPlaneLeasePutLatency",
        dedent(
            """\
            histogram_quantile(
              0.99,
              sum by (instance, le) (
                rate(apiserver_request_duration_seconds_bucket{resource="leases", verb="PUT", scope="resource"}[10m])
              )
            ) > 0.5
            """
        ),
        for_="10m",
        labels={"severity": "warning"},
        summary="Apiserver lease PUT latency is high on {{ $labels.instance }}",
        description=(
            "p99 apiserver lease PUT latency on {{ $labels.instance }} has exceeded 500ms for 10 minutes. This is an "
            "early signal for leader-election and node-lease write path contention."
        ),
    ),
    Rule.alert(
        "ControlPlaneLeasePutLatency",
        dedent(
            """\
            histogram_quantile(
              0.99,
              sum by (instance, le) (
                rate(apiserver_request_duration_seconds_bucket{resource="leases", verb="PUT", scope="resource"}[10m])
              )
            ) > 2
            """
        ),
        for_="5m",
        labels={"severity": "critical"},
        summary="Apiserver lease PUT latency is critical on {{ $labels.instance }}",
        description=(
            "p99 apiserver lease PUT latency on {{ $labels.instance }} has exceeded 2s for 5 minutes. Leader election "
            "and node heartbeats may time out."
        ),
    ),
]

_EXTERNAL_SECRETS = [
    Rule.alert(
        "ExternalSecretNotReady",
        'externalsecret_status_condition{condition="Ready",status="False"} == 1',
        for_="10m",
        labels={"severity": "warning"},
        summary="ExternalSecret {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
        description=(
            "ExternalSecret {{ $labels.namespace }}/{{ $labels.name }} has reported Ready=False for more than 10 "
            "minutes. For Kubernetes-provider mirrors, this usually means the source Secret/key is missing or "
            "unreadable.\n"
        ),
    ),
    Rule.alert(
        "ClusterExternalSecretNotReady",
        'clusterexternalsecret_status_condition{condition="Ready",status="False"} == 1',
        for_="10m",
        labels={"severity": "warning"},
        summary="ClusterExternalSecret {{ $labels.name }} is not Ready",
        description=(
            "ClusterExternalSecret {{ $labels.name }} has reported Ready=False for more than 10 minutes. Check failed "
            "namespaces and generated ExternalSecret children.\n"
        ),
    ),
    Rule.alert(
        "ExternalSecretsControllerBacklogged",
        dedent(
            """\
            sum by (controller) (
              workqueue_depth{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"}
            ) > 0
            and on (controller)
            controller_runtime_active_workers{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"}
              >= controller_runtime_max_concurrent_reconciles{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"}
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="External Secrets {{ $labels.controller }} controller is saturated with queued work",
        description=(
            "External Secrets {{ $labels.controller }} has queued work while all workers are busy for more than 15 "
            "minutes. Short-refresh mirrors may stop propagating source Secret updates even while Ready=True remains "
            "stale.\n"
        ),
    ),
    Rule.alert(
        "ExternalSecretsControllerLongRunningWorker",
        'workqueue_longest_running_processor_seconds{controller=~"externalsecret|clusterexternalsecret|secretstore|clustersecretstore"} > 120',
        for_="5m",
        labels={"severity": "warning"},
        summary=(
            "External Secrets {{ $labels.controller }} worker has been running for {{ $value | humanizeDuration }}"
        ),
        description=(
            "A single External Secrets {{ $labels.controller }} worker has been processing one item for more than two "
            "minutes. With low concurrency, this can starve unrelated mirrors and leave target Secrets stale.\n"
        ),
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
    Rule.alert(
        "FluxKustomizationNotReady",
        dedent(
            """\
            kube_customresource_flux_kustomization_ready{status!="True"} == 1
            unless on (namespace, name) (kube_customresource_flux_kustomization_suspended == 1)
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="Flux Kustomization {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
        description=(
            "Flux Kustomization {{ $labels.namespace }}/{{ $labels.name }} has reported Ready={{ $labels.status }} "
            "for more than 15 minutes."
        ),
    ),
    Rule.alert(
        "FluxHelmReleaseNotReady",
        dedent(
            """\
            kube_customresource_flux_helmrelease_ready{status!="True"} == 1
            unless on (namespace, name) (kube_customresource_flux_helmrelease_suspended == 1)
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="Flux HelmRelease {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
        description=(
            "Flux HelmRelease {{ $labels.namespace }}/{{ $labels.name }} has reported Ready={{ $labels.status }} for "
            "more than 15 minutes."
        ),
    ),
    Rule.alert(
        "FluxGitRepositoryNotReady",
        dedent(
            """\
            kube_customresource_flux_gitrepository_ready{status!="True"} == 1
            unless on (namespace, name) (kube_customresource_flux_gitrepository_suspended == 1)
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="Flux GitRepository {{ $labels.namespace }}/{{ $labels.name }} is not Ready",
        description=(
            "Flux GitRepository {{ $labels.namespace }}/{{ $labels.name }} has reported Ready={{ $labels.status }} "
            "for more than 15 minutes."
        ),
    ),
    Rule.alert(
        "FluxMainGitRepositoryArtifactLarge",
        'kube_customresource_flux_gitrepository_artifact_size_bytes{namespace="flux-system",name="flux-system"} > 16777216',
        for_="30m",
        labels={"severity": "warning"},
        summary="Flux main GitRepository artifact is large",
        description=(
            "GitRepository flux-system/flux-system artifact is {{ $value | humanize1024 }}B, increasing source "
            "unpack/write pressure for every dependent Kustomization."
        ),
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
    Rule.alert(
        "GitHubGraphQLQuotaBurnRateHigh",
        # 5000 points/hour is 1.389/s; `used` resets hourly, and `deriv` on the
        # reset goes sharply negative rather than spiking, so it does not fire.
        "deriv(github_graphql_rate_used[5m]) > 1.4",
        for_="2m",
        labels={"severity": "warning"},
        summary="GitHub GraphQL points for {{ $labels.github_account }} burning at {{ $value | humanize }}/s",
        description=(
            "Sustained above the 5000/hour budget, so this hour's bucket will exhaust before it resets. Attribution "
            "is only possible while this is firing: sample the bucket at 5s resolution to get the shape, and check "
            "the connection recorder and pod flow metrics against the same window.\n"
        ),
    ),
    Rule.alert(
        "GitHubGraphQLQuotaExhausted",
        # Latch a sampled zero across the hourly reset and Alertmanager's 30s
        # group_wait. Aggregate replacement/overlapping targets by account.
        # A zero entirely between the unchanged 1m scrapes is not observable.
        "min by (github_account) (min_over_time(github_graphql_rate_remaining[5m])) == 0",
        labels={"severity": "warning"},
        summary="GitHub GraphQL quota for {{ $labels.github_account }} was exhausted in the last 5 minutes",
        description=(
            "A retained observation reported zero remaining points. Ordinary GraphQL operations can fail until reset; "
            "rate-only probes may still work, and HTTP status varies. This alert remains active briefly after reset. "
            "Exhaustion entirely between the one-minute scrapes cannot be detected. REST `/rate_limit` can disagree "
            "with this bucket.\n"
        ),
    ),
    Rule.alert(
        "GitHubGraphQLQuotaObservationsMissing",
        # up has no github_account label. Keep the two expected account/job
        # mappings explicit so disappearance of discovery itself is detected.
        # TargetDown owns explicit scrape failures (including upstream fetch
        # errors, which the exporter exposes as HTTP 502). Only suppress when
        # ALL current targets are down: a failed old pod must not hide a healthy
        # replacement that returns no quota gauge.
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
        ),
        labels={"severity": "warning"},
        summary="GitHub GraphQL quota observations for {{ $labels.github_account }} are missing",
        description=(
            "No remaining-quota sample has been retained for five minutes. Check discovery, Alloy collection/remote "
            "write, and the exporter for this account. Missing observations cannot establish that quota stayed "
            "nonzero. Explicitly failed scrapes are covered separately by TargetDown after ten minutes.\n"
        ),
    ),
]

_GROCY_MCP = [
    Rule.alert(
        "GrocyMcpNoTools",
        'grocy_mcp_tools{namespace=~"grocy-.+"} == 0',
        for_="5m",
        labels={"severity": "warning"},
        summary="Grocy MCP {{ $labels.namespace }}/{{ $labels.pod }} is advertising zero tools",
        description=(
            "The Grocy MCP server is reachable on its metrics port, but its local FastMCP registry reports zero "
            "advertised tools. Clients can authenticate successfully and still see an empty tool list. Check the pod "
            "logs for OpenAPI generation, TOOL_OVERRIDES, or batch tool registration failures.\n"
        ),
    )
]

_MCP_AUTH = [
    # An OIDCProxy-fronted MCP server (oauth facades, grocy-mcp) failed to
    # refresh a client's token against Authentik or persist refreshed OAuth
    # state. Before the
    # RetryableRefreshOIDCProxy fix this instantly killed the claude.ai connector
    # (invalid_grant is terminal); now transient failures answer 503, but
    # each one still means claude.ai saw an error and the connector may
    # need a manual Reconnect. RCA:
    # cluster/debug/2026_06_claude_ai_connector_deauth.md
    Rule.alert(
        "McpUpstreamTokenRefreshFailed",
        "increase(mcp_auth_upstream_refresh_failures_total[1h]) > 0",
        labels={"severity": "warning"},
        summary=(
            "MCP server {{ $labels.namespace }}/{{ $labels.pod }} failed a token refresh (outcome: "
            "{{ $labels.outcome }})"
        ),
        description=(
            '{{ $value | printf "%.0f" }} token refresh failure(s) in the last hour on '
            "{{ $labels.namespace }}/{{ $labels.pod }} (outcome={{ $labels.outcome }}). outcome=transient means "
            "Authentik was unreachable/5xx (client got 503 and may recover on its own); outcome=oauth means Authentik "
            "genuinely rejected a grant — the claude.ai connector is likely dead until manually reconnected; "
            "outcome=storage means the MCP server could not persist OAuth state to its local store such as Valkey "
            "(client got 503 and may retry). Check Settings → Connectors in claude.ai, authentik-server health, and "
            "the MCP namespace's state-store pods.\n"
        ),
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
    Rule.alert(
        "KubeNodeNotReady",
        dedent(
            """\
            (
              kube_node_status_condition{condition="Ready",job="kube-state-metrics",status="true"} == 0
              and on (cluster, node)
              kube_node_spec_unschedulable{job="kube-state-metrics"} == 0
            )
            unless on (node) (kube_node_spec_taint{job="kube-state-metrics",key="node-role.kubernetes.io/roaming"} == 1)
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="Node is not ready.",
        description=(
            "{{ $labels.node }} has been unready for more than 15 minutes. Roaming nodes are excluded, so this is a "
            "node that was expected to stay up."
        ),
        annotations={"runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubenodenotready"},
    ),
    # Upstream already excludes nodes carrying a "this node is going away"
    # taint (cluster-autoscaler, spot termination). Roaming laptops are the
    # same category, so this adds one key to that existing regex rather than
    # bolting on a second mechanism.
    Rule.alert(
        "KubeNodeUnreachable",
        dedent(
            """\
            (
              kube_node_spec_taint{effect="NoSchedule",job="kube-state-metrics",key="node.kubernetes.io/unreachable"}
              unless ignoring (key, value)
              kube_node_spec_taint{job="kube-state-metrics",key=~"ToBeDeletedByClusterAutoscaler|cloud.google.com/impending-node-termination|aws-node-termination-handler/spot-itn|node-role.kubernetes.io/roaming"}
            ) == 1
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="Node is unreachable.",
        description=(
            "{{ $labels.node }} is unreachable and some workloads may be rescheduled. Roaming nodes are excluded, so "
            "this is a node that was expected to stay up."
        ),
        annotations={"runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubenodeunreachable"},
    ),
]

# Physical disk attribution for the cluster-disk-usage dashboard: which component holds how many bytes on
# each scraped filesystem, keyed by (node, device). Component bytes are what the component itself reports
# (SeaweedFS volume files, ClickHouse MergeTree parts, Postgres database files, kubelet volume stats), so
# `disk_usage:unattributed_bytes` is everything else on the filesystem: container images, emptyDirs, logs,
# etcd, node-local PVCs whose owner exports no size, and collector skew.
_NODE_EXPORTER_NODE = (
    'on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",'
    'pod=~"prometheus-node-exporter-.+"})'
)
_FILESYSTEMS = 'fstype=~"ext4|xfs",device!=""'


def _filesystem_bytes(metric: str) -> str:
    return f"max by (node, device) ({metric}{{{_FILESYSTEMS}}} * {_NODE_EXPORTER_NODE})"


def _pvc_on_mount_prefix(segments: int) -> str:
    """PVCs whose backing path starts with a `segments`-component mountpoint on their pod's node."""
    mountpoint = f'"$1", "path", "({"/[^/]+" * segments})(/.*)?"' if segments else '"/", "path", "/.*"'
    return (
        f'label_replace(disk_usage:pvc_path:info, "mountpoint", {mountpoint})\n'
        "  * on (node, mountpoint) group_left (device) disk_usage:mount:info"
    )


def _component(category: str, expr: str) -> str:
    return f'label_replace(\n{expr},\n  "category", "{category}", "", ""\n)'


_DISK_USAGE = [
    Rule.record(
        "disk_usage:mount:info",
        f"group by (node, device, mountpoint) (node_filesystem_size_bytes{{{_FILESYSTEMS}}} * {_NODE_EXPORTER_NODE})",
    ),
    Rule.record("disk_usage:filesystem_size_bytes", _filesystem_bytes("node_filesystem_size_bytes")),
    # Free counts root-reserved blocks (ext4 reserves 5% by default); avail does not. Used is size - free.
    Rule.record("disk_usage:filesystem_free_bytes", _filesystem_bytes("node_filesystem_free_bytes")),
    Rule.record("disk_usage:filesystem_avail_bytes", _filesystem_bytes("node_filesystem_avail_bytes")),
    # `media` from the kernel's rotational flag on the filesystem's whole disk. Virtual disks report
    # rotational=1 whatever backs them on the hypervisor, so they are `virtual` instead.
    Rule.record(
        "disk_usage:filesystem:info",
        dedent(
            f"""\
            max by (node, device, media) (
              label_replace(
                group by (node, device) (disk_usage:mount:info),
                "disk", "$1", "device", "/dev/(nvme[0-9]+n[0-9]+|mmcblk[0-9]+|[a-z]+)(p?[0-9]+)?"
              )
              * on (node, disk) group_left (media)
                max by (node, disk, media) (
                  label_replace(label_replace(label_replace(label_replace(label_replace(
                    node_disk_info * {_NODE_EXPORTER_NODE},
                    "disk", "$1", "device", "(.+)"),
                    "media", "HDD", "rotational", "1"),
                    "media", "SSD", "rotational", "0"),
                    "media", "virtual", "model", "QEMU_.*"),
                    "media", "virtual", "device", "vd[a-z]+")
                )
              or on (node, device)
              label_replace(group by (node, device) (disk_usage:mount:info), "media", "unknown", "", "")
            )
            """
        ),
    ),
    # The host path behind each pod's PVC: `host_path` for local-path-provisioner volumes, `local_path`
    # for static local PVs. CSI volumes have neither and drop out.
    Rule.record(
        "disk_usage:pvc_path:info",
        dedent(
            """\
            max by (namespace, pod, volume, persistentvolumeclaim, node, path) (
              max by (namespace, pod, volume, persistentvolumeclaim) (kube_pod_spec_volumes_persistentvolumeclaims_info)
              * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{node!=""})
              * on (namespace, persistentvolumeclaim) group_left (persistentvolume)
                label_replace(
                  max by (namespace, persistentvolumeclaim, volumename) (kube_persistentvolumeclaim_info),
                  "persistentvolume", "$1", "volumename", "(.+)"
                )
              * on (persistentvolume) group_left (path)
                label_replace(
                  label_replace(
                    max by (persistentvolume, host_path, local_path) (kube_persistentvolume_info),
                    "path", "$1", "host_path", "(.+)"
                  ),
                  "path", "$1", "local_path", "(.+)"
                )
            )
            """
        ),
    ),
    # Longest mountpoint prefix of the PVC path wins; mountpoints deeper than three components are not
    # matched and fall through to a shorter enclosing mount.
    Rule.record(
        "disk_usage:pvc_filesystem:info",
        "max by (namespace, pod, volume, persistentvolumeclaim, node, device) (\n"
        + "\nor on (namespace, pod, volume)\n".join(_pvc_on_mount_prefix(n) for n in (3, 2, 1, 0))
        + "\n)\n",
    ),
    Rule.record(
        "disk_usage:component_used_bytes",
        "\nor\n".join(
            [
                # Each pod-joining branch narrows the right side to its own pods: a pod elsewhere with two
                # node-local PVCs would otherwise be a duplicate match group and fail the whole rule.
                # One copy per volume server: replicas are separate physical bytes on separate disks.
                _component(
                    "SeaweedFS",
                    dedent(
                        """\
                        sum by (node, device, namespace, persistentvolumeclaim, component) (
                          label_replace(
                            label_replace(
                              max by (namespace, pod, collection) (
                                SeaweedFS_volumeServer_total_disk_size{namespace="seaweedfs",type="normal"}
                              ),
                              "component", "SeaweedFS (default collection)", "collection", ""
                            ),
                            "component", "SeaweedFS: $1", "collection", "(.+)"
                          )
                          * on (namespace, pod) group_left (node, device, persistentvolumeclaim)
                            disk_usage:pvc_filesystem:info{namespace="seaweedfs"}
                        )"""
                    ),
                ),
                _component(
                    "SeaweedFS deleted, awaiting vacuum",
                    dedent(
                        """\
                        label_replace(
                          sum by (node, device, namespace, persistentvolumeclaim) (
                            max by (namespace, pod) (
                              SeaweedFS_volumeServer_total_disk_size{namespace="seaweedfs",type="deleted_bytes"}
                            )
                            * on (namespace, pod) group_left (node, device, persistentvolumeclaim)
                              disk_usage:pvc_filesystem:info{namespace="seaweedfs"}
                          ),
                          "component", "SeaweedFS deleted, awaiting vacuum", "", ""
                        )"""
                    ),
                ),
                # MergeTree parts only. ClickHouse's DiskUsed is the whole filesystem's used bytes, shared
                # with everything else on the disk.
                _component(
                    "ClickHouse",
                    dedent(
                        """\
                        label_replace(
                          sum by (node, device, namespace, persistentvolumeclaim) (
                            (
                              max by (namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTables{namespace="clickhouse"})
                              + max by (namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTablesSystem{namespace="clickhouse"})
                            )
                            * on (namespace, pod) group_left (node, device, persistentvolumeclaim)
                              disk_usage:pvc_filesystem:info{namespace="clickhouse"}
                          ),
                          "component", "ClickHouse", "", ""
                        )"""
                    ),
                ),
                # Database files on each instance's PGDATA volume; WAL and other PostgreSQL files are not
                # counted.
                _component(
                    "Postgres (CNPG)",
                    dedent(
                        """\
                        sum by (node, device, namespace, persistentvolumeclaim, component) (
                          label_replace(
                            max by (namespace, pod, datname) (cnpg_pg_database_size_bytes{datname!~"template0|template1"}),
                            "component", "Postgres: $1", "namespace", "(.+)"
                          )
                          * on (namespace, pod) group_left (node, device, persistentvolumeclaim)
                            disk_usage:pvc_filesystem:info{volume="pgdata"}
                        )"""
                    ),
                ),
                _component(
                    "Kubelet-measured PVCs",
                    dedent(
                        """\
                        label_replace(
                          label_join(
                            max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes)
                            * on (namespace, persistentvolumeclaim) group_left (node, device)
                              max by (namespace, persistentvolumeclaim, node, device) (disk_usage:pvc_filesystem:info),
                            "component", "/", "namespace", "persistentvolumeclaim"
                          ),
                          "component", "PVC: $1", "component", "(.+)"
                        )"""
                    ),
                ),
            ]
        )
        + "\n",
    ),
    Rule.record(
        "disk_usage:unattributed_bytes",
        dedent(
            """\
            disk_usage:filesystem_size_bytes - disk_usage:filesystem_free_bytes
            - on (node, device) (
              sum by (node, device) (disk_usage:component_used_bytes)
              or on (node, device) 0 * disk_usage:filesystem_size_bytes
            )
            """
        ),
    ),
]

_ROAMING_NODE_WORKLOAD = [
    # The stock rule has no node label. Join through kube_pod_info so a pod
    # on a tainted roaming node disappears from the alert, while the same
    # pod condition remains alertable on ordinary nodes.
    Rule.alert(
        "KubePodNotReady",
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
        ),
        for_="15m",
        labels={"severity": "warning"},
        description=(
            "Pod {{ $labels.namespace }}/{{ $labels.pod }} has been in a non-ready state for longer than 15 minutes "
            "on cluster {{ $labels.cluster }}."
        ),
        summary="Pod has been in a non-ready state for more than 15 minutes.",
        annotations={"runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodnotready"},
    ),
    # The kubelet target already carries the node label, so the same taint
    # exclusion used by KubeNodeNotReady applies directly here.
    Rule.alert(
        "KubeletInstanceUnreachable",
        dedent(
            """\
            (
              up{job="kubelet", metrics_path="/metrics"} == 0
            )
            unless on (node)
            kube_node_spec_taint{job="kube-state-metrics", key="node-role.kubernetes.io/roaming"} == 1
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        description="A Kubelet instance has been unreachable for more than 15 minutes.",
        summary="Kubelet instance is unreachable.",
        annotations={
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubeletinstanceunreachable"
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
    Rule.alert(
        "TargetDown",
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
        ),
        for_="10m",
        labels={"severity": "warning"},
        summary="One or more targets are unreachable.",
        description=(
            '{{ printf "%.4g" $value }}% of the {{ $labels.job }}/{{ $labels.service }} targets in '
            "{{ $labels.namespace }} namespace are down. Roaming nodes are excluded from both sides of the ratio."
        ),
        annotations={"runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/general/targetdown"},
    ),
]


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    PrometheusRule(
        chart,
        "control-plane-io-alerts",
        metadata=ApiObjectMetadata(name="control-plane-io-alerts", namespace=NAMESPACE),
        groups=[group("control-plane-io", _CONTROL_PLANE_IO)],
    )
    PrometheusRule(
        chart,
        "disk-usage-rules",
        metadata=ApiObjectMetadata(name="disk-usage-rules", namespace=NAMESPACE),
        groups=[group("disk-usage", _DISK_USAGE)],
    )
    PrometheusRule(
        chart,
        "external-secrets-alerts",
        metadata=ApiObjectMetadata(name="external-secrets-alerts", namespace=NAMESPACE),
        groups=[group("external-secrets", _EXTERNAL_SECRETS)],
    )
    PrometheusRule(
        chart,
        "flux-alerts",
        metadata=ApiObjectMetadata(name="flux-alerts", namespace=NAMESPACE),
        groups=[group("flux", _FLUX)],
    )
    PrometheusRule(
        chart,
        "github-quota-alerts",
        metadata=ApiObjectMetadata(name="github-quota-alerts", namespace=NAMESPACE),
        groups=[group("github-quota", _GITHUB_QUOTA)],
    )
    PrometheusRule(
        chart,
        "grocy-mcp-alerts",
        metadata=ApiObjectMetadata(name="grocy-mcp-alerts", namespace=NAMESPACE),
        groups=[group("grocy-mcp", _GROCY_MCP)],
    )
    PrometheusRule(
        chart,
        "mcp-auth-alerts",
        metadata=ApiObjectMetadata(name="mcp-auth-alerts", namespace=NAMESPACE),
        groups=[group("mcp-auth", _MCP_AUTH)],
    )
    PrometheusRule(
        chart,
        "roaming-node-alerts",
        metadata=ApiObjectMetadata(name="roaming-node-alerts", namespace=NAMESPACE),
        groups=[
            group("roaming-node-alerts", _ROAMING_NODE),
            group("roaming-node-workload-alerts", _ROAMING_NODE_WORKLOAD),
        ],
    )
    return chart
