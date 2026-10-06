"""Forgejo: the Helm release, its git volume, S3 bucket, identity and credentials, metrics
token, route, public SSH listener, disruption budget and ServiceMonitor.

Hand-written beside the generated outputs: `forgejo-admin-password.sops.yaml`. OAuth OIDC credentials
come from `tf/gitops/sso-providers/`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cilium_envoyconfig_crds.io.cilium import (
    CiliumEnvoyConfig,
    CiliumEnvoyConfigSpec,
    CiliumEnvoyConfigSpecBackendServices,
    CiliumEnvoyConfigSpecNodeSelector,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeRemediation,
    HelmReleaseSpecValuesFrom,
    HelmReleaseSpecValuesFromKind,
)
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.forgejo import cache, db
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release, oci_helm_repository
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/forgejo"
_NAME = "forgejo"
_NAMESPACE = "forgejo"
HOSTNAME = "git.allegedly.works"
_METRICS_TOKEN = SecretRef(namespace=_NAMESPACE, name="forgejo-metrics-token").key("token")
_GIT_CLAIM = "forgejo-git-rwx-ssd"
_RELEASE_LABELS = {"app.kubernetes.io/name": _NAME, "app.kubernetes.io/instance": _NAME}
# The chart's HTTP and SSH Services.
HTTP = ServiceRef(
    name="forgejo-http",
    port=Port(name="http", number=3000),
    pods=Pods(namespace=_NAMESPACE, labels=tuple(_RELEASE_LABELS.items())),
)
_SSH = ServiceRef(name="forgejo-ssh", port=Port(name="ssh", number=2222), pods=HTTP.pods)
_VALKEY = f"redis://{cache.MASTER.host}:{cache.MASTER.port.number}/0"
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
    # retired. Gotcha: `weed mount` does not make O_EXCL exclusive across mounts, so git's
    # *.lock files do not serialize ref updates between replicas on different nodes, and a
    # collision can leave the ref empty:
    # cluster/docs/lessons_learned/2026_09_23_forgejo_cross_mount_ref_lock_zeroed_main.md.
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


def _object_storage(scope: Construct) -> s3.PrivateBucket:
    return s3.PrivateBucket(
        scope,
        "object-storage",
        name=_NAME,
        tenant=_NAMESPACE,
        adopt_existing=True,
        description="Forgejo packages, LFS, attachments, and artifacts.",
        key_fields=s3.SecretKeyFields(access_key="accessKey", secret_key="secretKey"),
    )


def _metrics_token(scope: Construct) -> None:
    """ESO owns a stable Forgejo metrics bearer token. CreatedOnce avoids rotating the token
    without coordinating a Forgejo restart and Prometheus scrape cutover."""
    mint_bearer_secret(
        scope,
        "metrics-token",
        name=_METRICS_TOKEN.secret.name,
        namespace=_METRICS_TOKEN.secret.namespace,
        key=_METRICS_TOKEN.key,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _values(storage: s3.PrivateBucket) -> dict[str, object]:
    return {
        "global": {"imageRegistry": ""},
        # HA: two replicas sharing the RWX git PVC. Both pods mount the same SeaweedFS
        # volume concurrently (RWX multi-mount; git ref locks do not hold across the two
        # mounts, see _git_storage), so a rolling update is safe — the old RWO Recreate-only
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
        "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
        "affinity": {
            "nodeAffinity": node_scheduling.PREFER_WORKERS.node_affinity,
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
                    "DOMAIN": HOSTNAME,
                    "SSH_DOMAIN": HOSTNAME,
                    "ROOT_URL": f"https://{HOSTNAME}",
                    "HTTP_PORT": HTTP.pod_port,
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
                # to the shared, replicated forgejo-valkey-ovh (cache.py)
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
                # registered act_runner (cluster/cdk8s/haku_ci) executes workflows. Used so
                # Haku can build its own UI image from haku-state source entirely
                # in-cluster — haku-state may hold private operator data, so its builds must
                # never go to BuildBuddy/RBE or any external CI. See haku/PLAN.md.
                "actions": {"ENABLED": True},
                "metrics": {"ENABLED": True},
                "security": {"INSTALL_LOCK": True, "SECRET_KEY": "change-this-secret-key-in-production"},
                "database": {
                    "DB_TYPE": "postgres",
                    "HOST": f"{db.POSTGRES.rw.host}:{db.POSTGRES.rw.port.number}",
                    "NAME": db.DATABASE,
                    "USER": db.DATABASE,
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
                _METRICS_TOKEN.env_var("FORGEJO__metrics__TOKEN"),
                storage.access_key.env_var("FORGEJO__storage__MINIO_ACCESS_KEY_ID"),
                storage.secret_key.env_var("FORGEJO__storage__MINIO_SECRET_ACCESS_KEY"),
            ],
        },
        # Trust cluster CA bundle (includes Let's Encrypt staging CA)
        "deployment": {"env": [{"name": "SSL_CERT_FILE", "value": "/etc/ssl/certs/cluster-ca/ca-certificates.crt"}]},
        # Mount CA bundle (includes Let's Encrypt staging CA when in staging mode)
        "extraVolumes": [{"name": "cluster-ca", "configMap": {"name": "cluster-internal-ca-bundle"}}],
        "extraContainerVolumeMounts": [_CLUSTER_CA_MOUNT],
        "extraInitVolumeMounts": [_CLUSTER_CA_MOUNT],
        "service": {
            "http": {"type": "ClusterIP", "port": HTTP.port.number},
            "ssh": {"type": "ClusterIP", "port": _SSH.port.number},
        },
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
        "serviceAccount": {"create": True},
    }


def _helm_release(scope: Construct, *, storage: s3.PrivateBucket) -> None:
    helm_release(
        scope,
        _NAME,
        _NAMESPACE,
        repository=oci_helm_repository(scope, _NAME, _NAMESPACE, url="oci://code.forgejo.org/forgejo-helm"),
        chart=_NAME,
        version="17.1.6",
        interval="15m",
        # Extended timeout (PostgreSQL + PVC binding + init containers)
        timeout="15m",
        # Runtime prerequisites may become ready after admission; keep retrying while
        # they converge.
        install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=-1)),
        upgrade=HelmReleaseSpecUpgrade(remediation=HelmReleaseSpecUpgradeRemediation(retries=-1)),
        values_from=[
            HelmReleaseSpecValuesFrom(
                kind=HelmReleaseSpecValuesFromKind.SECRET,
                name=db.POSTGRES.app_secret.name,
                values_key="password",
                # The Forgejo chart is a fork of the Gitea chart and keeps the `gitea:` values key.
                target_path="gitea.config.database.PASSWD",
            )
        ],
        values=_values(storage),
    )


_TCP_PROXY_TYPE = "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy"


def _ssh_listener(scope: Construct) -> None:
    """Public Forgejo SSH via a manual CiliumEnvoyConfig.

    Cilium 1.19's Gateway API controller supports HTTPRoute/GRPCRoute/TLSRoute but not TCPRoute,
    and SSH has no SNI/Host header to route on. The Cilium Envoy DaemonSet already runs with
    hostNetwork on HIL gateway nodes, so this binds git.allegedly.works:2222 directly on those
    nodes and forwards raw TCP to Forgejo's in-cluster SSH service.
    """
    service = _SSH.name
    cluster = f"{_NAMESPACE}:{service}:{_SSH.port.number}"
    CiliumEnvoyConfig(
        scope,
        "ssh-listener",
        metadata=ApiObjectMetadata(
            name=service, namespace=_NAMESPACE, annotations={"cec.cilium.io/use-original-source-address": "false"}
        ),
        spec=CiliumEnvoyConfigSpec(
            node_selector=CiliumEnvoyConfigSpecNodeSelector(match_labels={"topology.kubernetes.io/region": "hil"}),
            backend_services=[
                CiliumEnvoyConfigSpecBackendServices(name=service, namespace=_NAMESPACE, number=[str(_SSH.port.number)])
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
                    "edsClusterConfig": {"serviceName": f"{_NAMESPACE}/{service}:{_SSH.port.number}"},
                    "outlierDetection": {"splitExternalLocalOriginErrors": True},
                },
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _helm_release(chart, storage=_object_storage(chart))
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=[HOSTNAME],
        backend=HTTP,
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
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=HTTP.labels),
        endpoints=[
            Endpoint.bearer_token_secret(
                port=HTTP.port.name, secret_name=_METRICS_TOKEN.secret.name, key=_METRICS_TOKEN.key
            )
        ],
    )
    _metrics_token(chart)
    _ssh_listener(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart, manifest_name="app.k8s.yaml")
