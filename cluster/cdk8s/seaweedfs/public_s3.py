"""The single public-facing SeaweedFS S3 gateway (`s3.allegedly.works`), and the native IAM
registration of the externally managed credentials it serves.

The gateway merges its mounted static config with filer-backed IAM, so possession of any
valid S3 key authenticates here; bucket policy still limits what that key can do. Every
credential must therefore stay a confidential Secret even when its usual consumer is
cluster-internal.

The claude-reader and DriveFS reader/writer keys are registered without rotating them: the
operator reads each pair from its Secret in `seaweedfs-credentials` (`external_credentials`)
instead of generating a replacement. DriveFS permissions come from its Bucket
(`drivefs_artifacts_bucket`); claude-reader's from the policy below.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from seaweed_s3policy_crds.com.seaweedfs.seaweed import (
    S3Policy,
    S3PolicySpec,
    S3PolicySpecReclaimPolicy,
    S3PolicySpecSeaweedRef,
    S3PolicySpecStatements,
    S3PolicySpecStatementsEffect,
)
from seaweed_s3policybinding_crds.com.seaweedfs.seaweed import (
    S3PolicyBinding,
    S3PolicyBindingSpec,
    S3PolicyBindingSpecPolicyRef,
    S3PolicyBindingSpecReclaimPolicy,
    S3PolicyBindingSpecSeaweedRef,
    S3PolicyBindingSpecSubjects,
    S3PolicyBindingSpecSubjectsKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import (
    cluster,
    drivefs_artifacts_bucket,
    external_credentials,
    loom_gym_bucket,
    namespace,
    s3,
)

NAME = "public-s3"
OUTPUT_DIR = "cluster/k8s/seaweedfs/public-s3"
_PORT = 8333
_METRICS_PORT = 9327
_CONFIG_MAP = "public-s3-bootstrap-config"
_CONFIG_KEY = "seaweedfs_s3_config.json"
_CONFIG_DIR = "/etc/sw"
_SELECTOR = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "s3"}
_LABELS = {**_SELECTOR, "app.kubernetes.io/part-of": "seaweedfs"}
_CLAUDE_READER = "claude-reader"
_CLAUDE_READER_POLICY = "claude-reader-buckets"
# Buckets claude-reader may list and read.
_CLAUDE_READABLE_BUCKETS = ("attic", drivefs_artifacts_bucket.NAME, "vm-images", loom_gym_bucket.NAME)


def _external_identity(scope: Construct, name: str, *, secret: str, access_key: str, secret_key: str) -> None:
    """An S3Identity plus the S3Credentials registering its externally managed key pair as-is."""
    s3.identity(scope, name)
    s3.credentials(
        scope,
        identity=name,
        namespace=namespace.NAME,
        secret=secret,
        secret_namespace=external_credentials.NAMESPACE,
        key_fields=s3.SecretKeyFields(access_key=access_key, secret_key=secret_key),
    )


def _iam(scope: Construct) -> None:
    _external_identity(
        scope,
        "drivefs-artifacts-writer",
        secret=external_credentials.DRIVEFS_ARTIFACTS_SECRET,
        access_key="writerAccessKey",
        secret_key="writerSecretKey",
    )
    # Shares the writer's Secret but registers only the reader pair.
    _external_identity(
        scope,
        "drivefs-artifacts-reader",
        secret=external_credentials.DRIVEFS_ARTIFACTS_SECRET,
        access_key="readerAccessKey",
        secret_key="readerSecretKey",
    )
    _external_identity(
        scope,
        _CLAUDE_READER,
        secret=external_credentials.CLAUDE_READER_SECRET,
        access_key="claudeReaderAccessKey",
        secret_key="claudeReaderSecretKey",
    )
    S3Policy(
        scope,
        "claude-reader-policy",
        metadata=metadata(_CLAUDE_READER_POLICY, namespace.NAME),
        spec=S3PolicySpec(
            seaweed_ref=S3PolicySpecSeaweedRef(name=cluster.NAME),
            reclaim_policy=S3PolicySpecReclaimPolicy.RETAIN,
            statements=[
                S3PolicySpecStatements(
                    sid="ListReadableBuckets",
                    effect=S3PolicySpecStatementsEffect.ALLOW,
                    actions=["s3:ListBucket"],
                    resources=list(_CLAUDE_READABLE_BUCKETS),
                ),
                S3PolicySpecStatements(
                    sid="ReadBucketObjects",
                    effect=S3PolicySpecStatementsEffect.ALLOW,
                    actions=["s3:GetObject"],
                    resources=[f"{bucket}/*" for bucket in _CLAUDE_READABLE_BUCKETS],
                ),
                S3PolicySpecStatements(
                    sid="WriteAndTagLoomGymObjects",
                    effect=S3PolicySpecStatementsEffect.ALLOW,
                    actions=[
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:GetObjectTagging",
                        "s3:PutObjectTagging",
                        "s3:DeleteObjectTagging",
                    ],
                    resources=[f"{loom_gym_bucket.NAME}/*"],
                ),
            ],
        ),
    )
    S3PolicyBinding(
        scope,
        "claude-reader-policy-binding",
        metadata=metadata(_CLAUDE_READER_POLICY, namespace.NAME),
        spec=S3PolicyBindingSpec(
            seaweed_ref=S3PolicyBindingSpecSeaweedRef(name=cluster.NAME),
            policy_ref=S3PolicyBindingSpecPolicyRef(name=_CLAUDE_READER_POLICY),
            subjects=[
                S3PolicyBindingSpecSubjects(kind=S3PolicyBindingSpecSubjectsKind.S3_IDENTITY, name=_CLAUDE_READER)
            ],
            reclaim_policy=S3PolicyBindingSpecReclaimPolicy.RETAIN,
        ),
    )


def _gateway(scope: Construct) -> None:
    # The only local configuration left is the credential-free anonymous PR-visuals read
    # rule; credentialed identities are all native IAM, so this is a ConfigMap and not a
    # Secret.
    k8s.KubeConfigMap(
        scope,
        "bootstrap-config",
        metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=namespace.NAME),
        data={_CONFIG_KEY: '{"identities":[\n  {"name":"anonymous","actions":["Read:pr-visuals"]}\n]}\n'},
    )
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=namespace.NAME,
            labels=_LABELS,
            annotations={
                "reloader.stakater.com/auto": "true",
                "description": (
                    "Single public-facing SeaweedFS S3 gateway (s3.allegedly.works). Mounts a static config for"
                    " bootstrap and public-specific identities. Filer-backed IAM identities are also valid here,"
                    " so every credential must remain a confidential Secret even when its usual consumer is"
                    " cluster-internal."
                ),
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_SELECTOR),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                    tolerations=[
                        k8s.Toleration(
                            key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name="s3",
                            image="chrislusf/seaweedfs:4.46",
                            image_pull_policy="IfNotPresent",
                            command=[
                                "/bin/sh",
                                "-ec",
                                f"weed -logtostderr=true s3 -port={_PORT} -filer=seaweedfs-filer:8888"
                                f" -config={_CONFIG_DIR}/{_CONFIG_KEY} -metricsPort={_METRICS_PORT} -ip.bind=0.0.0.0",
                            ],
                            ports=[
                                k8s.ContainerPort(name="s3-http", container_port=_PORT, protocol="TCP"),
                                k8s.ContainerPort(name="s3-metrics", container_port=_METRICS_PORT, protocol="TCP"),
                            ],
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(path="/status", port=k8s.IntOrString.from_number(_PORT)),
                                initial_delay_seconds=10,
                                timeout_seconds=3,
                                period_seconds=15,
                                success_threshold=1,
                                failure_threshold=100,
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("500m"),
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("2"),
                                    "memory": k8s.Quantity.from_string("2Gi"),
                                },
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                run_as_non_root=True,
                                run_as_user=65532,
                                run_as_group=65532,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                            volume_mounts=[k8s.VolumeMount(name="s3-config", mount_path=_CONFIG_DIR, read_only=True)],
                        )
                    ],
                    volumes=[k8s.Volume(name="s3-config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=namespace.NAME, labels=_LABELS),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=_SELECTOR,
            ports=[
                k8s.ServicePort(
                    name="s3-http", protocol="TCP", port=_PORT, target_port=k8s.IntOrString.from_string("s3-http")
                ),
                k8s.ServicePort(
                    name="s3-metrics",
                    protocol="TCP",
                    port=_METRICS_PORT,
                    target_port=k8s.IntOrString.from_string("s3-metrics"),
                ),
            ],
        ),
    )
    https_route(
        scope,
        "route",
        metadata=metadata(NAME, namespace.NAME),
        hostname="s3.allegedly.works",
        backend=NAME,
        port=_PORT,
        timeout="3600s",
        hsts=False,
        listener=None,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _iam(chart)
    _gateway(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_public_s3(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_external_credentials: Kustomization,
    seaweedfs_drivefs_artifacts_bucket: Kustomization,
    vm_images_publisher: Kustomization,
    seaweedfs_secrets: Kustomization,
    seaweedfs_cluster: Kustomization,
    gateway: Kustomization,
) -> Kustomization:
    name = "seaweedfs-public-s3"
    return flux_kustomization(
        chart,
        name,
        # Gate on Bucket CRs managed in this repo that the public identities target.
        # Claude and DriveFS identities authenticate through native IAM; static
        # gateway configuration now contains only the credential-free anonymous read.
        artifact,
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_external_credentials,
            seaweedfs_drivefs_artifacts_bucket,
            vm_images_publisher,
            seaweedfs_secrets,
            seaweedfs_cluster,
            gateway,
        ),
        timeout="5m",
        decryption=SOPS_DECRYPTION,
    )
