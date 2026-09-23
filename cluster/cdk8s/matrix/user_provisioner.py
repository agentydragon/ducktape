"""The Job that registers Matrix users against Synapse's admin API.

The image tag is the placeholder "unset"; the hand-written
cluster/k8s/matrix/user-provisioner/image-pins/kustomization.yaml overrides it at
`kustomize build` time via Flux's image-automation marker.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.matrix.matrix import NAMESPACE, SYNAPSE

OUTPUT_DIR = "cluster/k8s/matrix/user-provisioner"
_NAME = "matrix-user-provisioner"
# Script baked in via Bazel (//cluster/provisioners/matrix_user_provisioner:image).
_IMAGE = "git.allegedly.works/ducktape-ci/matrix-user-provisioner:unset"


def _secret_env(name: str, secret: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    k8s.KubeJob(
        chart,
        "job",
        metadata=k8s.ObjectMeta(
            name="provision-matrix-users",
            namespace=NAMESPACE,
            # Recreate job on each reconciliation to ensure idempotent provisioning
            annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"},
        ),
        spec=k8s.JobSpec(
            backoff_limit=5,
            ttl_seconds_after_finished=3600,
            template=k8s.PodTemplateSpec(
                spec=k8s.PodSpec(
                    restart_policy="OnFailure",
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    containers=[
                        k8s.Container(
                            name="provision",
                            image=_IMAGE,
                            image_pull_policy="Always",
                            env=[
                                _secret_env(
                                    "REGISTRATION_SECRET", "synapse-registration-secret", "registration_shared_secret"
                                ),
                                _secret_env("ADMIN_PASSWORD", "synapse-admin-credentials", "password"),
                                _secret_env(
                                    "PUBLIC_CODER_AGENT_BOT_PASSWORD",
                                    "public-coder-agent-matrix-bot-password",
                                    "password",
                                ),
                            ],
                        )
                    ],
                )
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml"], components=["./image-pins"]),
    )


def matrix_user_provisioner(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    matrix: Kustomization,
) -> Kustomization:
    name = "matrix-user-provisioner"
    return flux_kustomization(
        chart,
        name,
        artifact,
        wait=None,
        timeout="5m",
        # The job registers users against Synapse's admin API, so it must not start
        # until Synapse answers. Depending on the app Kustomization (which is
        # wait:true over the HelmRelease) gives that ordering; the health check states
        # it directly rather than relying on that transitively.
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=SYNAPSE, namespace=NAMESPACE
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_config,
            forgejo_images,
            # Synapse is deployed and healthy; also carries the registration shared secret, admin and bot passwords
            matrix,
        ),
    )
