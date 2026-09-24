"""kube-prometheus-stack (Prometheus Operator, Alertmanager, node-exporter,
kube-state-metrics), the control-plane scrape token, and Prometheus's ingress policy.

Hand-written beside the generated output: `grafana-admin-password.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart, JsonPatch
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
    HelmReleaseSpecUpgradeRemediation,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "monitoring-stack"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/monitoring/stack"
_NAMESPACE = "monitoring"
_HELM_REPOSITORY = "prometheus-community"
_CONTROL_PLANE_TOKEN = "alloy-control-plane-token"
_PROMETHEUS_PORT = 9090

# A kube-controller-manager / kube-scheduler ServiceMonitor: bearer auth with the
# Alloy-owned token, the cluster CA for TLS.
_CONTROL_PLANE_SERVICE_MONITOR = {
    "authorization": {"type": "Bearer", "credentials": {"name": _CONTROL_PLANE_TOKEN, "key": "token"}},
    "tlsConfig": {"insecureSkipVerify": True, "ca": {"configMap": {"name": "kube-root-ca.crt", "key": "ca.crt"}}},
}


def _flux_state_metrics(
    group: str, version: str, kind: str, prefix: str, *extra_metrics: dict[str, object]
) -> dict[str, object]:
    """kube-state-metrics custom-resource-state metrics for one Flux kind: its Ready
    condition and `spec.suspend`."""
    return {
        "groupVersionKind": {"group": group, "version": version, "kind": kind},
        "labelsFromPath": {"namespace": ["metadata", "namespace"], "name": ["metadata", "name"]},
        "metrics": [
            {
                "name": f"flux_{prefix}_ready",
                "help": f"Flux {kind} Ready condition status.",
                "each": {
                    "type": "StateSet",
                    "stateSet": {
                        "labelName": "status",
                        "path": ["status", "conditions", "[type=Ready]", "status"],
                        "list": ["True", "False", "Unknown"],
                    },
                },
            },
            # Flux does not clear the Ready condition when a Kustomization is
            # suspended, so a deliberately-disabled service reports Ready=False
            # forever. Exposing spec.suspend lets the *NotReady alerts exclude
            # them; without it they are indistinguishable from real failures.
            {
                "name": f"flux_{prefix}_suspended",
                "help": f"Whether the Flux {kind} is suspended (spec.suspend).",
                "each": {"type": "Gauge", "gauge": {"path": ["spec", "suspend"], "nilIsZero": True}},
            },
            *extra_metrics,
        ],
    }


def _alertmanager_config() -> dict[str, object]:
    return {
        "global": {"resolve_timeout": "5m"},
        "inhibit_rules": [
            {
                "source_matchers": ["severity = critical"],
                "target_matchers": ["severity =~ warning|info"],
                "equal": ["namespace", "alertname"],
            },
            {
                "source_matchers": ["severity = warning"],
                "target_matchers": ["severity = info"],
                "equal": ["namespace", "alertname"],
            },
            {
                "source_matchers": ["alertname = InfoInhibitor"],
                "target_matchers": ["severity = info"],
                "equal": ["namespace"],
            },
            {"target_matchers": ["alertname = InfoInhibitor"]},
        ],
        # ntfy is the DEFAULT receiver and suppression is an explicit denylist below.
        #
        # This used to be an allowlist (`receiver: "null"` by default, one child route
        # naming the alertnames that page). That fails silently in the direction that
        # matters: an alert not named in the allowlist is discarded with no signal
        # anywhere, so a newly added rule looks healthy and fires into nothing. It cost
        # a 45h haku-console connector outage whose alert had been built, verified
        # firing, and never routed; and it silently dropped ControlPlaneLeasePutLatency
        # when that alert was renamed and the allowlist regex kept the old names.
        # Defaulting to ntfy makes the failure mode "too loud" rather than "silent".
        #
        # The denylist started as an exact reproduction of the old delivered set (116
        # names, 21 of them critical) so the inversion itself was a behavioral no-op.
        # It is now being pruned deliberately, a batch at a time.
        #
        # Every candidate is checked against 7 DAYS of ALERTS history before removal,
        # not a spot check. A 3h sample said TanaMcpFacadeProbeStale was quiet; over 7d
        # it fires 7286 min (~72% of the week), and enabling it would have planted a
        # permanently-firing alert in a channel whose daily quota is the thing that
        # broke on 2026-08-05. Query:
        #   max by (alertname) (count_over_time(ALERTS{alertstate="firing"}[7d]))
        #
        # Deviation worth knowing: a NEW alertname, from a chart upgrade or a new rule,
        # is not in the denylist and therefore pages. That asymmetry is the point of the
        # inversion -- silence is the failure mode that cost a 45h connector outage.
        "route": {
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "2h",
            "receiver": "ntfy",
            # One alert per ntfy message. ntfy caps a published message at
            # ~4KB; a group_by of ["namespace"] bundled every flux-system alert
            # (21+ FluxKustomizationNotReady during a storm) into one notification
            # that, expanded by the X-Template "alertmanager" renderer, exceeded
            # the cap and was rejected with HTTP 400 code 40041 ("message or title
            # is too large after replacing template"). Alertmanager treats 4xx as
            # unrecoverable, so alerts were silently dropped exactly when many were
            # firing. group_by ["..."] disables aggregation so each alert is its
            # own small message.
            #
            # Gotcha: this trades a size limit for a rate limit. One message per
            # alert instance against ntfy.sh's free daily quota is what produced
            # the 429 (code 42908) outage that took paging down for 23h on
            # 2026-08-05. The self-hosted instance has no hosted-service quota.
            "group_by": ["..."],
            "routes": [
                # PERMANENT. Watchdog fires continuously by design and is the heartbeat
                # for the external dead-man's switch (see cluster/docs/plan.md); paging
                # on it would be backwards. InfoInhibitor exists only to feed the
                # inhibit_rules above.
                {"receiver": "null", "matchers": ['alertname =~ "Watchdog|InfoInhibitor"']},
                # NOT NOISE -- UNFIXED PROBLEMS. Everything below fires ~continuously
                # because something is genuinely broken and has been for days. Each
                # line is a to-do, not a policy: fix the cause, delete the entry. The
                # number is minutes firing over the 7 days to 2026-08-06.
                #
                # Do NOT add to this list to quieten a new alert. That is how the
                # 2026-08-05 paging outage happened -- suppressed alerts for
                # deliberately-suspended services became a permanent floor that
                # exhausted the daily quota and took *all* paging down for 23h.
                # TargetDown is suppressed for ONE job rather than wholesale. Its other
                # causes are gone: kubelet by the roaming taint exclusion (#3815);
                # litellm-chatgpt with the suspension (#3812); and seaweedfs-volume-peer
                # twice over -- #3818 aligned the ssd metricsPort so the scrape would
                # succeed, then #3819 deleted the ServiceMonitor that was scraping that
                # topology-less Service at all, which is what actually retired the job.
                # (The operator does not recreate that ServiceMonitor: it emits them per
                # volumeTopology group, and the flat spec.volume is deliberately omitted.
                # #3818 was therefore not required for this alert.) node-exporter is
                # the last one: its `up` series carry no `node` label, only `instance`, so
                # excluding roaming nodes there needs an instance -> IP -> node join
                # through kube_node_status_addresses, which is a lot of dense PromQL
                # inside a rule that guards every scrape target in the cluster.
                #
                # Narrow beats wholesale here: a NEW target going down now pages, which
                # is the entire point of the alert and was not true while the alertname
                # was denied outright.
                {"receiver": "null", "matchers": ['alertname = "TargetDown"', 'job = "node-exporter"']},
                {
                    "receiver": "null",
                    "matchers": [
                        # 10049 -- CDI storage profiles incomplete (info).
                        #  9926 -- version drift: iguana/rugged/wyrm2 on v1.36.2 vs v1.35.1 elsewhere.
                        #  7935 -- wyrm2's disk is genuinely nearly full.
                        #  7286 -- Tana probe stale.
                        #  4234 -- CPU throttling (info).
                        #  3231 -- a quota fully used (info).
                        #   614 -- containers stuck Waiting.
                        #   157 -- major page faults.
                        'alertname =~ "CDIStorageProfilesIncomplete|KubeVersionMismatch|NodeFilesystemAlmostOutOfSpace'
                        "|TanaMcpFacadeProbeStale|CPUThrottlingHigh|KubeQuotaFullyUsed|KubeContainerWaiting"
                        '|NodeMemoryMajorPagesFaults"'
                    ],
                },
                # Same category, but specifically the fallout of roaming laptops
                # leaving the cluster. Unlike KubeNodeNotReady/KubeNodeUnreachable
                # (now forked in cluster/cdk8s/monitoring/rules.py
                # and excluded by taint), these carry no `node` label -- only namespace and
                # pod -- so there is no way to say "on a roaming node" at the routing
                # layer. Suppressing by namespace would blind us to real failures in
                # loki/egress-proxy/gecko. Re-evaluate once the laptops stop flapping;
                # if still needed, the fix is forking these rules the same way.
                {
                    "receiver": "null",
                    "matchers": [
                        'alertname =~ "KubePodNotReady|KubePodCrashLooping|KubeJobFailed|KubeDaemonSetRolloutStuck'
                        '|KubeDeploymentReplicasMismatch|KubeDeploymentRolloutStuck"'
                    ],
                },
                # The pager reporting on its own delivery. These fired for the whole
                # 23h outage and could not be delivered, because delivery was the
                # broken thing -- routing them adds quota pressure during exactly the
                # incident where quota is the problem. The external dead-man's switch
                # is the real fix. AlertmanagerClusterDown / ...Crashlooping / ...
                # ConfigInconsistent / ...FailedReload / ...MembersInconsistent DO page
                # (enabled in #3800/#3802): those describe a broken Alertmanager that
                # can still send.
                {
                    "receiver": "null",
                    "matchers": ['alertname =~ "AlertmanagerClusterFailedToSendAlerts|AlertmanagerFailedToSendAlerts"'],
                },
            ],
        },
        "receivers": [
            {"name": "null"},
            {
                "name": "ntfy",
                "webhook_configs": [
                    {
                        "send_resolved": True,
                        "url_file": "/etc/alertmanager/secrets/alertmanager-ntfy-webhook/address",
                        # Select ntfy's built-in alertmanager message template (firing/
                        # resolved formatting) so previews show a readable title + body
                        # instead of the raw webhook JSON. Sent as a header rather than a
                        # URL query param. The topic URL and bearer token are supplied by
                        # the ESO-managed Secret mounted below.
                        "http_config": {
                            "http_headers": {"X-Template": {"values": ["alertmanager"]}},
                            "bearer_token_file": "/etc/alertmanager/secrets/alertmanager-ntfy-webhook/token",
                        },
                    }
                ],
            },
        ],
        "templates": ["/etc/alertmanager/config/*.tmpl"],
    }


def _values() -> dict[str, object]:
    return {
        # Owned by the monitoring-crds Kustomization instead, so a ServiceMonitor
        # depends on the CRD existing rather than on Prometheus being healthy.
        # These come from the subchart's crds/ directory, which is outside the Helm
        # release manifest — disabling it stops Helm applying them, it does not
        # delete them.
        "crds": {"enabled": False},
        # Shorten resource names: "kube-prometheus-stack-*" → "monitoring-*"
        "fullnameOverride": "monitoring",
        # Remove redundant "-prometheus"/"-alertmanager" suffix from CR names
        # (avoids e.g. "prometheus-monitoring-prometheus" → "prometheus-monitoring")
        "cleanPrometheusOperatorObjectNames": True,
        "defaultRules": {
            "create": True,
            # This is a wyrm2-only volume for host-local Colibri/model data, not
            # storage used by Kubernetes workloads. Kubernetes filesystem alerts
            # should not care about it; node-exporter still exposes its raw metrics.
            "node": {"fsSelector": 'fstype!="",mountpoint!="/var/lib/colibri"'},
            # Forked into cluster/cdk8s/monitoring/rules.py so
            # roaming laptops (iguana/rugged) can be excluded by taint. Denying these two by
            # alertname in the route below would also have silenced them for the
            # control-plane nodes, which is the opposite of what we want.
            "disabled": {
                "KubeNodeNotReady": True,
                "KubeNodeUnreachable": True,
                "KubePodNotReady": True,
                "KubeletInstanceUnreachable": True,
                "TargetDown": True,
            },
            "rules": {
                "alertmanager": True,
                # Static Talos etcd endpoints are scraped by monitoring/etcd; keep the
                # chart's stock etcd rule bundle disabled until the scrape is verified.
                "etcd": False,
                "configReloaders": True,
                "general": True,
                "k8s": True,
                "kubeApiserverAvailability": True,
                "kubeApiserverSlos": True,
                "kubelet": True,
                # kube-proxy is intentionally absent: Cilium runs kube-proxy replacement,
                # so the stock KubeProxyDown rule would always be noise here.
                "kubeProxy": False,
                "kubePrometheusGeneral": True,
                "kubePrometheusNodeRecording": True,
                "kubernetesApps": True,
                "kubernetesResources": True,
                "kubernetesStorage": True,
                "kubernetesSystem": True,
                "kubeScheduler": True,
                "kubeStateMetrics": True,
                "network": True,
                "node": True,
                "nodeExporterAlerting": True,
                "nodeExporterRecording": True,
                "prometheus": False,  # Prometheus disabled; Mimir Ruler evaluates rules
                "prometheusOperator": True,
            },
        },
        "alertmanager": {
            "enabled": True,
            "config": _alertmanager_config(),
            "ingress": {"enabled": False},
            "alertmanagerSpec": {
                "replicas": 2,
                "secrets": ["alertmanager-ntfy-webhook"],
                "storage": {
                    "volumeClaimTemplate": {
                        "metadata": {"name": "db"},
                        "spec": {
                            "storageClassName": "local-path-ovh",
                            "accessModes": ["ReadWriteOnce"],
                            "resources": {"requests": {"storage": "1Gi"}},
                        },
                    }
                },
                # Chart auto-generates podAntiAffinity when replicas > 1
                "nodeSelector": {"topology.kubernetes.io/zone": "hil-ovh"},
                # The replica with its local PVC on a control plane must survive the
                # default taint until monitoring-state migration. Prefer workers for
                # any placement not constrained by that PVC.
                "tolerations": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
                ],
                "affinity": {
                    "nodeAffinity": {
                        "preferredDuringSchedulingIgnoredDuringExecution": [
                            {
                                "weight": 100,
                                "preference": {
                                    "matchExpressions": [
                                        {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}
                                    ]
                                },
                            }
                        ]
                    }
                },
                "resources": {
                    "requests": {"cpu": "10m", "memory": "64Mi"},
                    "limits": {"cpu": "100m", "memory": "128Mi"},
                },
                "retention": "120h",
            },
        },
        # Grafana disabled — managed by grafana-operator (cluster/k8s/monitoring/grafana-instance/).
        # Dashboards, datasources, and service accounts are GrafanaDashboard/GrafanaDatasource/
        # GrafanaServiceAccount CRs. JWT auth eliminates admin password bootstrap dependency.
        "grafana": {"enabled": False},
        "prometheusOperator": {
            "enabled": True,
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}},
            "admissionWebhooks": {
                "patch": {
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    }
                }
            },
        },
        # Prometheus disabled — Alloy scrapes metrics and pushes to Mimir.
        # Mimir Ruler evaluates alerting/recording rules.
        # Operator kept for CRD management (ServiceMonitor, PrometheusRule, etc.).
        "prometheus": {
            "enabled": False,
            # Control-plane ServiceMonitors use the Alloy-owned token below instead.
            "serviceAccount": {"createTokenSecret": False},
        },
        "prometheus-node-exporter": {
            "fullnameOverride": "prometheus-node-exporter",
            # Must match ducktape.raplMetrics.gid on Rugged. The host grants this
            # group read access only to root-owned Intel RAPL energy_uj files; the
            # exporter remains UID 65534 and has no added capabilities.
            "securityContext": {"fsGroup": 45321, "runAsGroup": 45321, "runAsNonRoot": True, "runAsUser": 65534},
        },
        "nodeExporter": {
            "enabled": True,
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
        },
        "kube-state-metrics": {
            "fullnameOverride": "kube-state-metrics",
            # Export the node media tier used by the local-path storage classes. The
            # default kube-state-metrics configuration intentionally omits arbitrary
            # Kubernetes labels; this narrow allowlist lets dashboards join node
            # device/cAdvisor metrics to the authoritative hdd/ssd classification
            # without maintaining a node-name regex.
            "metricLabelsAllowlist": ["nodes=[storage.allegedly.works/tier]"],
            "rbac": {
                "extraRules": [
                    {"apiGroups": [group], "resources": [resource], "verbs": ["list", "watch"]}
                    for group, resource in (
                        ("apiextensions.k8s.io", "customresourcedefinitions"),
                        ("kustomize.toolkit.fluxcd.io", "kustomizations"),
                        ("helm.toolkit.fluxcd.io", "helmreleases"),
                        ("source.toolkit.fluxcd.io", "gitrepositories"),
                    )
                ]
            },
            "customResourceState": {
                "enabled": True,
                "config": {
                    "kind": "CustomResourceStateMetrics",
                    "spec": {
                        "resources": [
                            _flux_state_metrics("kustomize.toolkit.fluxcd.io", "v1", "Kustomization", "kustomization"),
                            _flux_state_metrics("helm.toolkit.fluxcd.io", "v2", "HelmRelease", "helmrelease"),
                            _flux_state_metrics(
                                "source.toolkit.fluxcd.io",
                                "v1",
                                "GitRepository",
                                "gitrepository",
                                {
                                    "name": "flux_gitrepository_artifact_size_bytes",
                                    "help": "Flux GitRepository artifact size in bytes.",
                                    "each": {
                                        "type": "Gauge",
                                        "gauge": {"path": ["status", "artifact"], "valueFrom": ["size"]},
                                    },
                                },
                            ),
                        ]
                    },
                },
            },
            "resources": {"requests": {"cpu": "10m", "memory": "128Mi"}, "limits": {"cpu": "100m", "memory": "512Mi"}},
        },
        "kubeApiServer": {
            # Scraped natively from cluster/k8s/monitoring/alloy/config.alloy instead,
            # preserving the explicit auth and rule labels used by that configuration.
            "enabled": False
        },
        # Same as kubeApiServer: scraped natively in cluster/k8s/monitoring/alloy/config.alloy.
        "kubelet": {"enabled": False},
        "kubeControllerManager": {"enabled": True, "serviceMonitor": _CONTROL_PLANE_SERVICE_MONITOR},
        # `serviceMonitor.authorization: null` is patched in below.
        "coreDns": {"enabled": True, "serviceMonitor": {}},
        # Static Talos etcd endpoints are managed in cluster/generated/monitoring/etcd.
        "kubeEtcd": {"enabled": False},
        "kubeScheduler": {"enabled": True, "serviceMonitor": _CONTROL_PLANE_SERVICE_MONITOR},
        # kube-proxy is intentionally absent: Cilium runs kube-proxy replacement,
        # so do not create the stock kube-proxy Service/ServiceMonitor.
        "kubeProxy": {"enabled": False},
    }


def _prometheus_ingress_rule(namespace: str, app: str) -> k8s.NetworkPolicyIngressRule:
    return k8s.NetworkPolicyIngressRule(
        from_=[
            k8s.NetworkPolicyPeer(
                namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace}),
                pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": app}),
            )
        ],
        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_PROMETHEUS_PORT), protocol="TCP")],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeSecret(
        chart,
        "control-plane-token",
        metadata=k8s.ObjectMeta(
            name=_CONTROL_PLANE_TOKEN,
            namespace=_NAMESPACE,
            annotations={
                "description": "Long-lived service-account token for monitoring control-plane ServiceMonitors.",
                "kubernetes.io/service-account.name": "alloy",
            },
        ),
        type="kubernetes.io/service-account-token",
    )
    repository = HelmRepository(
        chart,
        "helm-repository",
        metadata=metadata(_HELM_REPOSITORY, "flux-system"),
        spec=HelmRepositorySpec(interval="12h", url="https://prometheus-community.github.io/helm-charts"),
    )
    release = helm_release(
        chart,
        "kube-prometheus-stack",
        _NAMESPACE,
        repository=repository,
        chart="kube-prometheus-stack",
        version="91.3.0",
        interval="30m",
        chart_interval="12h",
        timeout="10m",  # Prometheus stack upgrades can be slow
        install=HelmReleaseSpecInstall(
            # The `crds` subchart is disabled below; monitoring-crds owns them instead.
            # Kept as CreateReplace so any chart that does ship a crds/ directory is
            # updated rather than skipped.
            crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE,
            # TODO(roaming-nodes): Roaming nodes (iguana/rugged) are often offline,
            # so DaemonSets (node-exporter) have perpetually Pending pods. Helm waits
            # for all pods to be ready, causing timeout. disableWait is a blunt
            # workaround — need a general solution for every HelmRelease with a
            # DaemonSet. Options: taint roaming nodes + add tolerations to DaemonSets
            # that should run there, or use nodeAffinity to exclude roaming entirely.
            disable_wait=True,
            remediation=HelmReleaseSpecInstallRemediation(retries=3),
        ),
        upgrade=HelmReleaseSpecUpgrade(
            crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE,
            disable_wait=True,
            remediation=HelmReleaseSpecUpgradeRemediation(retries=3),
        ),
        values=_values(),
    )
    # CoreDNS serves metrics over plain HTTP on 9153 and requires no
    # authentication. Explicitly clear the chart's v91 default authorization
    # because this endpoint is scraped by Alloy without a bearer token. A patch,
    # because synthesis drops a None inside `values`.
    release.add_json_patch(JsonPatch.add("/spec/values/coreDns/serviceMonitor/authorization", None))
    # Restrict Prometheus API (port 9090) to authorized consumers.
    # Prometheus has no authentication; unrestricted access exposes full cluster
    # topology, node IPs, resource usage, and secret cardinality to any pod.
    k8s.KubeNetworkPolicy(
        chart,
        "prometheus-ingress",
        metadata=k8s.ObjectMeta(name="prometheus-ingress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": "prometheus"}),
            policy_types=["Ingress"],
            ingress=[
                # Grafana: datasource queries for dashboards
                _prometheus_ingress_rule(_NAMESPACE, "grafana"),
                # Alertmanager: Prometheus pushes alerts to Alertmanager; allow return traffic
                _prometheus_ingress_rule(_NAMESPACE, "alertmanager"),
                # Gatus: health check probes
                _prometheus_ingress_rule("gatus", "gatus"),
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml", "grafana-admin-password.sops.yaml"]),
    )


def monitoring_stack(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    monitoring_namespace: Kustomization,
    monitoring_crds: Kustomization,
    ntfy: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-stack",
        artifact,
        wait=None,
        decryption=SOPS_DECRYPTION,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="v1", kind="Secret", name="alloy-control-plane-token", namespace="monitoring"
            ),
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2",
                kind="HelmRelease",
                name="kube-prometheus-stack",
                namespace="monitoring",
            ),
        ],
        # The built-in Secret health check only checks existence. This CEL check waits
        # for the service-account token controller to populate data.token.
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="v1", kind="Secret", current="has(data.token) && data.token != ''"
            )
        ],
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            monitoring_namespace,
            # The chart's Prometheus/Alertmanager CRs are rejected at admission until
            # the CRDs exist, and the chart no longer installs them itself.
            monitoring_crds,
            ntfy,
            external_secrets_config,
        ),
    )
