"""The cluster-wide alert and recording PrometheusRules in the monitoring namespace."""

from __future__ import annotations

from textwrap import dedent

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "monitoring-rules"
NAMESPACE = "monitoring"

_CONTROL_PLANE_IO = [
    Rule.record(
        "cluster:talos_system_disk_device_info",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/control-plane-system-disk-device-info.promql")
        ).read_text(),
    ),
    Rule.alert(
        "ControlPlaneSystemDiskBusy",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/control-plane-system-disk-busy-warning.promql")
        ).read_text(),
        for_="15m",
        labels={"severity": "warning"},
        summary="Control-plane system disk {{ $labels.device_path }} is busy on {{ $labels.node }}",
        description=(
            "Device {{ $labels.device_path }} backing Talos /var on control-plane node {{ $labels.node }} has been "
            "busy for more than 50% of wall time over 15 minutes. Talos EPHEMERAL and etcd use this system disk."
        ),
    ),
    Rule.alert(
        "ControlPlanePodSystemDiskWriterHigh",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/control-plane-pod-system-disk-writer-high-warning.promql")
        ).read_text(),
        for_="10m",
        labels={"severity": "warning"},
        summary="Pod {{ $labels.namespace }}/{{ $labels.pod }} is writing heavily to {{ $labels.device_path }}",
        description=(
            "Pod {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.node }} is writing "
            "{{ $value | humanize1024 }}B/s to {{ $labels.device_path }}, the device backing Talos /var, for more "
            "than 10 minutes, which can contend with etcd."
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/control-plane-lease-put-latency-warning.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/control-plane-lease-put-latency-critical.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/external-secrets-controller-backlogged-warning.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/flux-kustomization-not-ready-warning.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/flux-helm-release-not-ready-warning.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/flux-git-repository-not-ready-warning.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/github-graphql-quota-observations-missing-warning.promql")
        ).read_text(),
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

_IO_PRESSURE = [
    # PSI `full` (every non-idle task stalled at once), not `some`: one I/O-bound job keeps `some` high while
    # the rest of the node runs fine.
    Rule.alert(
        "NodeIOPressure",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/node-io-pressure-warning.promql")).read_text(),
        for_="1h",
        labels={"severity": "warning"},
        summary="{{ $labels.node }} is stalled on disk I/O",
        description=(
            "All non-idle tasks on {{ $labels.node }} were stalled on I/O at once for "
            "{{ $value | humanizePercentage }} of the time, sustained for an hour. Image pulls and Pod starts there "
            "slow down with it. container_pressure_io_stalled_seconds_total on that node shows who is waiting; "
            "ContainerPageCacheThrash names the container when the reads are its own evicted files."
        ),
    ),
    # Major faults are the discriminator: a container reading files with read() stalls on I/O too but takes
    # none.
    Rule.alert(
        "ContainerPageCacheThrash",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/container-page-cache-thrash-warning.promql")
        ).read_text(),
        for_="15m",
        labels={"severity": "warning"},
        summary="{{ $labels.namespace }}/{{ $labels.pod }} {{ $labels.container }} is thrashing its page cache",
        description=(
            "Container {{ $labels.container }} of {{ $labels.namespace }}/{{ $labels.pod }} on {{ $labels.node }} "
            "is stalled on I/O {{ $value | humanizePercentage }} of the time while taking more than 5 major page "
            "faults/s: the kernel keeps evicting its mapped files, usually its own binary, and it reads them back "
            "from disk. It stays Ready and is never OOM-killed; the node's disk saturates instead. Raise its memory "
            "limit above heap plus mapped files (cluster/docs/lessons_learned/2026_09_07_eso_memory_limit_thrash.md)."
        ),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/kube-node-not-ready-warning.promql")
        ).read_text(),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/kube-node-unreachable-warning.promql")
        ).read_text(),
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
_FILESYSTEMS = 'fstype=~"ext4|xfs",device!="",device!~"/dev/drbd.*"'


def _filesystem_bytes(metric: str) -> str:
    return f"max by (node, device) ({metric}{{{_FILESYSTEMS}}} * {_NODE_EXPORTER_NODE})"


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
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/disk-usage-pvc-path-info.promql")).read_text(),
    ),
    # Longest mountpoint prefix of the PVC path wins; mountpoints deeper than three components are not
    # matched and fall through to a shorter enclosing mount.
    Rule.record(
        "disk_usage:pvc_filesystem:info",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/disk-usage-pvc-filesystem-info.promql")
        ).read_text(),
    ),
    # Workload-scoped pod-to-PVC joins avoid duplicate matches from unrelated pods; Seaweed volume-server
    # replicas are separate physical copies and are counted once on each backing filesystem.
    Rule.record(
        "disk_usage:seaweedfs_volume_server_used_bytes",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/seaweedfs-disk-usage.promql")).read_text(),
        labels={"category": "SeaweedFS"},
    ),
    Rule.record(
        "disk_usage:seaweedfs_deleted_volume_server_used_bytes",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/seaweedfs-deleted-disk-usage.promql")
        ).read_text(),
        labels={"category": "SeaweedFS deleted, awaiting vacuum", "component": "SeaweedFS deleted, awaiting vacuum"},
    ),
    # MergeTree parts only. ClickHouse's DiskUsed is the whole filesystem's used bytes, shared
    # with everything else on the disk.
    Rule.record(
        "disk_usage:clickhouse_merge_tree_used_bytes",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/clickhouse-disk-usage.promql")).read_text(),
        labels={"category": "ClickHouse", "component": "ClickHouse"},
    ),
    # Database files on each instance's PGDATA volume; WAL and other PostgreSQL files are not counted.
    Rule.record(
        "disk_usage:cnpg_database_used_bytes",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/postgres-disk-usage.promql")).read_text(),
        labels={"category": "Postgres (CNPG)"},
    ),
    Rule.record(
        "disk_usage:application_used_bytes",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/disk-usage-application-used-bytes.promql")
        ).read_text(),
    ),
    # The filesystem total and application detail overlap. Use kubelet only as a fallback for a PVC
    # without application attribution; otherwise adding CSI joins would count its bytes twice.
    Rule.record(
        "disk_usage:kubelet_pvc_fallback_used_bytes",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/pvc-disk-usage.promql")).read_text(),
        labels={"category": "Kubelet-measured PVCs"},
    ),
    Rule.record(
        "disk_usage:component_used_bytes",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/disk-usage-component-used-bytes.promql")
        ).read_text(),
    ),
    Rule.record(
        "disk_usage:unattributed_bytes",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/disk-usage-unattributed-bytes.promql")
        ).read_text(),
    ),
]

# Piraeus copies belong to storage nodes, regardless of where a PVC is mounted. Native LINSTOR
# allocation, thin-pool physical use, and kubelet filesystem use are separate measurements. In
# particular, a DRBD mount must never become another physical disk in the filesystem totals above.
_PIRAEUS = [
    Rule.record(
        "disk_usage:pool_capacity_bytes",
        'max by (node, storage_pool) (linstor_storage_pool_capacity_total_bytes{driver="LVM_THIN"} > 0)',
    ),
    Rule.record(
        "disk_usage:pool_free_bytes",
        'max by (node, storage_pool) (linstor_storage_pool_capacity_free_bytes{driver="LVM_THIN"} >= 0)',
    ),
    Rule.record("disk_usage:pool_used_bytes", "disk_usage:pool_capacity_bytes - disk_usage:pool_free_bytes"),
    Rule.record(
        "disk_usage:volume_definition_size_bytes",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/piraeus-volume-definition-size.promql")
        ).read_text(),
    ),
    # PV claim references survive release; retain their resource/PV identity and phase rather than
    # silently attributing an old retained resource to a newly created PVC with the same name.
    Rule.record(
        "disk_usage:piraeus_volume:info",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/piraeus-volume-info.promql")).read_text(),
    ),
    Rule.record(
        "disk_usage:diskful_volume:info",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/piraeus-diskful-volume-state-info.promql")
        ).read_text(),
    ),
    Rule.record(
        "disk_usage:volume:info",
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/piraeus-diskful-volume-info.promql")
        ).read_text(),
    ),
    Rule.record(
        "disk_usage:volume_allocated_bytes",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/piraeus-volume-allocated.promql")).read_text(),
    ),
    Rule.record(
        "disk_usage:volume_capacity_bytes",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/piraeus-volume-capacity.promql")).read_text(),
    ),
    Rule.record(
        "disk_usage:pool_committed_bytes",
        "sum by (node, storage_pool) (disk_usage:volume_capacity_bytes)\n"
        "or on (node, storage_pool) (\n"
        "  (0 * disk_usage:pool_capacity_bytes) unless on (node, storage_pool) disk_usage:volume:info\n)",
    ),
    Rule.record(
        "disk_usage:pvc_used_bytes", "max by (namespace, persistentvolumeclaim, node) (kubelet_volume_stats_used_bytes)"
    ),
    Rule.alert(
        "PiraeusPoolExhaustionPredicted",
        "(predict_linear(disk_usage:pool_free_bytes[6h], 24 * 3600) < 0)\n"
        "and (disk_usage:pool_free_bytes / disk_usage:pool_capacity_bytes < 0.25)\n"
        "and (count_over_time(disk_usage:pool_free_bytes[6h]) >= 100)\n"
        "and (disk_usage:pool_free_bytes offset 6h)",
        for_="1h",
        labels={"severity": "warning"},
        summary="Piraeus pool {{ $labels.node }}/{{ $labels.storage_pool }} may fill within 24 hours",
        description=(
            "The pool has less than 25% physical free space and its six-hour linear trend predicts "
            "exhaustion within 24 hours. Check actual free space, recent migrations, snapshots and "
            "growth before acting. PVC commitments may exceed physical capacity by design."
        ),
    ),
]

_ROAMING_NODE_WORKLOAD = [
    # The stock rule has no node label. Join through kube_pod_info so a pod
    # on a tainted roaming node disappears from the alert, while the same
    # pod condition remains alertable on ordinary nodes.
    Rule.alert(
        "KubePodNotReady",
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/kube-pod-not-ready-warning.promql"))
        .read_text()
        .removesuffix("\n"),
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
        get_required_path(
            own_repo_rlocation("cluster/cdk8s/monitoring/kubelet-instance-unreachable-warning.promql")
        ).read_text(),
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
        get_required_path(own_repo_rlocation("cluster/cdk8s/monitoring/target-down-warning.promql")).read_text(),
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
        groups=[group("disk-usage", _DISK_USAGE), group("piraeus-storage", _PIRAEUS)],
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
        "io-pressure-alerts",
        metadata=ApiObjectMetadata(name="io-pressure-alerts", namespace=NAMESPACE),
        groups=[group("io-pressure", _IO_PRESSURE)],
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
