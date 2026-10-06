"""Matrix: Synapse (Helm) with its CNPG Postgres, the Element web client, and their routes.

Hand-written beside the generated output: the SOPS Secrets (signing key, registration,
macaroon and Redis secrets, admin and bot credentials). Matrix OIDC/SSO: the sso-providers
Terraform manages the Authentik provider and writes the `matrix-oidc-config` Secret, which
Reflector copies into this namespace.
"""

from __future__ import annotations

import json

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallStrategy,
    HelmReleaseSpecInstallStrategyName,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeStrategy,
    HelmReleaseSpecUpgradeStrategyName,
    HelmReleaseSpecValuesFrom,
    HelmReleaseSpecValuesFromKind,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from gateway_api_crds.io.k8s.networking.gateway import HttpRouteSpecRules, HttpRouteSpecRulesBackendRefs

from cluster.cdk8s import cnpg, namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import cluster_gateway_parent_ref, https_route
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.gateway_api.http_route import HttpRoute, RouteMatch
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/matrix"
NAMESPACE = "matrix"
SYNAPSE = "matrix-synapse"
HOSTNAME = "matrix.allegedly.works"
ELEMENT_HOSTNAME = "chat.allegedly.works"
_NAME = "matrix"
DATABASE = cnpg.PostgresRef.generated(name="matrix-db", namespace=NAMESPACE)
_HELM_REPOSITORY = "ananace-charts"
# The chart's main Service: its selector is the chart name, the release and the component.
SYNAPSE_HTTP = ServiceRef(
    name=SYNAPSE,
    port=Port(name="http", number=8008),
    pods=Pods(
        namespace=NAMESPACE,
        labels=(
            ("app.kubernetes.io/name", "matrix-synapse"),
            ("app.kubernetes.io/instance", SYNAPSE),
            ("app.kubernetes.io/component", "synapse"),
        ),
    ),
)
_ELEMENT = "element-web"
_ELEMENT_HTTP = ServiceRef(
    name=_ELEMENT,
    port=Port(name="http", number=80),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", _ELEMENT),)),
)
_ELEMENT_CONFIG_MAP = "element-web-config"
SOPS_FILES = (
    "synapse-signing-key.sops.yaml",
    "synapse-registration-secret.sops.yaml",
    "synapse-macaroon-secret.sops.yaml",
    "synapse-redis-password.sops.yaml",
    "synapse-admin-credentials.sops.yaml",
    "public-coder-agent-matrix-bot-password.sops.yaml",
)

_ELEMENT_CONFIG = {
    "default_server_config": {"m.homeserver": {"base_url": f"https://{HOSTNAME}", "server_name": "allegedly.works"}},
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
    namespaces.namespace(scope, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        ref=DATABASE,
        image_name=None,
        # OVH-HA profile (docs/cnpg_conventions.md R2/R3), replacing the Proxmox-single
        # shape this had before the namespace was parked: Synapse's media store is on
        # SeaweedFS now, whose CSI node plugin only runs on the OVH nodes, so the app
        # moved there and R5 requires the database to follow it.
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh",
        size="10Gi",
        initdb=cnpg.same_owner_initdb("synapse", locale_c_type="C", locale_collate="C"),
        wal_archive=False,
    )


def _synapse(scope: Construct) -> None:
    helm_release(
        scope,
        SYNAPSE,
        NAMESPACE,
        repository=https_helm_repository(scope, _HELM_REPOSITORY, NAMESPACE, url="https://ananace.gitlab.io/charts"),
        chart="matrix-synapse",
        version="3.12.37",
        interval="15m",
        install=HelmReleaseSpecInstall(
            strategy=HelmReleaseSpecInstallStrategy(name=HelmReleaseSpecInstallStrategyName.RETRY_ON_FAILURE)
        ),
        upgrade=HelmReleaseSpecUpgrade(
            strategy=HelmReleaseSpecUpgradeStrategy(name=HelmReleaseSpecUpgradeStrategyName.RETRY_ON_FAILURE)
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
            "image": {"repository": "matrixdotorg/synapse", "tag": "v1.160.0", "pullPolicy": "IfNotPresent"},
            # Server name - this is the domain used in Matrix user IDs (@user:allegedly.works)
            "serverName": "allegedly.works",
            # Public server name - the hostname where Synapse is publicly accessible.
            # The chart derives public_baseurl as https://<publicServerName>.
            "publicServerName": HOSTNAME,
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
                "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR
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
            "service": {"type": "ClusterIP", "port": SYNAPSE_HTTP.port.number},
            # PostgreSQL via external CNPG cluster (matrix-db)
            "postgresql": {"enabled": False},
            "externalPostgresql": {
                "host": DATABASE.rw.name,
                "port": DATABASE.rw.port.number,
                "username": "synapse",
                "database": "synapse",
                "existingSecret": DATABASE.app_secret.name,
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
                    "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
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
    )


def _synapse_routes(scope: Construct) -> None:
    https_route(
        scope,
        "synapse-route",
        metadata=ApiObjectMetadata(name="synapse", namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=SYNAPSE_HTTP,
        hsts=False,
        listener=None,
    )
    # Federation and well-known endpoints on the apex domain. More specific path matches
    # take priority over the website catch-all route. Two independent rules, not one
    # https_route() call: each needs its own single-prefix match (Gateway API's own
    # per-rule structure), and both happen to share this backend.
    HttpRoute(
        scope,
        "federation-route",
        metadata=ApiObjectMetadata(name="federation", namespace=NAMESPACE),
        parent_refs=[cluster_gateway_parent_ref()],
        hostnames=["allegedly.works"],
        rules=[
            HttpRouteSpecRules(
                matches=[RouteMatch.path_prefix(prefix)],
                backend_refs=[HttpRouteSpecRulesBackendRefs(name=SYNAPSE_HTTP.name, port=SYNAPSE_HTTP.port.number)],
            )
            for prefix in ("/_matrix", "/.well-known/matrix")
        ],
    )


def _element(scope: Construct) -> None:
    k8s.KubeConfigMap(
        scope,
        "element-config",
        metadata=k8s.ObjectMeta(name=_ELEMENT_CONFIG_MAP, namespace=NAMESPACE),
        data={"config.json": json.dumps(_ELEMENT_CONFIG, indent=2) + "\n"},
    )
    probe_action = k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_string(_ELEMENT_HTTP.port.name))
    k8s.KubeDeployment(
        scope,
        "element-deployment",
        metadata=k8s.ObjectMeta(name=_ELEMENT, namespace=NAMESPACE, labels=_ELEMENT_HTTP.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_ELEMENT_HTTP.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_ELEMENT_HTTP.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    containers=[
                        k8s.Container(
                            name=_ELEMENT,
                            image="vectorim/element-web:v1.12.27",
                            ports=[_ELEMENT_HTTP.port.k8s_container_port()],
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
        metadata=k8s.ObjectMeta(name=_ELEMENT_HTTP.name, namespace=NAMESPACE, labels=_ELEMENT_HTTP.labels),
        spec=k8s.ServiceSpec(
            type="ClusterIP", ports=[_ELEMENT_HTTP.port.k8s_service_port()], selector=_ELEMENT_HTTP.pods.selector
        ),
    )
    https_route(
        scope,
        "element-route",
        metadata=ApiObjectMetadata(name=_ELEMENT, namespace=NAMESPACE),
        hostnames=[ELEMENT_HOSTNAME],
        backend=_ELEMENT_HTTP,
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


def matrix(chart: Chart, directory: RenderedDirectory, cnpg: Kustomization) -> Kustomization:
    name = "matrix"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg),
    )
