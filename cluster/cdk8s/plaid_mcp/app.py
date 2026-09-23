"""plaid-mcp's `app/`: the Plaid link web UI Deployment, the full-refresh sync CronJob, their
shared config, the Secret-managing RBAC, the Service and the ingress policy.

The images' tags are the placeholder "unset"; the hand-written `app/image-pins/kustomization.yaml`
overrides them at `kustomize build` time via Flux's image-automation markers
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker). `plaid-client-credentials.sops.yaml`
stays hand-written beside the generated output.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/app"
NAMESPACE = "plaid-mcp"
_NAME = "plaid-mcp"
_LABELS = {"app.kubernetes.io/name": _NAME}
_CONFIG_MAP = "plaid-mcp-config"
_SECRET_MANAGER = "plaid-mcp-secret-manager"
_CREDENTIALS_FILE = "plaid-client-credentials.sops.yaml"
_HTTP_PORT = 8080
_CONFIG = {
    "PLAID_MCP_PLAID_ENV": "production",
    "PLAID_MCP_PUBLIC_BASE_URL": "https://plaid-mcp.allegedly.works",
    "PLAID_MCP_TRANSACTION_DAYS": "730",
    "PLAID_MCP_INVESTMENT_TRANSACTION_DAYS": "730",
}


def _secret_env(name: str, secret: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


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
        # CNPG generates this Secret for the plaid-mcp-db Cluster (db.py).
        _secret_env("DATABASE_URL", "plaid-mcp-db-app", "uri"),
        _secret_env("PLAID_MCP_CLIENT_ID", "plaid-client-credentials", "client_id"),
        _secret_env("PLAID_MCP_CLIENT_SECRET", "plaid-client-credentials", "client_secret"),
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
    health = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_HTTP_PORT))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "Plaid self-contained link web UI. Authentik proxy outpost protects browser access; no"
                    " bespoke Plaid MCP tools are exposed in v0. The app writes access-token Secrets and syncs"
                    " linked Items into the plaid-mcp Postgres database."
                ),
                "reloader.stakater.com/auto": "true",
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    service_account_name=_NAME,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="plaid-web",
                            image="git.allegedly.works/ducktape-ci/plaid-mcp-server:unset",
                            image_pull_policy="Always",
                            security_context=_container_security_context(),
                            ports=[k8s.ContainerPort(name="http", container_port=_HTTP_PORT, protocol="TCP")],
                            env=_env(),
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
                    "v0 synchronous full-refresh Plaid sync. Keeps Postgres fresh within 12 hours; no real-time"
                    " balance endpoint calls."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="17 */12 * * *",
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
    _deployment(chart)
    _sync_cronjob(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http", port=_HTTP_PORT, target_port=k8s.IntOrString.from_string("http"), protocol="TCP"
                )
            ],
            type="ClusterIP",
        ),
    )
    cilium.network_policy(
        chart,
        "ingress-policy",
        metadata=metadata(
            "plaid-mcp-ingress",
            NAMESPACE,
            annotations={
                "description": (
                    "Default-deny ingress for plaid-mcp pods. Only the Authentik embedded proxy outpost can"
                    " reach the v0 web UI."
                )
            },
        ),
        selector=_LABELS,
        ingress=[cilium.ingress_from(cilium.endpoint_labels("authentik", "authentik"), ports=[_HTTP_PORT])],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            namespace=NAMESPACE, resources=[f"{_NAME}.k8s.yaml", _CREDENTIALS_FILE], components=["./image-pins"]
        ),
    )
