"""Langfuse: its namespace, Postgres, S3 bucket, identity and credentials, route, log-reader RBAC,
queue/cache Valkey and Helm release, and the `langfuse` Flux Kustomization owning them.

Hand-written beside the generated output: `langfuse-secrets.sops.yaml`.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallStrategy,
    HelmReleaseSpecInstallStrategyName,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeStrategy,
    HelmReleaseSpecUpgradeStrategyName,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cnpg, namespaces, node_scheduling
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.valkey import valkey_instance

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/langfuse"
_NAME = "langfuse"
_NAMESPACE = "langfuse"
# The chart's web Service (`langfuse.selectorLabels` plus `app: web`), for release `_NAME`.
WEB = ServiceRef(
    name="langfuse-web",
    port=Port(name="http", number=3000),
    pods=Pods(
        namespace=_NAMESPACE,
        labels=(("app.kubernetes.io/name", _NAME), ("app.kubernetes.io/instance", _NAME), ("app", "web")),
    ),
)
# The SOPS sibling.
_SECRETS = SecretRef(namespace=_NAMESPACE, name="langfuse-secrets")
_OIDC = SecretRef(namespace=_NAMESPACE, name="langfuse-oidc-config")
_VALKEY = "langfuse-valkey-ovh"
DATABASE = cnpg.PostgresRef.generated(name="langfuse-db", namespace=_NAMESPACE)


def _namespace(scope: Construct) -> None:
    namespaces.namespace(scope, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO)


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-ssd",
        size="10Gi",
        initdb=cnpg.same_owner_initdb("langfuse"),
        wal_archive=False,
    )


def _storage(scope: Construct) -> s3.PrivateBucket:
    # Retain the previous credential Secret during the staged handoff. The old
    # S3Credentials resource is retired separately; revoking its retained key and
    # removing this rollback Secret is an explicit follow-up.
    k8s.KubeSecret(
        scope,
        "legacy-s3-credentials",
        metadata=k8s.ObjectMeta(
            name="langfuse-s3-credentials",
            namespace=_NAMESPACE,
            annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"},
        ),
        type="Opaque",
    )
    return s3.PrivateBucket(
        scope,
        "storage",
        name=_NAME,
        tenant=_NAMESPACE,
        adopt_existing=True,
        description="Langfuse event, export, and media objects.",
        # Not the default `langfuse-s3-credentials`: that is the legacy Secret above, populated by
        # the old cross-namespace S3Credentials, which this one cannot adopt.
        secret_name="langfuse-seaweedfs-credentials",
        key_fields=s3.SecretKeyFields(access_key="s3-access-key-id", secret_key="s3-secret-access-key"),
    )


def _log_reader(scope: Construct) -> None:
    role = k8s.KubeRole(
        scope,
        "log-reader",
        metadata=k8s.ObjectMeta(name="langfuse-log-reader", namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["pods", "pods/log", "services", "configmaps", "events"],
                verbs=["get", "list", "watch"],
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "log-reader-binding",
        metadata=k8s.ObjectMeta(name="claude-langfuse-reader", namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind=role.kind, name=role.name),
        subjects=[
            k8s.Subject(kind="ServiceAccount", name="default", namespace="claude-sandbox"),
            k8s.Subject(
                kind="Group", name="oidc-ksbx-groups:kubectl-sandbox-users", api_group="rbac.authorization.k8s.io"
            ),
        ],
    )


def _values(storage: s3.PrivateBucket) -> dict[str, object]:
    resources = {"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}}
    return {
        "langfuse": {
            # Chart 2.1.0's appVersion trails the current release; pin both web and
            # worker to the same current Langfuse image explicitly.
            "image": {"tag": "4.35.0"},
            "features": {
                # SSO-only: email/password login is disabled below via
                # AUTH_DISABLE_USERNAME_PASSWORD. Signup must stay enabled so the
                # Authentik-gated SSO flow can establish the session on first login;
                # access is bounded by the langfuse application's admins-group policy
                # binding in Authentik (tf/gitops/sso-providers/provider_langfuse.tf).
                "signUpDisabled": False
            },
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            # Langfuse is stateless at the pod level and uses external storage. Allow
            # control-plane nodes as overflow capacity, while the affinity below keeps
            # ordinary placement on workers.
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "affinity": node_scheduling.PREFER_WORKERS,
            "nextauth": {
                "url": "https://langfuse.allegedly.works",
                "secret": _SECRETS.key("nextauth-secret").value_from(),
            },
            "salt": _SECRETS.key("salt").value_from(),
            "encryptionKey": _SECRETS.key("encryption-key").value_from(),
            "web": {
                "resources": resources,
                # The image runs initialization before its health endpoints are
                # available; give it time to finish instead of restarting it during
                # startup.
                "livenessProbe": {"initialDelaySeconds": 300},
            },
            "worker": {"resources": resources, "livenessProbe": {"initialDelaySeconds": 300}},
            # Headless initialization — bootstrap org/project/user on first startup.
            # API keys live in langfuse-secrets so LiteLLM can consume the same keys
            # later without a manual copy step.
            "additionalEnv": [
                {"name": "NODE_OPTIONS", "value": "--max-old-space-size=1536"},
                # The shared ClickHouse uses the canonical default logical cluster
                # name, so Langfuse can keep automatic migrations enabled.
                {"name": "CLICKHOUSE_CLUSTER_NAME", "value": "default"},
                # v3 -> v4 migration: dual-write has been validated with fresh LiteLLM
                # chat and Responses traffic. Backfill existing history as-is,
                # including any already-corrupt timestamps.
                # CLEANUP(langfuse-v4-migration): remove these migration overrides once
                # compatible producers, v4 API consumers, evaluators, and exports are
                # verified and historic backfill has completed; that selects v4's
                # events_only/direct defaults and stops legacy writes.
                {"name": "LANGFUSE_MIGRATION_V4_WRITE_MODE", "value": "dual"},
                {"name": "LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR", "value": "dual_write"},
                {"name": "LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL", "value": "true"},
                # Authentik SSO (generic OIDC relying party). Client credentials come
                # from the langfuse-oidc-config secret, minted by
                # tf/gitops/sso-providers/provider_langfuse.tf in the authentik
                # namespace and reflected here by emberstack reflector.
                {"name": "AUTH_CUSTOM_NAME", "value": "Authentik"},
                {
                    "name": "AUTH_CUSTOM_ISSUER",
                    # No trailing slash: next-auth builds the discovery URL as
                    # ${AUTH_CUSTOM_ISSUER}/.well-known/openid-configuration, and a
                    # trailing slash yields a double slash that Authentik 301-redirects —
                    # openid-client doesn't follow redirects on discovery (OAuthSignin).
                    "value": "https://auth.allegedly.works/application/o/langfuse",
                },
                {"name": "AUTH_CUSTOM_SCOPE", "value": "openid email profile"},
                {"name": "AUTH_CUSTOM_CLIENT_ID", "valueFrom": _OIDC.key("client-id").value_from()},
                {"name": "AUTH_CUSTOM_CLIENT_SECRET", "valueFrom": _OIDC.key("client-secret").value_from()},
                # Link the SSO identity to the headless-init admin user (same email)
                # so login lands on the existing org/project instead of an empty one.
                {"name": "AUTH_CUSTOM_ALLOW_ACCOUNT_LINKING", "value": "true"},
                # SSO-only: disable email/password login and signup entirely.
                {"name": "AUTH_DISABLE_USERNAME_PASSWORD", "value": "true"},
                {"name": "LANGFUSE_INIT_ORG_ID", "value": "langfuse-default-org"},
                {"name": "LANGFUSE_INIT_ORG_NAME", "value": "Default"},
                {"name": "LANGFUSE_INIT_PROJECT_ID", "value": "langfuse-litellm-project"},
                {"name": "LANGFUSE_INIT_PROJECT_NAME", "value": "litellm"},
                {
                    "name": "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
                    "valueFrom": _SECRETS.key("LANGFUSE_INIT_PROJECT_PUBLIC_KEY").value_from(),
                },
                {
                    "name": "LANGFUSE_INIT_PROJECT_SECRET_KEY",
                    "valueFrom": _SECRETS.key("LANGFUSE_INIT_PROJECT_SECRET_KEY").value_from(),
                },
                # Email matches the Authentik identity (agentydragon@gmail.com) so the
                # SSO account links to this org owner. Headless init adds this user as
                # an owner of langfuse-default-org on startup.
                {"name": "LANGFUSE_INIT_USER_EMAIL", "value": "agentydragon@gmail.com"},
                {"name": "LANGFUSE_INIT_USER_NAME", "value": "Rai"},
                {
                    "name": "LANGFUSE_INIT_USER_PASSWORD",
                    "valueFrom": _SECRETS.key("LANGFUSE_INIT_USER_PASSWORD").value_from(),
                },
            ],
        },
        "postgresql": {
            "deploy": False,
            "host": DATABASE.rw.name,
            "auth": {
                "username": "langfuse",
                "database": "langfuse",
                "existingSecret": DATABASE.app_secret.name,
                "secretKeys": {"userPasswordKey": "password"},
            },
        },
        # ClickHouse is managed centrally in the clickhouse namespace.
        "clickhouse": {
            "deploy": False,
            "host": client.HTTP.host,
            "httpPort": client.HTTP.port.number,
            "nativePort": client.NATIVE.port.number,
            "database": "langfuse",
            "auth": {
                "username": "langfuse",
                "existingSecret": "clickhouse-langfuse-credentials",
                "existingSecretKey": "password",
            },
            "migration": {
                "url": f"clickhouse://{client.NATIVE.host}:{client.NATIVE.port.number}",
                "ssl": False,
                "autoMigrate": True,
            },
            "clusterEnabled": True,
        },
        "redis": {
            "deploy": False,
            "host": f"{_VALKEY}-master.{_NAMESPACE}.svc.cluster.local",
            "port": 6379,
            # The Valkey runs without auth. cdk8s drops None, so the chart's default
            # username cannot be nulled; an empty one renders the same URL (the chart
            # adds `user@` only for a non-empty username). Dropping the key instead
            # would bring back the default and add `default@`.
            "auth": {"username": ""},
        },
        "s3": {
            "deploy": False,
            "storageProvider": "s3",
            "bucket": _NAME,
            "region": "auto",
            "endpoint": "http://seaweedfs-s3.seaweedfs.svc:8333",
            "forcePathStyle": True,
            "accessKeyId": storage.access_key.value_from(),
            "secretAccessKey": storage.secret_key.value_from(),
            "eventUpload": {"prefix": "events/"},
            "batchExport": {"prefix": "exports/"},
            "mediaUpload": {"prefix": "media/"},
        },
        # Ingress disabled — using Gateway API HTTPRoute
        "ingress": {"enabled": False},
    }


def _helm_release(scope: Construct, *, storage: s3.PrivateBucket) -> None:
    helm_release(
        scope,
        _NAME,
        _NAMESPACE,
        repository=https_helm_repository(scope, _NAME, _NAMESPACE, url="https://langfuse.github.io/langfuse-k8s"),
        chart=_NAME,
        version="2.1.0",
        interval="15m",
        timeout="20m",
        install=HelmReleaseSpecInstall(
            strategy=HelmReleaseSpecInstallStrategy(name=HelmReleaseSpecInstallStrategyName.RETRY_ON_FAILURE)
        ),
        upgrade=HelmReleaseSpecUpgrade(
            strategy=HelmReleaseSpecUpgradeStrategy(name=HelmReleaseSpecUpgradeStrategyName.RETRY_ON_FAILURE)
        ),
        values=_values(storage),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _database(chart)
    storage = _storage(chart)
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=["langfuse.allegedly.works"],
        backend=WEB,
        hsts=False,
        listener=None,
    )
    _log_reader(chart)
    valkey_instance(
        chart,
        name=_VALKEY,
        namespace=_NAMESPACE,
        description="OVH Valkey for Langfuse queue/cache state",
        memory_request=Size.mebibytes(128),
        cpu_limit=Cpu.millis(500),
        memory_limit=Size.mebibytes(512),
        max_memory_percent_of_limit=80,
        storage_class="local-path-ovh",
        storage_size=Size.gibibytes(2),
    )
    _helm_release(chart, storage=storage)
    return chart


def langfuse(
    chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    valkey: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        _NAME,
        directory,
        suspend=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="20m",
        depends_on=flux_kustomization_depends_on_many(cnpg, valkey, seaweedfs_operator),
    )
