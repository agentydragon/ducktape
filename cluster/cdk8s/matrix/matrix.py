"""Matrix: Synapse (Helm) with its CNPG Postgres, the Element web client, and their routes.

Hand-written beside the generated output: the SOPS Secrets (signing key, registration,
macaroon and Redis secrets, admin and bot credentials). Matrix OIDC/SSO: the sso-providers
Terraform manages the Authentik provider and writes the `matrix-oidc-config` Secret, which
Reflector copies into this namespace.
"""

from __future__ import annotations

import json
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallStrategy,
    HelmReleaseSpecInstallStrategyName,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeStrategy,
    HelmReleaseSpecUpgradeStrategyName,
    HelmReleaseSpecValuesFrom,
    HelmReleaseSpecValuesFromKind,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesMatches,
    HttpRouteSpecRulesMatchesPath,
    HttpRouteSpecRulesMatchesPathType,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.gateway import cluster_gateway_parent_ref, https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/matrix"
NAMESPACE = "matrix"
SYNAPSE = "matrix-synapse"
_NAME = "matrix"
_DB_NAME = "matrix-db"
_ZONE = "hil-ovh"
_HELM_REPOSITORY = "ananace-charts"
# renovate: datasource=docker depName=matrixdotorg/synapse
_SYNAPSE_TAG = "v1.160.0"
_SYNAPSE_PORT = 8008
_ELEMENT = "element-web"
_ELEMENT_LABELS = {"app.kubernetes.io/name": _ELEMENT}
_ELEMENT_CONFIG_MAP = "element-web-config"
_SOPS_FILES = (
    "synapse-signing-key.sops.yaml",
    "synapse-registration-secret.sops.yaml",
    "synapse-macaroon-secret.sops.yaml",
    "synapse-redis-password.sops.yaml",
    "synapse-admin-credentials.sops.yaml",
    "public-coder-agent-matrix-bot-password.sops.yaml",
)

_ELEMENT_CONFIG = {
    "default_server_config": {
        "m.homeserver": {"base_url": "https://matrix.allegedly.works", "server_name": "allegedly.works"}
    },
    "brand": "Element",
    "integrations_ui_url": "https://scalar.vector.im/",
    "integrations_rest_url": "https://scalar.vector.im/api",
    "integrations_widgets_urls": [
        "https://scalar.vector.im/_matrix/integrations/v1",
        "https://scalar.vector.im/api",
        "https://scalar-staging.vector.im/_matrix/integrations/v1",
        "https://scalar-staging.vector.im/api",
        "https://scalar-staging.riot.im/scalar/api",
    ],
    "hosting_signup_link": "https://element.io/matrix-services?utm_source=element-web&utm_medium=web",
    "bug_report_endpoint_url": "https://element.io/bugreports/submit",
    "uisi_autorageshake_app": "element-auto-uisi",
    "showLabsSettings": True,
    "features": {},
    "map_style_url": "https://api.maptiler.com/maps/streets/style.json?key=fU3vlMsMn4Jb6dnEIFsx",
    "sso_redirect_options": {"immediate": True},
}


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "auto"},
        ),
    )


def _database(scope: Construct) -> None:
    Cluster(
        scope,
        "database",
        metadata=metadata(_DB_NAME, NAMESPACE),
        spec=ClusterSpec(
            # OVH-HA profile (docs/cnpg_conventions.md R2/R3), replacing the Proxmox-single
            # shape this had before the namespace was parked: Synapse's media store is on
            # SeaweedFS now, whose CSI node plugin only runs on the OVH nodes, so the app
            # moved there and R5 requires the database to follow it.
            instances=2,
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/zone": _ZONE},
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh", size="10Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            # CNPG auto-generates credentials in secret matrix-db-app
            bootstrap=ClusterSpecBootstrap(
                initdb=ClusterSpecBootstrapInitdb(
                    database="synapse", owner="synapse", locale_c_type="C", locale_collate="C"
                )
            ),
        ),
    )


def _synapse(scope: Construct) -> None:
    HelmRepository(
        scope,
        "helm-repository",
        metadata=metadata(_HELM_REPOSITORY, NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://ananace.gitlab.io/charts"),
    )
    HelmRelease(
        scope,
        "synapse",
        metadata=metadata(SYNAPSE, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            install=HelmReleaseSpecInstall(
                strategy=HelmReleaseSpecInstallStrategy(name=HelmReleaseSpecInstallStrategyName.RETRY_ON_FAILURE)
            ),
            upgrade=HelmReleaseSpecUpgrade(
                strategy=HelmReleaseSpecUpgradeStrategy(name=HelmReleaseSpecUpgradeStrategyName.RETRY_ON_FAILURE)
            ),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="matrix-synapse",
                    # renovate: datasource=helm depName=matrix-synapse registryUrl=https://ananace.gitlab.io/charts
                    version="3.12.37",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=_HELM_REPOSITORY,
                        namespace=NAMESPACE,
                    ),
                )
            ),
            values_from=[
                # OIDC provider configuration from SOPS-managed secret
                HelmReleaseSpecValuesFrom(
                    kind=HelmReleaseSpecValuesFromKind.SECRET, name="matrix-oidc-config", values_key="values.yaml"
                ),
                # Macaroon secret key (SOPS, synapse-macaroon-secret.sops.yaml)
                HelmReleaseSpecValuesFrom(
                    kind=HelmReleaseSpecValuesFromKind.SECRET,
                    name="synapse-macaroon-secret",
                    values_key="macaroon_secret_key",
                    target_path="config.macaroonSecretKey",
                ),
                # Registration shared secret (SOPS)
                HelmReleaseSpecValuesFrom(
                    kind=HelmReleaseSpecValuesFromKind.SECRET,
                    name="synapse-registration-secret",
                    values_key="registration_shared_secret",
                    target_path="config.registrationSharedSecret",
                ),
            ],
            values={
                "image": {"repository": "matrixdotorg/synapse", "tag": _SYNAPSE_TAG, "pullPolicy": "IfNotPresent"},
                # Server name - this is the domain used in Matrix user IDs (@user:allegedly.works)
                "serverName": "allegedly.works",
                # Public server name - the hostname where Synapse is publicly accessible.
                # The chart derives public_baseurl as https://<publicServerName>.
                "publicServerName": "matrix.allegedly.works",
                "resources": {
                    "limits": {"cpu": "1000m", "memory": "2Gi"},
                    "requests": {"cpu": "200m", "memory": "512Mi"},
                },
                # Media store on distributed storage — the AGENTS.md default for app data
                # volumes, and RWX, so Synapse is not pinned to whichever node owns a local
                # directory. Supersedes the old idea of synapse-s3-storage-provider against
                # SeaweedFS S3: that module never replaces the local media path (it is a
                # backup/retrieval layer needing a custom image and an eviction cron), while
                # the CSI class gives Synapse the filesystem it insists on directly.
                "persistence": {
                    "enabled": True,
                    "storageClass": "seaweedfs-ovh",
                    "size": "20Gi",
                    "accessMode": "ReadWriteMany",
                },
                "synapse": {
                    # Same zone as matrix-db (cnpg_conventions.md R5) and the only nodes where
                    # the SeaweedFS CSI node plugin runs, which the media store above needs.
                    # This is `synapse.nodeSelector`, not the chart's top-level one — the chart
                    # has no top-level key of that name, so a selector placed there is silently
                    # ignored (which is why the previous `region: proxmox` never pinned
                    # anything, and Synapse only landed on wyrm2 by chance).
                    "nodeSelector": {"topology.kubernetes.io/zone": _ZONE},
                    # Reloader: auto-restart pods when secrets change
                    "annotations": {"reloader.stakater.com/auto": "true"},
                },
                # macaroonSecretKey and registrationSharedSecret injected via valuesFrom;
                # extraConfig with oidc_providers is injected via valuesFrom from the
                # matrix-oidc-config Secret.
                "config": {"enableRegistration": False, "reportStats": False},
                # Signing key from SOPS (disable chart auto-generation)
                "signingkey": {
                    "job": {"enabled": False},
                    "existingSecret": "synapse-signing-key",
                    "existingSecretKey": "signing.key",
                },
                "service": {"type": "ClusterIP", "port": _SYNAPSE_PORT, "federation": {"enabled": True, "port": 8448}},
                # PostgreSQL via external CNPG cluster (matrix-db)
                "postgresql": {"enabled": False},
                "externalPostgresql": {
                    "host": f"{_DB_NAME}-rw",
                    "port": 5432,
                    "username": "synapse",
                    "database": "synapse",
                    "existingSecret": f"{_DB_NAME}-app",
                    "existingSecretPasswordKey": "password",
                },
                # Redis (required by chart even for single-instance setup)
                "redis": {
                    "enabled": True,
                    "auth": {
                        "enabled": True,
                        "existingSecret": "synapse-redis-password",
                        "existingSecretPasswordKey": "redis-password",
                    },
                    "master": {
                        # Keep Synapse's Redis in the same zone as Synapse; it is a subchart, so
                        # this key is separate from synapse.nodeSelector above. Without it the
                        # placement is luck, and a cross-site hop for every pub/sub round trip.
                        "nodeSelector": {"topology.kubernetes.io/zone": _ZONE},
                        "persistence": {"enabled": False},
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "128Mi"},
                        },
                    },
                },
                # Synapse workers (disabled for now, can be enabled for scaling)
                "workers": {"default": {"enabled": False}},
                "serviceAccount": {"create": True},
            },
        ),
    )


def _synapse_routes(scope: Construct) -> None:
    https_route(
        scope,
        "synapse-route",
        metadata=metadata(SYNAPSE, NAMESPACE),
        hostname="matrix.allegedly.works",
        backend=SYNAPSE,
        port=_SYNAPSE_PORT,
        hsts=False,
        listener=None,
    )
    # Federation and well-known endpoints on the apex domain. More specific path matches
    # take priority over the website catch-all route.
    HttpRoute(
        scope,
        "federation-route",
        metadata=metadata("matrix-federation", NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["allegedly.works"],
            rules=[
                HttpRouteSpecRules(
                    matches=[
                        HttpRouteSpecRulesMatches(
                            path=HttpRouteSpecRulesMatchesPath(
                                type=HttpRouteSpecRulesMatchesPathType.PATH_PREFIX, value=prefix
                            )
                        )
                    ],
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name=SYNAPSE, port=_SYNAPSE_PORT)],
                )
                for prefix in ("/_matrix", "/.well-known/matrix")
            ],
        ),
    )


def _element(scope: Construct) -> None:
    k8s.KubeConfigMap(
        scope,
        "element-config",
        metadata=k8s.ObjectMeta(name=_ELEMENT_CONFIG_MAP, namespace=NAMESPACE),
        data={"config.json": json.dumps(_ELEMENT_CONFIG, indent=2) + "\n"},
    )
    probe_action = k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_string("http"))
    k8s.KubeDeployment(
        scope,
        "element-deployment",
        metadata=k8s.ObjectMeta(name=_ELEMENT, namespace=NAMESPACE, labels=_ELEMENT_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_ELEMENT_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_ELEMENT_LABELS, annotations={"reloader.stakater.com/auto": "true"}),
                spec=k8s.PodSpec(
                    containers=[
                        k8s.Container(
                            name=_ELEMENT,
                            # renovate: datasource=docker
                            image="vectorim/element-web:v1.12.27",
                            ports=[k8s.ContainerPort(container_port=80, name="http", protocol="TCP")],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="config", mount_path="/app/config.json", sub_path="config.json", read_only=True
                                )
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("200m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                            ),
                            liveness_probe=k8s.Probe(
                                http_get=probe_action, initial_delay_seconds=10, period_seconds=30
                            ),
                            readiness_probe=k8s.Probe(
                                http_get=probe_action, initial_delay_seconds=5, period_seconds=10
                            ),
                            security_context=k8s.SecurityContext(allow_privilege_escalation=False),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_ELEMENT_CONFIG_MAP))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "element-service",
        metadata=k8s.ObjectMeta(name=_ELEMENT, namespace=NAMESPACE, labels=_ELEMENT_LABELS),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            ports=[
                k8s.ServicePort(port=80, target_port=k8s.IntOrString.from_string("http"), protocol="TCP", name="http")
            ],
            selector=_ELEMENT_LABELS,
        ),
    )
    https_route(
        scope,
        "element-route",
        metadata=metadata(_ELEMENT, NAMESPACE),
        hostname="chat.allegedly.works",
        backend=_ELEMENT,
        port=80,
        hsts=False,
        listener=None,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _database(chart)
    _synapse(chart)
    _synapse_routes(chart)
    _element(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml", *_SOPS_FILES])
    )


def matrix(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization) -> Kustomization:
    name = "matrix"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=SYNAPSE, namespace=NAMESPACE
                )
            ],
            depends_on=flux_kustomization_depends_on_many(cnpg),
        ),
    )
