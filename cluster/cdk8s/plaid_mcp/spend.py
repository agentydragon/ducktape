"""The separate plaid-spend API and its shared encrypted card configuration."""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.plaid_mcp import db
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from finance.plaid.spend.settings import SpendSettings
from util.settings_contract import env_name

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/spend"
NAMESPACE = db.NAMESPACE
_NAME = "plaid-spend"
_HOST = "plaid-spend.allegedly.works"
_CONFIG_MAP = "plaid-spend-config"
_CARD_CONFIG = SecretRef(namespace=NAMESPACE, name="plaid-spend-cards")
_DB = db.SPEND
_CARD_CONFIG_PATH = SpendSettings.model_fields["cards_config_path"].default
_DESKTOP_OIDC_ISSUER = "https://auth.allegedly.works/application/o/plaid-spend-desktop/"
_DESKTOP_CLIENT_ID = "plaid-spend-desktop"
_WEB = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
)
_CONFIG = {
    env_name(SpendSettings, "cards_config_path"): str(_CARD_CONFIG_PATH),
    "PLAID_SPEND_API_OIDC_ISSUER": _DESKTOP_OIDC_ISSUER,
    "PLAID_SPEND_API_OIDC_CLIENT_ID": _DESKTOP_CLIENT_ID,
    "PLAID_SPEND_API_OIDC_DISCOVERED_ISSUER": _DESKTOP_OIDC_ISSUER,
    "PLAID_SPEND_API_OIDC_JWKS_URI": f"{_DESKTOP_OIDC_ISSUER}jwks/",
}


def _container_security_context() -> k8s.SecurityContext:
    return k8s.SecurityContext(
        allow_privilege_escalation=False,
        capabilities=k8s.Capabilities(drop=["ALL"]),
        run_as_group=1000,
        run_as_non_root=True,
        run_as_user=1000,
    )


def _resources() -> k8s.ResourceRequirements:
    return k8s.ResourceRequirements(
        requests={"memory": k8s.Quantity.from_string("128Mi"), "cpu": k8s.Quantity.from_string("50m")},
        limits={"memory": k8s.Quantity.from_string("512Mi"), "cpu": k8s.Quantity.from_string("500m")},
    )


def _deployment(chart: Chart) -> None:
    health = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_WEB.pod_port))
    k8s.KubeServiceAccount(
        chart,
        "service-account",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE),
        automount_service_account_token=False,
    )
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_WEB.pods.selector,
            annotations={
                "description": (
                    "Separate Plaid spend API; reads one SOPS-managed shared card configuration and"
                    " subscribes to committed plaid_spend_changed invalidations."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_WEB.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_WEB.pods.selector),
                spec=k8s.PodSpec(
                    service_account_name=_NAME,
                    image_pull_secrets=[k8s.LocalObjectReference(name="forgejo-images-creds")],
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    volumes=[k8s.Volume(name="cards", secret=k8s.SecretVolumeSource(secret_name=_CARD_CONFIG.name))],
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image="git.allegedly.works/ducktape-ci/plaid-spend:unset",
                            image_pull_policy="Always",
                            security_context=_container_security_context(),
                            ports=[_WEB.port.k8s_container_port()],
                            env=[
                                *(
                                    k8s.EnvVar(
                                        name=key,
                                        value_from=k8s.EnvVarSource(
                                            config_map_key_ref=k8s.ConfigMapKeySelector(name=_CONFIG_MAP, key=key)
                                        ),
                                    )
                                    for key in _CONFIG
                                ),
                                _DB.key("DATABASE_URL").env_var("DATABASE_URL"),
                            ],
                            volume_mounts=[
                                k8s.VolumeMount(name="cards", mount_path=str(_CARD_CONFIG_PATH.parent), read_only=True)
                            ],
                            resources=_resources(),
                            readiness_probe=k8s.Probe(http_get=health, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(http_get=health, initial_delay_seconds=20, period_seconds=20),
                        )
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    k8s.KubeConfigMap(chart, "config", metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAMESPACE), data=_CONFIG)
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_WEB.name, namespace=NAMESPACE, labels=_WEB.labels),
        spec=k8s.ServiceSpec(selector=_WEB.pods.selector, ports=[_WEB.port.k8s_service_port()], type="ClusterIP"),
    )
    https_route(
        chart,
        "public-route",
        metadata=ApiObjectMetadata(
            name=_NAME,
            namespace=NAMESPACE,
            annotations={"description": "Public entry point for the Authentik-authenticated Plaid Spend API."},
        ),
        hostnames=[_HOST],
        backend=_WEB,
        listener=None,
    )
    NetworkPolicy(
        chart,
        "ingress-policy",
        metadata=ApiObjectMetadata(
            name="plaid-spend-ingress",
            namespace=NAMESPACE,
            annotations={"description": "Only the cluster Gateway may reach the Plaid spend API."},
        ),
        endpoint_selector=_WEB.pods.selector,
        ingress=[IngressRule.from_gateway(_WEB.pod_port)],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
