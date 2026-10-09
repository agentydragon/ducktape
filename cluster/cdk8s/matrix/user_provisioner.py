"""The Job that registers Matrix users against Synapse's admin API.

The image tag is the placeholder "unset"; the hand-written `PINS_DIR` Component, which the
kustomization includes across the roots, overrides it at `kustomize build` time via Flux's
image-automation marker.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo.images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.matrix.matrix import NAMESPACE, SYNAPSE
from cluster.cdk8s.secret_ref import SecretRef

OUTPUT_DIR = f"{GENERATED_ROOT}/matrix/user-provisioner"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/matrix/user-provisioner-image-pins"
_NAME = "matrix-user-provisioner"
# Script baked in via Bazel (//cluster/provisioners/matrix_user_provisioner:image).
_IMAGE = "git.allegedly.works/ducktape-ci/matrix-user-provisioner:unset"


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
                    automount_service_account_token=False,
                    restart_policy="OnFailure",
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    containers=[
                        k8s.Container(
                            name="provision",
                            image=_IMAGE,
                            image_pull_policy="Always",
                            env=[
                                SecretRef(namespace=NAMESPACE, name="synapse-registration-secret")
                                .key("registration_shared_secret")
                                .env_var("REGISTRATION_SECRET"),
                                SecretRef(namespace=NAMESPACE, name="synapse-admin-credentials")
                                .key("password")
                                .env_var("ADMIN_PASSWORD"),
                                SecretRef(namespace=NAMESPACE, name="public-coder-agent-matrix-bot-password")
                                .key("password")
                                .env_var("PUBLIC_CODER_AGENT_BOT_PASSWORD"),
                            ],
                        )
                    ],
                )
            ),
        ),
    )
    return chart


def matrix_user_provisioner(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, matrix: Kustomization
) -> Kustomization:
    name = "matrix-user-provisioner"
    return flux_kustomization(
        chart,
        name,
        directory,
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
            external_secrets_operator,
            # Synapse is deployed and healthy; also carries the registration shared secret, admin and bot passwords
            matrix,
        ),
    )
