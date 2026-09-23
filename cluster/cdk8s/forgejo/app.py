"""Forgejo: the Helm release, its git volume, S3 bucket and credentials, metrics token, route,
public SSH listener, disruption budget and ServiceMonitor.

Hand-written beside the generated output: `forgejo-admin-password.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cilium_envoyconfig_crds.io.cilium import (
    CiliumEnvoyConfig,
    CiliumEnvoyConfigSpec,
    CiliumEnvoyConfigSpecBackendServices,
    CiliumEnvoyConfigSpecNodeSelector,
)
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeRemediation,
    HelmReleaseSpecValuesFrom,
    HelmReleaseSpecValuesFromKind,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec, HelmRepositorySpecType
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsBearerTokenSecret,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import s3

_OUTPUT_DIR = "cluster/k8s/forgejo/app"
_NAME = "forgejo"
_NAMESPACE = "forgejo"
_S3_CREDENTIALS_SECRET = "forgejo-s3-credentials"
_METRICS_TOKEN = "forgejo-metrics-token"
_GIT_CLAIM = "forgejo-git-rwx-ssd"
_RELEASE_LABELS = {"app.kubernetes.io/name": _NAME, "app.kubernetes.io/instance": _NAME}
_VALKEY = "redis://forgejo-valkey-ovh-master.forgejo.svc.cluster.local:6379/0"
_CLUSTER_CA_MOUNT = {"name": "cluster-ca", "mountPath": "/etc/ssl/certs/cluster-ca", "readOnly": True}


def _git_storage(scope: Construct) -> None:
    # Git repositories volume for Forgejo, ReadWriteMany so the two forgejo replicas
    # (replicaCount 2 below) can mount it concurrently. Provisioned here rather than by
    # the chart because the chart's default claim (gitea-shared-storage) is RWO and
    # accessModes are immutable — going HA required a fresh RWX claim.
    #
    # On the SeaweedFS SSD tier (seaweedfs-ovh-ssd, KS-GAME NVMe) since the 2026-07
    # git-latency migration; the repos were copied here from the original HDD RWX claim
    # (forgejo-git-rwx on seaweedfs-ovh) via a one-time VolSync rsync-TLS cutover, now
    # retired. SeaweedFS RWX multi-mount and the cross-node git filesystem semantics it
    # depends on (immediate write visibility, atomic exclusive-create for *.lock, atomic
    # rename) were verified on this cluster before the HA cutover.
    k8s.KubePersistentVolumeClaim(
        scope,
        "git-storage",
        metadata=k8s.ObjectMeta(
            name=_GIT_CLAIM,
            namespace=_NAMESPACE,
            annotations={"description": "Forgejo git repos on the SeaweedFS SSD tier, RWX for 2-replica HA"},
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            storage_class_name="seaweedfs-ovh-ssd",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("200Gi")}),
        ),
    )


def _object_storage(scope: Construct) -> None:
    bucket = s3.Bucket(
        scope,
        "bucket",
        name=_NAME,
        namespace=_NAMESPACE,
        adopt_existing=True,
        description="Forgejo packages, LFS, attachments, and artifacts.",
    )
    # Declared by the seaweedfs-forgejo-bucket Kustomization.
    identity = s3.IdentityRef(scope, "identity", name=_NAME)
    bucket.grant_read_write(identity)
    identity.credentials(
        namespace=_NAMESPACE,
        secret=_S3_CREDENTIALS_SECRET,
        key_fields=s3.SecretKeyFields(access_key="accessKey", secret_key="secretKey"),
        description="Forgejo's SeaweedFS S3 credentials.",
    )


def _metrics_token(scope: Construct) -> None:
    """ESO owns a stable Forgejo metrics bearer token. CreatedOnce avoids rotating the token
    without coordinating a Forgejo restart and Prometheus scrape cutover."""
    generator = Password(
        scope,
        "metrics-token-generator",
        metadata=metadata(_METRICS_TOKEN, _NAMESPACE),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        scope,
        "metrics-token",
        metadata=metadata(_METRICS_TOKEN, _NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
            target=ExternalSecretSpecTarget(
                name=_METRICS_TOKEN,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=ExternalSecretSpecTargetTemplate(type="Opaque", data={"token": "{{ .password }}"}),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            api_version="generators.external-secrets.io/v1alpha1",
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name=generator.name,
                        )
                    )
                )
            ],
        ),
    )


def _secret_env(name: str, secret: str, key: str) -> dict[str, object]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def _values() -> dict[str, object]:
    return {
        "global": {"imageRegistry": ""},
        # HA: two replicas sharing the RWX git PVC. Both pods mount the same SeaweedFS
        # volume concurrently (RWX multi-mount + cross-node git lock/rename semantics
        # verified 2026-06-28), so a rolling update is safe — the old RWO Recreate-only
        # deadlock (RollingUpdate + RWO = stuck init container) no longer applies. Session
        # and issue search live in Postgres, and cache/queue in the shared valkey
        # (../cache), so neither replica holds per-instance state.
        "replicaCount": 2,
        "strategy": {
            "type": "RollingUpdate",
            # Allow one old pod to be removed so a replacement can schedule when hard
            # anti-affinity and the two-worker capacity would otherwise deadlock.
            "rollingUpdate": {"maxUnavailable": 1},
        },
        # Pin to OVH kimsufi nodes: required by the seaweedfs-ovh CSI (OVH-only) and
        # co-located with the OVH-HA forgejo-db (cnpg_conventions R5).
        "nodeSelector": {"topology.kubernetes.io/zone": "hil-ovh"},
        "affinity": {
            # Prefer ordinary workers when this workload tolerates control planes.
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
            },
            # Keep the two replicas on different hosts so a single node loss can't take
            # both down. Required (not preferred): with two off-CP workers they land one
            # each; if a worker is gone the second can still schedule elsewhere (the off-CP
            # rule above is only a soft preference), so this never blocks the second
            # replica — it only forbids co-location.
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {"labelSelector": {"matchLabels": _RELEASE_LABELS}, "topologyKey": "kubernetes.io/hostname"}
                ]
            },
        },
        # Forgejo configuration (chart retains the legacy `gitea:` values key)
        "gitea": {
            "admin": {
                # Username and password from SOPS secret
                "existingSecret": "forgejo-admin-password",
                "email": "admin@allegedly.works",
                # Password set once on creation, never reset (SSO for normal use)
                "passwordMode": "initialOnlyNoReset",
            },
            # OAuth2 authentication sources
            "oauth": [
                {
                    "name": "authentik",
                    "provider": "openidConnect",
                    "existingSecret": "forgejo-oauth-client-secret",
                    "autoDiscoverUrl": (
                        "https://auth.allegedly.works/application/o/forgejo/.well-known/openid-configuration"
                    ),
                    "iconUrl": "https://auth.allegedly.works/static/dist/assets/icons/icon.png",
                    "scopes": "email profile",
                    # Map the Authentik `groups` claim (emitted by the default `profile`
                    # scope mapping) to Forgejo site-admin: members of the `forgejo-admins`
                    # Authentik group (tf/gitops/sso-providers/main.tf) are promoted to admin
                    # on OIDC login, non-members demoted. The built-in local admin account is
                    # unaffected (not an OAuth2 user). Takes effect on the user's next login.
                    "groupClaimName": "groups",
                    "adminGroup": "forgejo-admins",
                }
            ],
            "config": {
                "APP_NAME": "Forgejo: Beyond coding. We forge.",
                "RUN_MODE": "prod",
                "server": {
                    "DOMAIN": "git.allegedly.works",
                    "SSH_DOMAIN": "git.allegedly.works",
                    "ROOT_URL": "https://git.allegedly.works",
                    "HTTP_PORT": 3000,
                    "SSH_PORT": 2222,
                    "DISABLE_SSH": False,
                    "START_SSH_SERVER": True,
                    "LFS_START_SERVER": True,
                },
                # Session stays in Postgres (the CNPG forgejo-db is already HA), so it's
                # shared across replicas — no per-instance state. Cache + queue, however,
                # default to per-instance backends (memory / leveldb on the pod's PVC):
                # with >1 replica each instance would keep its own cache (stale reads) and,
                # worse, two leveldb queues on one shared volume would corrupt. Both move
                # to the shared, replicated forgejo-valkey-ovh (cluster/k8s/forgejo/cache)
                # so the deployment can scale to 2 replicas. (Switching the queue backend
                # abandons any in-flight leveldb queue items on the next restart — fine for
                # this instance's transient queues: webhook deliveries, mirror syncs.)
                "session": {"PROVIDER": "db"},
                "cache": {"ENABLED": True, "ADAPTER": "redis", "HOST": _VALKEY},
                "queue": {"TYPE": "redis", "CONN_STR": _VALKEY},
                # Bleve is a local file-backed issue index. With two Forgejo replicas,
                # both can open the same index on the shared RWX volume; issues.bleve
                # became corrupted and left one replica crash-looping on 2026-08-02.
                # Forgejo's PostgreSQL-backed issue index is shared safely by both
                # replicas and needs no rebuildable filesystem state.
                "indexer": {"ISSUE_INDEXER_TYPE": "db"},
                # In-cluster CI (Forgejo Actions). Enables the server-side feature; a
                # registered act_runner (cluster/k8s/haku-ci) executes workflows. Used so
                # Haku can build its own UI image from haku-state source entirely
                # in-cluster — haku-state may hold private operator data, so its builds must
                # never go to BuildBuddy/RBE or any external CI. See haku/PLAN.md.
                "actions": {"ENABLED": True},
                "metrics": {"ENABLED": True},
                "security": {"INSTALL_LOCK": True, "SECRET_KEY": "change-this-secret-key-in-production"},
                "database": {
                    "DB_TYPE": "postgres",
                    "HOST": "forgejo-db-ssd-rw.forgejo:5432",
                    "NAME": "forgejo",
                    "USER": "forgejo",
                },
                "oauth2_client": {
                    "REGISTER_EMAIL_CONFIRM": False,
                    "ENABLE_AUTO_REGISTRATION": True,
                    "USERNAME": "nickname",
                    "UPDATE_AVATAR": True,
                    "ACCOUNT_LINKING": "login",
                },
                # Object storage on SeaweedFS S3: packages (container registry), LFS,
                # attachments, artifacts. Git repos stay on the seaweedfs-ovh PVC — git
                # needs a POSIX filesystem, so there's no S3 backend for repos. Creds are
                # injected via additionalConfigFromEnvs below.
                "storage": {
                    "STORAGE_TYPE": "minio",
                    "MINIO_ENDPOINT": "seaweedfs-s3.seaweedfs.svc:8333",
                    "MINIO_BUCKET": _NAME,
                    "MINIO_LOCATION": "us-east-1",
                    "MINIO_USE_SSL": False,
                    "MINIO_BUCKET_LOOKUP": "path",
                },
            },
            # SeaweedFS S3 credentials from the operator-owned Secret in the Forgejo
            # namespace. FORGEJO__ is the chart's env -> app.ini prefix.
            "additionalConfigFromEnvs": [
                _secret_env("FORGEJO__metrics__TOKEN", _METRICS_TOKEN, "token"),
                _secret_env("FORGEJO__storage__MINIO_ACCESS_KEY_ID", _S3_CREDENTIALS_SECRET, "accessKey"),
                _secret_env("FORGEJO__storage__MINIO_SECRET_ACCESS_KEY", _S3_CREDENTIALS_SECRET, "secretKey"),
            ],
        },
        # Trust cluster CA bundle (includes Let's Encrypt staging CA)
        "deployment": {
            # Reloader: auto-restart pods when secrets change
            "annotations": {"reloader.stakater.com/auto": "true"},
            "env": [{"name": "SSL_CERT_FILE", "value": "/etc/ssl/certs/cluster-ca/ca-certificates.crt"}],
        },
        # Mount CA bundle (includes Let's Encrypt staging CA when in staging mode)
        "extraVolumes": [{"name": "cluster-ca", "configMap": {"name": "cluster-internal-ca-bundle"}}],
        "extraContainerVolumeMounts": [_CLUSTER_CA_MOUNT],
        "extraInitVolumeMounts": [_CLUSTER_CA_MOUNT],
        "service": {"http": {"type": "ClusterIP", "port": 3000}, "ssh": {"type": "ClusterIP", "port": 2222}},
        # Resources. Forgejo is a monolith: the web UI, API, git HTTP/SSH, the
        # container/package registry, AND the Actions coordinator all run in this one
        # process — so they share this CPU budget. The old 500m limit caused constant
        # CFS throttling under load (CI image pushes + the haku-ui app's reads + git
        # ops), making even trivial /api/v1/version take ~7s. Give it real headroom.
        "resources": {
            "limits": {
                "cpu": "4",
                # TODO(vpa-memory-audit): 2Gi -> 8Gi. VPA observed a 3.24Gi request /
                # 4.79Gi upper bound — the declared limit was below what Forgejo
                # actually uses, and only VPA raising it kept this from OOMing. 3.2Gi
                # resident for this instance's repo count is worth understanding
                # (git pack cache? mirror sync?) before treating 8Gi as correct.
                "memory": "8Gi",
            },
            "requests": {"cpu": "500m", "memory": "512Mi"},
        },
        # Repos + LFS/packages/attachments/artifacts all live here, on SeaweedFS via the
        # seaweedfs-ovh CSI (object-backed; git needs a POSIX FS, so no S3 backend).
        "persistence": {
            "enabled": True,
            # Mount our pre-provisioned RWX claim (_git_storage) instead of the chart's
            # default RWO gitea-shared-storage. This chart has no `existingClaim`: it
            # mounts whatever `claimName` points at and only creates the PVC itself when
            # `create: true`. So create:false + claimName makes it adopt our externally
            # managed PVC and create nothing. accessModes are immutable, so HA needed a
            # fresh claim — existing git data was copied old->new during the 2026-06-28
            # migration cutover. (The old gitea-shared-storage PVC has the chart's
            # helm.sh/resource-policy:keep annotation, so it survives as a rollback.)
            "create": False,
            # git repos on the SeaweedFS SSD tier (Phase 3, ovh_storage_tiering.md).
            # Migrated from forgejo-git-rwx (HDD) via VolSync rsync-TLS; read-zero-downtime
            # rolling repoint.
            "claimName": _GIT_CLAIM,
            # The chart refuses replicaCount > 1 unless accessModes[0] is ReadWriteMany (a
            # guard against a single-writer FS under multiple replicas). create:false means
            # the chart won't actually create a PVC from this — _git_storage owns the real
            # RWX claim — but the assertion still reads this value.
            "accessModes": ["ReadWriteMany"],
        },
        # PostgreSQL: external CNPG cluster (forgejo-db)
        "postgresql": {"enabled": False},
        "postgresql-ha": {"enabled": False},
        "redis-cluster": {"enabled": False},
        # Valkey (disabled — using db sessions, memory cache, level queues)
        "valkey-cluster": {"enabled": False},
        "memcached": {"enabled": False},
        "serviceAccount": {"create": True},
    }


def _helm_release(scope: Construct) -> None:
    repository = HelmRepository(
        scope,
        "helm-repository",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HelmRepositorySpec(
            type=HelmRepositorySpecType.OCI, interval="24h", url="oci://code.forgejo.org/forgejo-helm"
        ),
    )
    HelmRelease(
        scope,
        "helm-release",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            # Extended timeout (PostgreSQL + PVC binding + init containers)
            timeout="15m",
            # Runtime prerequisites may become ready after admission; keep retrying while
            # they converge.
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=-1)),
            upgrade=HelmReleaseSpecUpgrade(remediation=HelmReleaseSpecUpgradeRemediation(retries=-1)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=_NAME,
                    version="17.1.6",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values_from=[
                HelmReleaseSpecValuesFrom(
                    kind=HelmReleaseSpecValuesFromKind.SECRET,
                    name="forgejo-db-ssd-creds",
                    values_key="password",
                    # The Forgejo chart is a fork of the Gitea chart and keeps the `gitea:` values key.
                    target_path="gitea.config.database.PASSWD",
                )
            ],
            values=_values(),
        ),
    )


_TCP_PROXY_TYPE = "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy"


def _ssh_listener(scope: Construct) -> None:
    """Public Forgejo SSH via a manual CiliumEnvoyConfig.

    Cilium 1.19's Gateway API controller supports HTTPRoute/GRPCRoute/TLSRoute but not TCPRoute,
    and SSH has no SNI/Host header to route on. The Cilium Envoy DaemonSet already runs with
    hostNetwork on HIL gateway nodes, so this binds git.allegedly.works:2222 directly on those
    nodes and forwards raw TCP to Forgejo's in-cluster SSH service.
    """
    service = "forgejo-ssh"
    cluster = f"{_NAMESPACE}:{service}:2222"
    CiliumEnvoyConfig(
        scope,
        "ssh-listener",
        metadata=metadata(service, _NAMESPACE, annotations={"cec.cilium.io/use-original-source-address": "false"}),
        spec=CiliumEnvoyConfigSpec(
            node_selector=CiliumEnvoyConfigSpecNodeSelector(match_labels={"topology.kubernetes.io/region": "hil"}),
            backend_services=[
                CiliumEnvoyConfigSpecBackendServices(name=service, namespace=_NAMESPACE, number=["2222"])
            ],
            resources=[
                {
                    "@type": "type.googleapis.com/envoy.config.listener.v3.Listener",
                    "name": service,
                    "address": {"socketAddress": {"address": "0.0.0.0", "portValue": 2222}},
                    "filterChains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.tcp_proxy",
                                    "typedConfig": {
                                        "@type": _TCP_PROXY_TYPE,
                                        "statPrefix": service,
                                        "cluster": cluster,
                                        "idleTimeout": "3600s",
                                    },
                                }
                            ]
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/envoy.config.cluster.v3.Cluster",
                    "name": cluster,
                    "type": "EDS",
                    "edsClusterConfig": {"serviceName": f"{_NAMESPACE}/{service}:2222"},
                    "outlierDetection": {"splitExternalLocalOriginErrors": True},
                },
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _helm_release(chart)
    https_route(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname="git.allegedly.works",
        backend="forgejo-http",
        port=3000,
        hsts=False,
        listener=None,
    )
    _git_storage(chart)
    # Keep at least one Forgejo replica serving during voluntary disruption (node drain,
    # descheduler eviction, rolling update). With replicaCount 2 this lets one pod be
    # evicted at a time but never both — git/CI/registry/API stay up. The descheduler
    # honors PDBs unconditionally, so this is also what stops it from taking both
    # replicas down at once.
    k8s.KubePodDisruptionBudget(
        chart,
        "disruption-budget",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=_RELEASE_LABELS)
        ),
    )
    _object_storage(chart)
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=ServiceMonitorSpec(
            # Helm release name; robust regardless of the chart's app name label.
            selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/instance": _NAME}),
            endpoints=[
                ServiceMonitorSpecEndpoints(
                    port="http",
                    path="/metrics",
                    bearer_token_secret=ServiceMonitorSpecEndpointsBearerTokenSecret(name=_METRICS_TOKEN, key="token"),
                )
            ],
        ),
    )
    _metrics_token(chart)
    _ssh_listener(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
    # The OAuth OIDC credentials are provisioned by tf/gitops/sso-providers/ as a k8s Secret.
    write_yaml(
        root / _OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml", "forgejo-admin-password.sops.yaml"]),
    )
