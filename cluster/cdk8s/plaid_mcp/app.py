"""plaid-mcp's web UI Deployment, webhook receiver and daily sync CronJob,
their shared config, the Secret-managing RBAC, the Service and ingress policy.

The images' tags are the placeholder "unset"; the hand-written `image-pins/kustomization.yaml`
overrides them at `kustomize build` time via Flux's image-automation markers
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker). `plaid-client-credentials.sops.yaml`
stays hand-written beside the generated output in the flat Plaid manifest directory.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount, k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.plaid_mcp.db import NAMESPACE, POSTGRES
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp"
_NAME = "plaid-mcp"
_CONFIG_MAP = "plaid-mcp-config"
_SECRET_MANAGER = "plaid-mcp-secret-manager"
_CREDENTIALS = SecretRef(namespace=NAMESPACE, name="plaid-client-credentials")
_OIDC_CREDENTIALS = SecretRef(namespace=NAMESPACE, name="plaid-link-oidc-config")
# The link web UI authenticates browser sessions with Authentik OIDC.
_WEB = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
)
_OIDC_ISSUER = "https://auth.allegedly.works/application/o/plaid-link/"
_OIDC_CREDENTIALS_NAME = "plaid-link-oidc-config"
_OIDC_READER = "plaid-link-oidc-reader"
_WEBHOOK_HOST = "plaid-mcp.allegedly.works"
_WEBHOOK_URL = f"https://{_WEBHOOK_HOST}/webhooks/plaid"
_CONFIG = {
    "PLAID_MCP_PLAID_ENV": "production",
    "PLAID_MCP_PUBLIC_BASE_URL": "https://plaid-mcp.allegedly.works",
    "PLAID_MCP_WEBHOOK_URL": _WEBHOOK_URL,
    "PLAID_MCP_TRANSACTION_DAYS": "730",
    "PLAID_MCP_INVESTMENT_TRANSACTION_DAYS": "730",
}


def _env() -> list[k8s.EnvVar]:
    """The environment the web UI and the sync job share."""
    return [
        *(
            k8s.EnvVar(
                name=key,
                value_from=k8s.EnvVarSource(config_map_key_ref=k8s.ConfigMapKeySelector(name=_CONFIG_MAP, key=key)),
            )
            for key in _CONFIG
        ),
        POSTGRES.app_secret.key("uri").env_var("DATABASE_URL"),
        _CREDENTIALS.key("client_id").env_var("PLAID_MCP_CLIENT_ID"),
        _CREDENTIALS.key("client_secret").env_var("PLAID_MCP_CLIENT_SECRET"),
    ]


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


def _oidc_credentials(chart: Chart) -> None:
    """Read only the app's Authentik client Secret from the Authentik namespace through ESO."""
    reader = ServiceAccount(
        chart,
        "oidc-secret-reader",
        metadata=ApiObjectMetadata(name=_OIDC_READER, namespace=NAMESPACE),
        automount_token=False,
    )
    store = single_secret_store(
        chart,
        "plaid-link-oidc",
        reader=reader,
        source_namespace="authentik",
        source_secret=_OIDC_CREDENTIALS_NAME,
        consumer_namespace=NAMESPACE,
    )
    ExternalSecret(
        chart,
        "oidc-external-secret",
        metadata=ApiObjectMetadata(name=_OIDC_CREDENTIALS_NAME, namespace=NAMESPACE),
        refresh_interval="10m",
        secret_store_ref=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract(_OIDC_CREDENTIALS_NAME)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.DELETE,
    )


def _rbac(chart: Chart) -> None:
    k8s.KubeServiceAccount(
        chart,
        "service-account",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE),
        image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
    )
    k8s.KubeRole(
        chart,
        "role",
        metadata=k8s.ObjectMeta(name=_SECRET_MANAGER, namespace=NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""], resources=["secrets"], verbs=["get", "list", "create", "update", "patch", "delete"]
            )
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "role-binding",
        metadata=k8s.ObjectMeta(name=_SECRET_MANAGER, namespace=NAMESPACE),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_NAME, namespace=NAMESPACE)],
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_SECRET_MANAGER),
    )


def _deployment(chart: Chart) -> None:
    health = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_WEB.pod_port))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_WEB.pods.selector,
            annotations={
                "description": (
                    "Plaid Link UI authenticates users with Authentik OIDC; verified webhooks queue transaction"
                    " syncs, with the daily Cron catching up the mirror."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_WEB.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_WEB.pods.selector),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    service_account_name=_NAME,
                    # K8sSecretStore.from_incluster (finance/plaid/db/secret_store.py) writes the
                    # access-token Secrets with this token.
                    automount_service_account_token=True,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="plaid-web",
                            image="git.allegedly.works/ducktape-ci/plaid-mcp-server:unset",
                            image_pull_policy="Always",
                            security_context=_container_security_context(),
                            ports=[_WEB.port.k8s_container_port()],
                            env=[
                                *_env(),
                                k8s.EnvVar(name="PLAID_MCP_OIDC_ISSUER", value=_OIDC_ISSUER),
                                _OIDC_CREDENTIALS.key("client_id").env_var("PLAID_MCP_OIDC_CLIENT_ID"),
                                _OIDC_CREDENTIALS.key("client_secret").env_var("PLAID_MCP_OIDC_CLIENT_SECRET"),
                                _OIDC_CREDENTIALS.key("session_secret").env_var("PLAID_MCP_OIDC_SESSION_SECRET"),
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


def _sync_cronjob(chart: Chart) -> None:
    k8s.KubeCronJob(
        chart,
        "sync",
        metadata=k8s.ObjectMeta(
            name="plaid-mcp-sync",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Daily catch-up sync for transactions, accounts, holdings and liabilities. Transaction history"
                    " uses Plaid cursors; no real-time balance endpoint calls."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="17 0 * * *",
            concurrency_policy="Forbid",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    backoff_limit=1,
                    template=k8s.PodTemplateSpec(
                        metadata=k8s.ObjectMeta(labels={"app.kubernetes.io/name": "plaid-mcp-sync"}),
                        spec=k8s.PodSpec(
                            service_account_name=_NAME,
                            # The sync reads the access-token Secrets through K8sSecretStore.
                            automount_service_account_token=True,
                            restart_policy="Never",
                            security_context=k8s.PodSecurityContext(
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
                            ),
                            containers=[
                                k8s.Container(
                                    name="sync",
                                    image="git.allegedly.works/ducktape-ci/plaid-mcp-sync:unset",
                                    image_pull_policy="Always",
                                    security_context=_container_security_context(),
                                    env=_env(),
                                    resources=_resources(),
                                )
                            ],
                        ),
                    ),
                )
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    k8s.KubeConfigMap(chart, "config", metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAMESPACE), data=_CONFIG)
    _rbac(chart)
    _oidc_credentials(chart)
    _deployment(chart)
    _sync_cronjob(chart)
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
            annotations={"description": "Plaid Link UI; browser access authenticates with Authentik OIDC."},
        ),
        hostnames=["plaid-mcp.allegedly.works"],
        backend=_WEB,
        listener=None,
    )
    NetworkPolicy(
        chart,
        "ingress-policy",
        metadata=ApiObjectMetadata(
            name="plaid-mcp-ingress",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Default-deny ingress for plaid-mcp pods. Only the cluster Gateway can reach the public"
                    " Link UI and Plaid webhook routes."
                )
            },
        ),
        endpoint_selector=_WEB.pods.selector,
        ingress=[IngressRule.from_gateway(_WEB.pod_port)],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart, manifest_name="app.k8s.yaml")
