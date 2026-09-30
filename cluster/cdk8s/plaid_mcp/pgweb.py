"""plaid-mcp's `pgweb/`: pgweb, a stock PostgreSQL browser whose JSON API runs a query and returns
its rows, over the Plaid sync database, for the sandboxes that need to run SQL.

It connects as `plaid_ro` using the shared read-only database credential (`db.READONLY`), so it
reads what that role may SELECT and changes nothing that role may not write. `--readonly` and a
locked session sit on top of the role as guard rails: pgweb's read-only mode is a keyword filter,
not a boundary. A caller authenticates to pgweb with one HTTP Basic password, minted here and copied
to the agentplane-staging egress proxy (agentplane/egress_staging_credentials.py), which presents it
for a sandbox that sends a placeholder; the network policy admits nothing but that proxy.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import cilium
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.plaid_mcp import db
from cluster.cdk8s.plaid_mcp.app import NAMESPACE
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/pgweb"
_NAME = "plaid-pgweb"
_LABELS = {"app.kubernetes.io/name": _NAME}
# The image's own entrypoint binds 0.0.0.0:8081.
SERVICE = ServiceRef(
    name=_NAME, port=Port(name="http", number=8081), pods=Pods(namespace=NAMESPACE, labels=tuple(_LABELS.items()))
)
# What pgweb asks its callers for, as HTTP Basic. The user is not secret; the password is minted
# below and read by the egress proxy, which is the only holder besides pgweb.
AUTH_USER = "plaid"
AUTH = SecretRef(namespace=NAMESPACE, name=f"{_NAME}-auth")
AUTH_KEY = "password"
# 0.17.0's multi-arch index digest.
_IMAGE = "ghcr.io/sosedoff/pgweb:0.17.0@sha256:a5256d416e2e8b92d69a4459058e3eca33a9f075d8325491644411d0bc3bd70b"
_QUERY_TIMEOUT_SECONDS = 60
# The CNPG Cluster in db.py.
_DB_CLUSTER = "plaid-mcp-db"


def _deployment(chart: Chart) -> None:
    port = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(SERVICE.pod_port))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "pgweb over the Plaid sync database as the read-only plaid_ro role, for sandboxes that reach it"
                    " through the agentplane-staging egress proxy."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="pgweb",
                            image=_IMAGE,
                            image_pull_policy="IfNotPresent",
                            # The image's entrypoint already binds 0.0.0.0:8081. No idle timeout, so
                            # the database connection does not lapse in a quiet stretch.
                            args=[
                                "--readonly",
                                "--no-ssh",
                                "--no-idle-timeout",
                                f"--query-timeout={_QUERY_TIMEOUT_SECONDS}",
                                "--skip-open",
                            ],
                            env=[
                                db.READONLY.key("DATABASE_URL").env_var("PGWEB_DATABASE_URL"),
                                # A caller cannot point pgweb at another database.
                                k8s.EnvVar(name="PGWEB_LOCK_SESSION", value="true"),
                                k8s.EnvVar(name="PGWEB_AUTH_USER", value=AUTH_USER),
                                AUTH.key(AUTH_KEY).env_var("PGWEB_AUTH_PASS"),
                            ],
                            ports=[SERVICE.port.k8s_container_port()],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                    "cpu": k8s.Quantity.from_string("25m"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                # The image's `pgweb` user, named rather than numbered there.
                                run_as_group=1000,
                                run_as_user=1000,
                            ),
                            readiness_probe=k8s.Probe(tcp_socket=port, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(tcp_socket=port, initial_delay_seconds=20, period_seconds=20),
                        )
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    mint_bearer_secret(
        chart,
        "auth-secret",
        name=AUTH.name,
        namespace=AUTH.namespace,
        key=AUTH_KEY,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.ServiceSpec(selector=_LABELS, ports=[SERVICE.port.k8s_service_port()], type="ClusterIP"),
    )
    NetworkPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name=_NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Default-deny for plaid-pgweb pods. Only the agentplane-staging egress proxy may reach pgweb, and"
                    " pgweb reaches only DNS and the Plaid CNPG cluster."
                )
            },
        ),
        endpoint_selector=_LABELS,
        ingress=[cilium.AGENTPLANE_STAGING_PROXY.admit(SERVICE.pod_port)],
        # Cilium exposes Kubernetes pod labels with the k8s: prefix.
        egress=[cilium.dns_egress(), EgressRule.to_endpoints({"k8s:cnpg.io/cluster": _DB_CLUSTER}, 5432)],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
